"""Polymarket public data client: Gamma (markets/events/tags), CLOB (books), Data API (trades)."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from .http import Http, fnum

GAMMA = 'https://gamma-api.polymarket.com'
CLOB = 'https://clob.polymarket.com'
DATA = 'https://data-api.polymarket.com'
PAGE = 100


def _jlist(v: Any) -> list:
    if isinstance(v, list):
        return v
    if isinstance(v, str) and v.startswith('['):
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return []
    return []


class Polymarket:
    def __init__(self) -> None:
        self.gamma = Http(GAMMA, concurrency=6)
        self.clob = Http(CLOB, concurrency=6)
        self.data = Http(DATA, concurrency=6)

    async def aclose(self) -> None:
        await asyncio.gather(self.gamma.aclose(), self.clob.aclose(), self.data.aclose())

    async def tag_ids(self, tags: list[str]) -> list[int]:
        ids: list[int] = []
        for t in tags:
            t = t.strip()
            if t.isdigit():
                ids.append(int(t))
                continue
            d = await self.gamma.get_json(f'/tags/slug/{t.lower()}')
            if d and d.get('id'):
                ids.append(int(d['id']))
        return ids

    MAX_PAGES = 20  # Gamma rejects offset > 2000 ("use /markets/keyset for deeper pagination")

    async def _page_markets(self, params: dict[str, Any], max_pages: int = 20) -> list[dict]:
        out: list[dict] = []
        for p in range(min(max_pages, self.MAX_PAGES)):
            data = await self.gamma.get_json('/markets', {**params, 'limit': PAGE, 'offset': p * PAGE}) or []
            out.extend(data)
            if len(data) < PAGE:
                break
        return out

    async def markets(self, *, status: str, tag_ids: list[int], event_slugs: list[str], market_slugs: list[str],
                      settled_lookback_days: int, max_items: int) -> list[dict]:
        found: dict[str, dict] = {}
        statuses = {'open': ['open'], 'settled': ['settled'], 'all': ['open', 'settled']}[status]
        max_pages = max(1, min(self.MAX_PAGES, -(-max_items * 3 // PAGE)))  # over-fetch 3x for filtering, cap 2k (Gamma offset limit)
        tasks = []
        for st in statuses:
            if st == 'open':
                base = {'closed': 'false', 'active': 'true', 'order': 'volume24hr', 'ascending': 'false'}
            else:
                since = (datetime.now(timezone.utc) - timedelta(days=settled_lookback_days)).strftime('%Y-%m-%dT%H:%M:%SZ')
                base = {'closed': 'true', 'end_date_min': since, 'order': 'endDate', 'ascending': 'false'}
            if market_slugs:
                tasks.extend(self._page_markets({**base, 'slug': s}, 1) for s in market_slugs)
            if event_slugs:
                tasks.extend(self._events_markets(s, st) for s in event_slugs)
            if tag_ids:
                tasks.extend(self._page_markets({**base, 'tag_id': t}, max_pages) for t in tag_ids)
            if not (market_slugs or event_slugs or tag_ids):
                tasks.append(self._page_markets(base, max_pages))
        for batch in await asyncio.gather(*tasks):
            for m in batch:
                if m.get('conditionId'):
                    found[m['conditionId']] = m
        return list(found.values())

    async def _events_markets(self, slug: str, st: str) -> list[dict]:
        evs = await self.gamma.get_json('/events', {'slug': slug}) or []
        out = []
        for e in evs:
            for m in e.get('markets', []):
                m = dict(m)
                m.setdefault('events', [{'slug': e.get('slug'), 'title': e.get('title'), 'id': e.get('id')}])
                closed = str(m.get('closed')).lower() == 'true'
                if (st == 'open' and not closed) or (st == 'settled' and closed):
                    out.append(m)
        return out

    async def orderbook(self, token_id: str, depth: int) -> dict | None:
        b = await self.clob.get_json('/book', {'token_id': token_id})
        if not b:
            return None
        bids = sorted(({'price': fnum(x.get('price')), 'size': fnum(x.get('size'))} for x in b.get('bids', [])), key=lambda r: -(r['price'] or 0))
        asks = sorted(({'price': fnum(x.get('price')), 'size': fnum(x.get('size'))} for x in b.get('asks', [])), key=lambda r: (r['price'] or 0))
        return {'bids': bids[:depth], 'asks': asks[:depth]}

    async def trades(self, condition_id: str, limit: int) -> list[dict]:
        rows = await self.data.get_json('/trades', {'market': condition_id, 'limit': limit}) or []
        out = []
        for t in rows:
            ts = t.get('timestamp')
            when = datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat(timespec='seconds') if ts else None
            price = fnum(t.get('price'))
            yes_price = price if (t.get('outcome') or '').lower() == 'yes' or t.get('outcomeIndex') in (0, '0') else (None if price is None else round(1 - price, 4))
            out.append({'time': when, 'yes_price': yes_price, 'size': fnum(t.get('size')), 'taker_side': (t.get('side') or '').lower() or None})
        return out

    def normalize(self, m: dict) -> dict:
        outcomes = _jlist(m.get('outcomes'))
        prices = [fnum(p) for p in _jlist(m.get('outcomePrices'))]
        tokens = _jlist(m.get('clobTokenIds'))
        yes_idx = 0
        for i, o in enumerate(outcomes):
            if str(o).lower() == 'yes':
                yes_idx = i
                break
        yes_price = prices[yes_idx] if len(prices) > yes_idx else None
        bid = fnum(m.get('bestBid'))
        ask = fnum(m.get('bestAsk'))
        closed = str(m.get('closed')).lower() == 'true'
        active = str(m.get('active')).lower() == 'true'
        status = 'settled' if closed else ('open' if active else 'closed')
        result = None
        if closed and prices and len(prices) == len(outcomes):
            winners = [str(o) for o, p in zip(outcomes, prices) if p is not None and p >= 0.99]
            result = winners[0].lower() if len(winners) == 1 else None
        ev = (_jlist(m.get('events')) or [{}])[0] if m.get('events') else {}
        ev_slug = ev.get('slug')
        url = f"https://polymarket.com/event/{ev_slug}/{m.get('slug')}" if ev_slug else f"https://polymarket.com/market/{m.get('slug')}"
        day_change = fnum(m.get('oneDayPriceChange'))
        return {
            'source': 'polymarket',
            'id': m.get('conditionId'),
            'url': url,
            'title': m.get('question'),
            'outcome_label': m.get('groupItemTitle') or None,
            'event_id': ev.get('id') or ev_slug,
            'event_title': ev.get('title'),
            'series_id': ev_slug,
            'series_title': ev.get('title'),
            'category': (ev.get('category') or None),
            'tags': [t.get('slug') for t in _jlist(m.get('tags')) if isinstance(t, dict) and t.get('slug')],
            'status': status,
            'market_type': 'binary' if len(outcomes) == 2 else 'multi',
            'yes_bid': bid,
            'yes_ask': ask,
            'last_price': fnum(m.get('lastTradePrice')),
            'yes_price': yes_price,
            'no_price': None if yes_price is None else round(1 - yes_price, 4),
            'implied_probability': yes_price,
            'spread': fnum(m.get('spread')) if m.get('spread') is not None else (None if bid is None or ask is None else round(ask - bid, 4)),
            'volume': fnum(m.get('volumeNum')) or fnum(m.get('volume')),
            'volume_24h': fnum(m.get('volume24hr')),
            'open_interest': None,
            'liquidity': fnum(m.get('liquidityNum')) or fnum(m.get('liquidity')),
            'open_time': m.get('startDate'),
            'close_time': m.get('endDate'),
            'expiration_time': m.get('endDate'),
            'settled_time': m.get('closedTime') or None,
            'result': result,
            'settlement_value': None,
            'strike_type': None,
            'floor_strike': None,
            'cap_strike': None,
            'settlement_station': None,
            'rules': (m.get('description') or None),
            'price_change_24h_pts': None if day_change is None else round(day_change * 100, 2),
            'outcomes': outcomes,
            'outcome_prices': prices,
            'yes_token_id': tokens[yes_idx] if len(tokens) > yes_idx else None,
            'fetched_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        }
