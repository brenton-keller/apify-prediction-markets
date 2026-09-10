"""Kalshi public market-data client (unauthenticated trade-api v2)."""
from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timezone
from typing import Any

from .http import Http, fnum

BASE = 'https://api.elections.kalshi.com/trade-api/v2'
WEATHER_CATEGORY = 'Climate and Weather'
_STATION_RE = re.compile(r'\(([A-Z0-9]{3,8})\)')


def _slug(text: str) -> str:
    """kalshi.com path slug: lowercase, non-alphanumerics collapsed to single hyphens."""
    return re.sub(r'-+', '-', re.sub(r'[^a-z0-9]+', '-', text.lower())).strip('-')


class Kalshi:
    SERIES_FANOUT_MAX = 60

    def __init__(self, http: Http | None = None) -> None:
        self.http = http or Http(BASE, concurrency=4, max_rps=4)
        self._series: dict[str, dict] | None = None

    async def series_index(self) -> dict[str, dict]:
        """All series keyed by ticker (one call, ~14k rows)."""
        if self._series is None:
            data = await self.http.get_json('/series', {'limit': 5000}) or {}
            self._series = {s['ticker']: s for s in data.get('series', [])}
        return self._series

    async def select_series(self, categories: list[str], queries: list[str], cities: list[str], weather: bool) -> list[str] | None:
        """Return series tickers matching filters, or None when no series-level filter applies."""
        if not (categories or weather or cities or queries):
            return None
        idx = await self.series_index()
        cats = set(categories or [])
        if weather:
            cats.add(WEATHER_CATEGORY)
        out = []
        for t, s in idx.items():
            if cats and s.get('category') not in cats:
                continue
            title = (s.get('title') or '').lower()
            hay = f"{title} {t.lower()} {' '.join((s.get('tags') or []))}".lower()
            if cities and not any(c.lower() in hay for c in cities):
                continue
            if queries and not any(q.lower() in hay for q in queries):
                # queries may also match market titles later; keep series only when filtered by category/weather
                if not (cats):
                    continue
            out.append(t)
        return out

    async def markets(self, *, status: str, series_tickers: list[str] | None, event_tickers: list[str],
                      market_tickers: list[str], settled_lookback_days: int, on_batch, max_pages: int = 400) -> int:
        """Stream markets page by page into `on_batch(list[dict])`. Returns number of raw markets seen.

        Streaming keeps memory flat: a full open-market scan is 100k+ rows, far too many to hold at once.
        """
        statuses = {'open': ['open'], 'settled': ['settled'], 'all': ['open', 'settled']}[status]
        min_close_ts = int(time.time()) - settled_lookback_days * 86400
        seen = 0
        lock = asyncio.Lock()
        # Many series: one streamed full-board scan (~120 requests) beats one request per series, which trips Kalshi's rate limit.
        series_set: set[str] | None = None
        if series_tickers is not None and len(series_tickers) > self.SERIES_FANOUT_MAX and status == 'open':
            series_set = {s.upper() for s in series_tickers}
            series_tickers = None

        async def page(params: dict[str, Any]) -> None:
            nonlocal seen
            cursor = None
            for _ in range(max_pages):
                data = await self.http.get_json('/markets', {**params, 'limit': 1000, 'cursor': cursor, 'mve_filter': 'exclude'}) or {}
                batch = data.get('markets', [])
                if series_set is not None:
                    batch = [m for m in batch if (m.get('event_ticker') or '').split('-')[0] in series_set]
                seen += len(batch)
                async with lock:
                    on_batch(batch)
                cursor = data.get('cursor')
                if not cursor or len(data.get('markets', [])) < 1000:
                    break

        tasks = []
        for st in statuses:
            base = {'status': st}
            if st == 'settled':
                base['min_close_ts'] = min_close_ts
            if market_tickers:
                for i in range(0, len(market_tickers), 100):
                    tasks.append(page({**base, 'tickers': ','.join(market_tickers[i:i + 100])}))
            if event_tickers:
                tasks.extend(page({**base, 'event_ticker': e}) for e in event_tickers)
            if series_tickers is not None and not (market_tickers or event_tickers):
                tasks.extend(page({**base, 'series_ticker': s}) for s in series_tickers)
            if not (market_tickers or event_tickers or series_tickers is not None):
                tasks.append(page(base))
        await asyncio.gather(*tasks)
        return seen

    async def event_titles(self, event_tickers: list[str], max_events: int = 400) -> dict[str, str]:
        """Title per event ticker (one call each, deduped, concurrency-limited). Beyond `max_events` titles stay None."""
        tickers = list(dict.fromkeys(t for t in event_tickers if t))[:max_events]

        async def one(t: str) -> tuple[str, str | None]:
            try:
                data = await self.http.get_json(f'/events/{t}') or {}
            except Exception:
                return t, None
            ev = data.get('event') or {}
            return t, ev.get('title') or None

        pairs = await asyncio.gather(*(one(t) for t in tickers))
        return {t: title for t, title in pairs if title}

    async def event_index(self, status: str, series_tickers: list[str] | None = None, max_pages: int = 100) -> dict[str, str]:
        """event_ticker -> title for every event (paged /events, 200 per call); cheaper than one call per event past ~50 events."""
        out: dict[str, str] = {}
        statuses = {'open': ['open'], 'settled': ['settled'], 'all': ['open', 'settled']}[status]

        async def page(params: dict) -> None:
            cursor = None
            for _ in range(max_pages):
                data = await self.http.get_json('/events', {**params, 'limit': 200, 'cursor': cursor}) or {}
                for e in data.get('events', []):
                    if e.get('event_ticker') and e.get('title'):
                        out[e['event_ticker']] = e['title']
                cursor = data.get('cursor')
                if not cursor:
                    break

        tasks = []
        for st in statuses:
            if series_tickers:
                tasks.extend(page({'status': st, 'series_ticker': t}) for t in series_tickers)
            else:
                tasks.append(page({'status': st}))
        await asyncio.gather(*tasks)
        return out

    async def orderbook(self, ticker: str, depth: int) -> dict | None:
        data = await self.http.get_json(f'/markets/{ticker}/orderbook', {'depth': depth}) or {}
        ob = data.get('orderbook_fp') or data.get('orderbook') or {}
        def side(rows: list) -> list[dict]:
            return [{'price': fnum(p), 'size': fnum(q)} for p, q in (rows or [])]
        yes = side(ob.get('yes_dollars') or ob.get('yes'))
        no = side(ob.get('no_dollars') or ob.get('no'))
        # Kalshi books list resting YES bids and NO bids; a NO bid at p is a YES ask at 1-p.
        asks = sorted(({'price': round(1 - r['price'], 4), 'size': r['size']} for r in no if r['price'] is not None), key=lambda r: r['price'])
        bids = sorted((r for r in yes if r['price'] is not None), key=lambda r: -r['price'])
        return {'bids': bids[:depth], 'asks': asks[:depth]}

    async def trades(self, ticker: str, limit: int) -> list[dict]:
        data = await self.http.get_json('/markets/trades', {'ticker': ticker, 'limit': limit}) or {}
        out = []
        for t in data.get('trades', []):
            out.append({
                'time': t.get('created_time'),
                'yes_price': fnum(t.get('yes_price_dollars')),
                'size': fnum(t.get('count_fp')),
                'taker_side': t.get('taker_side'),
            })
        return out

    def normalize(self, m: dict, series: dict | None) -> dict:
        yes_bid = fnum(m.get('yes_bid_dollars'))
        yes_ask = fnum(m.get('yes_ask_dollars'))
        last = fnum(m.get('last_price_dollars'))
        if yes_bid is not None and yes_ask is not None and (yes_bid > 0 or yes_ask < 1):
            mid = round((yes_bid + yes_ask) / 2, 4)
        else:
            mid = last
        status_raw = m.get('status') or ''
        status = {'active': 'open', 'open': 'open', 'initialized': 'open', 'closed': 'closed',
                  'settled': 'settled', 'finalized': 'settled', 'determined': 'settled'}.get(status_raw, status_raw)
        rules = m.get('rules_primary') or ''
        station = _STATION_RE.search(rules)
        series_ticker = (series or {}).get('ticker') or m.get('event_ticker', '').split('-')[0]
        result = m.get('result') or None
        event_ticker = m.get('event_ticker') or ''
        series_slug = _slug((series or {}).get('title') or series_ticker)
        return {
            'source': 'kalshi',
            'id': m['ticker'],
            'url': f"https://kalshi.com/markets/{series_ticker.lower()}/{series_slug}",
            'event_url': f"https://kalshi.com/markets/{series_ticker.lower()}/{series_slug}/{event_ticker.lower()}" if event_ticker else None,
            'title': m.get('title'),
            'outcome_label': m.get('yes_sub_title') or m.get('subtitle'),
            'event_id': m.get('event_ticker'),
            'event_title': None,
            'series_id': series_ticker,
            'series_title': (series or {}).get('title'),
            'category': (series or {}).get('category'),
            'tags': (series or {}).get('tags') or [],
            'status': status,
            'market_type': m.get('market_type'),
            'yes_bid': yes_bid,
            'yes_ask': yes_ask,
            'last_price': last,
            'yes_price': mid,
            'no_price': None if mid is None else round(1 - mid, 4),
            'implied_probability': mid,
            'spread': None if yes_bid is None or yes_ask is None else round(yes_ask - yes_bid, 4),
            'volume': fnum(m.get('volume_fp')) or fnum(m.get('volume')),
            'volume_24h': fnum(m.get('volume_24h_fp')) or fnum(m.get('volume_24h')),
            'open_interest': fnum(m.get('open_interest_fp')) or fnum(m.get('open_interest')),
            'liquidity': fnum(m.get('liquidity_dollars')),
            'open_time': m.get('open_time'),
            'close_time': m.get('close_time'),
            'expiration_time': m.get('expiration_time'),
            'settled_time': m.get('settled_time'),
            'result': result,
            'settlement_value': m.get('expiration_value') or None,
            'strike_type': m.get('strike_type'),
            'floor_strike': m.get('floor_strike'),
            'cap_strike': m.get('cap_strike'),
            'settlement_station': station.group(1) if station else None,
            'rules': rules or None,
            'can_close_early': m.get('can_close_early'),
            'fetched_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        }
