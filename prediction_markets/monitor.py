"""Changes-only mode: remember a per-row value (YES price, or spread) in a named key-value store between runs."""
from __future__ import annotations

from datetime import datetime, timezone

from apify import Actor

KEY = 'snapshot'


class Monitor:
    """`field` is the tracked value; `scale` converts its delta to points (100 for 0-1 prices, 1 for values already in points)."""

    def __init__(self, store_name: str, field: str = 'yes_price', scale: float = 100,
                 prev_key: str = 'previous_yes_price', move_key: str = 'price_move_pts') -> None:
        self.store_name = store_name
        self.field, self.scale, self.prev_key, self.move_key = field, scale, prev_key, move_key
        self.prev: dict[str, dict] = {}
        self.next: dict[str, dict] = {}
        self._kv = None

    async def load(self) -> None:
        self._kv = await Actor.open_key_value_store(name=self.store_name)
        self.prev = (await self._kv.get_value(KEY)) or {}
        Actor.log.info('Monitor: loaded %d previously seen rows from store "%s"', len(self.prev), self.store_name)

    def annotate(self, rec: dict) -> dict:
        key = f"{rec['source']}:{rec['id']}"
        value = rec.get(self.field)
        old = self.prev.get(key)
        rec['is_new'] = old is None
        rec[self.prev_key] = None if old is None else old.get('p')
        rec['previous_seen_at'] = None if old is None else old.get('t')
        if old is not None and old.get('p') is not None and value is not None:
            rec[self.move_key] = round((value - old['p']) * self.scale, 2)
        else:
            rec[self.move_key] = None
        self.next[key] = {'p': value, 't': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'status': rec.get('status')}
        return rec

    def changed(self, rec: dict, min_move_pts: float) -> bool:
        if rec.get('is_new'):
            return True
        mv = rec.get(self.move_key)
        if mv is not None and abs(mv) >= min_move_pts:
            return True
        old = self.prev.get(f"{rec['source']}:{rec['id']}") or {}
        return bool(old) and old.get('status') != rec.get('status')

    async def save(self) -> None:
        if self._kv is None:
            return
        merged = {**self.prev, **self.next}
        await self._kv.set_value(KEY, merged)
        Actor.log.info('Monitor: saved %d rows to store "%s"', len(merged), self.store_name)
