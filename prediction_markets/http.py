"""Small resilient HTTP helper shared by both exchange clients."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

log = logging.getLogger('apify')

RETRY_STATUSES = {408, 425, 429, 500, 502, 503, 504}


class Http:
    def __init__(self, base_url: str, concurrency: int = 6, timeout: float = 30.0, max_rps: float | None = None) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            headers={'User-Agent': 'prediction-markets-data/0.1 (+https://apify.com)', 'Accept': 'application/json'},
        )
        self._sem = asyncio.Semaphore(concurrency)
        self._min_gap = (1.0 / max_rps) if max_rps else 0.0  # simple request pacing (Kalshi allows ~10 reads/s unauthenticated)
        self._next_slot = 0.0
        self._pace_lock = asyncio.Lock()
        self.requests = 0
        self.retries = 0

    async def _pace(self) -> None:
        if not self._min_gap:
            return
        async with self._pace_lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            wait = self._next_slot - now
            self._next_slot = max(now, self._next_slot) + self._min_gap
        if wait > 0:
            await asyncio.sleep(wait)

    async def get_json(self, path: str, params: dict[str, Any] | None = None, retries: int = 6) -> Any:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        delay = 0.8
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            async with self._sem:
                try:
                    await self._pace()
                    self.requests += 1
                    r = await self._client.get(path, params=params)
                except (httpx.TransportError, httpx.TimeoutException) as e:
                    last_exc = e
                    r = None
            if r is not None:
                if r.status_code == 200:
                    return r.json()
                if r.status_code == 404:
                    return None
                if r.status_code not in RETRY_STATUSES:
                    raise httpx.HTTPStatusError(f'{r.status_code} for {r.url}: {r.text[:200]}', request=r.request, response=r)
                last_exc = httpx.HTTPStatusError(f'{r.status_code} for {r.url}', request=r.request, response=r)
                retry_after = r.headers.get('retry-after')
                if retry_after and retry_after.isdigit():
                    delay = max(delay, float(retry_after))
            if attempt < retries:
                self.retries += 1
                log.debug('retrying %s (%s) in %.1fs', path, last_exc, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)
        raise last_exc  # type: ignore[misc]

    async def aclose(self) -> None:
        await self._client.aclose()


def fnum(v: Any, default: float | None = None) -> float | None:
    """Parse numbers that upstream APIs return as strings ('0.0100', '1244.00')."""
    if v is None or v == '':
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default
