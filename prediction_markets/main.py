"""Prediction Markets Data: Kalshi + Polymarket. Apify Actor entry point."""
from __future__ import annotations

import asyncio
from typing import Any

from apify import Actor

from .kalshi import Kalshi
from .monitor import Monitor
from .polymarket import Polymarket
from .spread import build_pairs, parse_pairs, tokens

EVENT_BASE = 'market-record'
EVENT_ENRICHED = 'enriched-market-record'
EVENT_SPREAD = 'spread-record'

SORT_KEYS = {
    'volume_24h': (lambda r: -(r.get('volume_24h') or 0)),
    'liquidity': (lambda r: -(r.get('liquidity') or 0)),
    'open_interest': (lambda r: -(r.get('open_interest') or 0)),
    'close_time': (lambda r: r.get('close_time') or '9999'),
}


def _matches(rec: dict, queries: list[str]) -> bool:
    if not queries:
        return True
    hay = ' '.join(str(rec.get(k) or '') for k in ('title', 'outcome_label', 'event_title', 'series_title', 'series_id', 'id')).lower()
    return any(q.lower() in hay for q in queries)


def _passes(rec: dict, inp: dict) -> bool:
    if (rec.get('volume_24h') or 0) < inp['minVolume24h'] and inp['minVolume24h'] > 0:
        return False
    if (rec.get('liquidity') or 0) < inp['minLiquidity'] and inp['minLiquidity'] > 0:
        return False
    if rec['source'] == 'kalshi' and inp['minOpenInterest'] > 0 and (rec.get('open_interest') or 0) < inp['minOpenInterest']:
        return False
    return True


class Keeper:
    """Bounded collector: keeps the best `cap` records by sort key without holding the whole stream."""

    def __init__(self, cap: int, key) -> None:
        self.cap, self.key, self.rows, self.seen_ids = cap, key, [], set()

    def add(self, rec: dict) -> None:
        if rec['id'] in self.seen_ids:
            return
        self.seen_ids.add(rec['id'])
        self.rows.append(rec)
        if len(self.rows) > self.cap * 2:
            self.rows.sort(key=self.key)
            del self.rows[self.cap:]

    def result(self) -> list[dict]:
        self.rows.sort(key=self.key)
        return self.rows[: self.cap]


async def collect_kalshi(inp: dict, k: Kalshi) -> list[dict]:
    if inp.get('_series_override'):
        series_filter: list[str] | None = inp['_series_override']
    else:
        series_filter = await k.select_series(inp['kalshiCategories'], inp['searchQueries'], inp['cities'], inp['weatherPreset'])
        if series_filter is not None:
            Actor.log.info('Kalshi: %d series match filters', len(series_filter))
            if not series_filter and not (inp['kalshiEventTickers'] or inp['kalshiMarketTickers']):
                return []
            # Search keywords alone are matched against market titles too, which needs a full scan.
            if not (inp['kalshiCategories'] or inp['weatherPreset'] or inp['cities']):
                series_filter = None
    idx = await k.series_index()
    # In monitor mode every market must be seen (a bounded top-N would hide moves), so keep everything that passes filters.
    uncapped = inp['changesOnly'] or inp['mode'] == 'spread'
    keeper = Keeper(inp['maxItems'] if not uncapped else 10**9, SORT_KEYS[inp['sortBy']])
    cities = inp['cities']
    prefilter = inp.get('_kalshi_prefilter')

    def on_batch(batch: list[dict]) -> None:
        for m in batch:
            ev = m.get('event_ticker', '')
            s = idx.get(ev.rsplit('-', 1)[0]) or idx.get(ev.split('-')[0])
            rec = k.normalize(m, s)
            if inp['status'] != 'all' and rec.get('status') != inp['status']:
                continue
            if cities and not any(c.lower() in f"{rec.get('series_title') or ''} {rec.get('title') or ''}".lower() for c in cities):
                continue
            if prefilter is not None and not prefilter(rec):
                continue
            if _matches(rec, inp['searchQueries']) and _passes(rec, inp):
                if inp['includeRaw']:
                    rec['raw'] = m
                keeper.add(rec)

    seen = await k.markets(status=inp['status'], series_tickers=series_filter, event_tickers=inp['kalshiEventTickers'],
                           market_tickers=inp['kalshiMarketTickers'], settled_lookback_days=inp['settledLookbackDays'], on_batch=on_batch)
    out = keeper.result()
    Actor.log.info('Kalshi: scanned %d markets, %d match (%d requests)', seen, len(out), k.http.requests)
    return out


async def collect_polymarket(inp: dict, p: Polymarket) -> list[dict]:
    tags = list(inp['polymarketTags'])
    if inp['weatherPreset']:
        tags.append('weather')
    tag_ids = await p.tag_ids(tags) if tags else []
    if tags and not tag_ids and not (inp['polymarketEventSlugs'] or inp['polymarketMarketSlugs']):
        Actor.log.warning('Polymarket: none of the tags %s resolved to a tag id', tags)
        return []
    pool = inp['maxItems'] if inp['mode'] != 'spread' else max(inp['maxItems'], inp['spreadPoolSize'])
    raw = await p.markets(status=inp['status'], tag_ids=tag_ids, event_slugs=inp['polymarketEventSlugs'],
                          market_slugs=inp['polymarketMarketSlugs'], settled_lookback_days=inp['settledLookbackDays'],
                          max_items=pool)
    Actor.log.info('Polymarket: fetched %d markets (%d requests)', len(raw), p.gamma.requests)
    out = []
    for m in raw:
        rec = p.normalize(m)
        if inp['cities'] and not any(c.lower() in f"{rec.get('title') or ''} {rec.get('event_title') or ''}".lower() for c in inp['cities']):
            continue
        if _matches(rec, inp['searchQueries']) and _passes(rec, inp):
            if inp['includeRaw']:
                rec['raw'] = m
            out.append(rec)
    return out


async def collect_spreads(inp: dict, k: Kalshi, p: Polymarket) -> list[dict]:
    """Spread mode: Polymarket first (bounded pool), then Kalshi pre-filtered to rows that can plausibly match, then pairing."""
    p_rows = await collect_polymarket(inp, p)
    if inp.get('_pair_condition_ids'):
        for cid in inp['_pair_condition_ids']:
            for m in await p._page_markets({'condition_ids': cid}, 1):
                if m.get('conditionId'):
                    p_rows.append(p.normalize(m))
    Actor.log.info('Spread: %d Polymarket candidates', len(p_rows))
    # Blocking index: a Kalshi row is kept only if its title/series shares two tokens with some Polymarket event title
    # (one token when the Polymarket title is that short). Keeps a full-board Kalshi scan in memory.
    p_tok = [tokens(r.get('event_title') or r.get('title')) for r in p_rows]
    explicit_k = {kt for kt, _ in inp['_pairs']}

    def prefilter(rec: dict) -> bool:
        if rec['id'] in explicit_k:
            return True
        kt = tokens(f"{rec.get('series_title') or ''} {rec.get('title') or ''}")
        for pt in p_tok:
            need = 1 if len(pt) <= 2 else 2
            if len(kt & pt) >= need:
                return True
        return False

    inp['_kalshi_prefilter'] = prefilter
    k_rows = await collect_kalshi(inp, k)
    # Event titles for the kept Kalshi rows: per-event lookups when few, one paged listing when many.
    events = sorted({r['event_id'] for r in k_rows if r.get('event_id') and not r.get('event_title')})
    if events:
        if len(events) <= 50:
            titles = await k.event_titles(events)
        else:
            series = sorted({r['series_id'] for r in k_rows if r.get('series_id')})
            titles = await k.event_index(inp['status'], series if len(series) <= 200 else None)
        for r in k_rows:
            if r.get('event_id') in titles:
                r['event_title'] = titles[r['event_id']]
        Actor.log.info('Spread: resolved %d Kalshi event titles for %d events', sum(1 for e in events if e in titles), len(events))
    rows, stats = build_pairs(k_rows, p_rows, inp['_pairs'], inp['minMatchScore'] / 100)
    if stats['missing']:
        Actor.log.warning('Spread: %d explicit pairs not found: %s', len(stats['missing']), stats['missing'][:10])
    Actor.log.info('Spread: %d Kalshi candidates, %d pairs (%d explicit, %d auto)', len(k_rows), len(rows), stats['explicit'], stats['auto'])
    if inp['minSpreadPts'] > 0:
        rows = [r for r in rows if (r.get('abs_spread_pts') or 0) >= inp['minSpreadPts']]
    rows.sort(key=lambda r: (-(r.get('abs_spread_pts') or 0), -(r.get('net_edge_pts') or -999)))
    return rows


async def enrich(rec: dict, inp: dict, k: Kalshi | None, p: Polymarket | None) -> dict:
    try:
        if rec['source'] == 'kalshi' and k is not None:
            if inp['includeOrderbook']:
                rec['orderbook'] = await k.orderbook(rec['id'], inp['orderbookDepth'])
            if inp['includeRecentTrades']:
                rec['recent_trades'] = await k.trades(rec['id'], inp['tradesLimit'])
        elif rec['source'] == 'polymarket' and p is not None:
            if inp['includeOrderbook'] and rec.get('yes_token_id'):
                rec['orderbook'] = await p.orderbook(rec['yes_token_id'], inp['orderbookDepth'])
            if inp['includeRecentTrades']:
                rec['recent_trades'] = await p.trades(rec['id'], inp['tradesLimit'])
    except Exception as e:  # enrichment is best-effort; the base record is still valuable
        Actor.log.warning('enrichment failed for %s: %s', rec['id'], e)
        rec['enrichment_error'] = str(e)[:200]
    return rec


def read_input(raw: dict[str, Any] | None) -> dict:
    raw = raw or {}
    defaults = {
        'sources': ['kalshi', 'polymarket'], 'status': 'open', 'searchQueries': [], 'weatherPreset': False, 'cities': [],
        'kalshiCategories': [], 'kalshiSeriesTickers': [], 'kalshiEventTickers': [], 'kalshiMarketTickers': [],
        'polymarketTags': [], 'polymarketEventSlugs': [], 'polymarketMarketSlugs': [],
        'minVolume24h': 0, 'minLiquidity': 0, 'minOpenInterest': 0, 'settledLookbackDays': 7, 'sortBy': 'volume_24h',
        'maxItems': 500, 'includeOrderbook': False, 'includeRecentTrades': False, 'tradesLimit': 25, 'orderbookDepth': 10,
        'changesOnly': False, 'minPriceMovePts': 2, 'monitorStoreName': 'prediction-markets-monitor', 'includeRaw': False,
        'mode': 'markets', 'pairs': [], 'minSpreadPts': 0, 'minMatchScore': 60, 'spreadPoolSize': 2000,
    }
    inp = {**defaults, **{k: v for k, v in raw.items() if v is not None}}
    inp['_blank_only'] = []
    for key in ('searchQueries', 'cities', 'kalshiCategories', 'kalshiSeriesTickers', 'kalshiEventTickers', 'kalshiMarketTickers',
                'polymarketTags', 'polymarketEventSlugs', 'polymarketMarketSlugs', 'pairs'):
        given = list(inp[key] or [])
        inp[key] = [str(x).strip() for x in given if str(x).strip()]
        if given and not inp[key]:
            inp['_blank_only'].append(key)  # the user typed a filter but left it blank; do not silently run (and bill) unfiltered
    inp['_pairs'] = parse_pairs(inp['pairs'])
    if inp['_pairs']:
        inp['kalshiMarketTickers'] = list(dict.fromkeys(inp['kalshiMarketTickers'] + [k for k, _ in inp['_pairs']]))
        inp['polymarketMarketSlugs'] = list(dict.fromkeys(inp['polymarketMarketSlugs'] + [p for _, p in inp['_pairs'] if not p.startswith('0x')]))
        inp['_pair_condition_ids'] = [p for _, p in inp['_pairs'] if p.startswith('0x')]
    if inp['kalshiSeriesTickers']:
        inp['_series_override'] = [s.upper() for s in inp['kalshiSeriesTickers']]
    return inp


async def main() -> None:
    async with Actor:
        inp = read_input(await Actor.get_input())
        Actor.log.info('Input: %s', {k: v for k, v in inp.items() if v not in ([], 0, False, None) and not k.startswith('_')})
        if inp['_blank_only']:
            msg = f"Nothing fetched: {', '.join(inp['_blank_only'])} contained only blank values. Remove the field to run unfiltered."
            Actor.log.warning(msg)
            await Actor.set_status_message(msg, is_terminal=True)
            return
        spread_mode = inp['mode'] == 'spread'
        want_k = 'kalshi' in inp['sources'] or spread_mode
        want_p = 'polymarket' in inp['sources'] or spread_mode
        k = Kalshi() if want_k else None
        p = Polymarket() if want_p else None
        if spread_mode:
            monitor = Monitor(inp['monitorStoreName'], field='spread_pts', scale=1, prev_key='previous_spread_pts', move_key='spread_move_pts') if inp['changesOnly'] else None
        else:
            monitor = Monitor(inp['monitorStoreName']) if inp['changesOnly'] else None
        if monitor:
            await monitor.load()

        await Actor.set_status_message('Fetching markets…')
        if spread_mode:
            records = await collect_spreads(inp, k, p)
        else:
            tasks = []
            if k:
                tasks.append(collect_kalshi(inp, k))
            if p:
                tasks.append(collect_polymarket(inp, p))
            results = await asyncio.gather(*tasks, return_exceptions=True)
            records = []
            failures = []
            for r in results:
                if isinstance(r, Exception):
                    failures.append(r)
                    Actor.log.exception('source failed', exc_info=r)
                else:
                    records.extend(r)
            if failures and not records:
                raise failures[0]
            records.sort(key=SORT_KEYS[inp['sortBy']])
        if monitor:
            records = [monitor.annotate(r) for r in records]
            before = len(records)
            records = [r for r in records if monitor.changed(r, inp['minPriceMovePts'])]
            Actor.log.info('Monitor: %d of %d markets are new or moved >= %s pts', len(records), before, inp['minPriceMovePts'])
        records = records[: inp['maxItems']]

        # Kalshi's /markets rows carry no event title; fill it for the final rows only (one call per distinct event).
        if k and not spread_mode:
            k_events = [r['event_id'] for r in records if r['source'] == 'kalshi' and r.get('event_id') and not r.get('event_title')]
            if k_events:
                titles = await k.event_titles(k_events)
                for r in records:
                    if r['source'] == 'kalshi' and r.get('event_id') in titles:
                        r['event_title'] = titles[r['event_id']]
                Actor.log.info('Kalshi: resolved %d event titles for %d events', len(titles), len(set(k_events)))

        enriched = (inp['includeOrderbook'] or inp['includeRecentTrades']) and not spread_mode
        if enriched and records:
            await Actor.set_status_message(f'Enriching {len(records)} markets with orderbook/trades…')
            records = list(await asyncio.gather(*(enrich(r, inp, k, p) for r in records)))

        def event_for(r: dict) -> str:
            # Charge the enriched rate only for rows that actually carry enrichment data; spread pairs have their own event.
            if r.get('source') == 'spread':
                return EVENT_SPREAD
            return EVENT_ENRICHED if (r.get('orderbook') or r.get('recent_trades')) else EVENT_BASE

        pushed = 0
        base_billed = 0
        stop = False

        async def flush(batch: list[dict], event: str) -> None:
            nonlocal pushed, stop, base_billed
            if not batch or stop:
                return
            res = await Actor.push_data(batch, charged_event_name=event)
            charged = getattr(res, 'charged_count', None)
            pushed += charged if (charged is not None and getattr(res, 'is_pay_per_event', None) is not False and charged > 0) else len(batch)
            if event == EVENT_BASE:
                base_billed += len(batch)
            if res is not None and getattr(res, 'event_charge_limit_reached', False):
                Actor.log.warning('Charge limit reached after %d records; stopping. Raise "Maximum total charge" on the run to get more.', pushed)
                stop = True

        # Push in sorted order; start a new batch whenever the billing event changes or the batch reaches 100 rows.
        batch: list[dict] = []
        batch_event = EVENT_BASE
        for r in records:
            ev = event_for(r)
            if batch and (ev != batch_event or len(batch) >= 100):
                await flush(batch, batch_event)
                batch = []
            batch_event = ev
            batch.append(r)
        await flush(batch, batch_event)
        if enriched and base_billed:
            Actor.log.info('%d rows had no enrichment data and were billed at the base rate', base_billed)

        if monitor:
            await monitor.save()
        if spread_mode:
            msg = f"Done: {pushed} cross-venue pairs" + (' [changes only]' if monitor else '')
        else:
            by_src = {s: sum(1 for r in records if r['source'] == s) for s in ('kalshi', 'polymarket')}
            msg = f"Done: {pushed} markets ({by_src['kalshi']} Kalshi, {by_src['polymarket']} Polymarket)" + (' [changes only]' if monitor else '')
        Actor.log.info(msg)
        await Actor.set_status_message(msg, is_terminal=True)
        if k:
            await k.http.aclose()
        if p:
            await p.aclose()
