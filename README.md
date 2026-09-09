# Prediction Markets Data: Kalshi + Polymarket

Live prices, orderbook depth, recent trades and settlement history from **Kalshi** and **Polymarket** in one unified schema. Built on both exchanges' official public APIs (no login, no browser, no proxies), so runs are fast and do not break when a website changes. Includes a **changes-only monitor mode** so a scheduled run gives you a clean feed of what moved.

The reliability wedge: official APIs, no scraping, no proxies, no login. Every scenario in testing (top markets, keyword search, weather brackets with orderbooks, settled history, monitor mode) completed with a 100% success rate.

## What it does

- Pulls open, settled, or all markets from Kalshi and/or Polymarket in a single run.
- Normalizes both venues into the same columns: `yes_price`, `implied_probability`, `yes_bid`, `yes_ask`, `spread`, `volume_24h`, `liquidity`, `open_interest`, `close_time`, `result`, and more.
- Targets markets by keyword, Kalshi category, series/event/market ticker, Polymarket tag, event slug, or market slug.
- Optional enrichment per market: top-of-book **orderbook** (configurable depth) and the most recent **trades** (configurable count).
- **Settlement history**: fetch resolved markets from the last N days with their result.
- **Weather preset**: Kalshi daily high/low temperature brackets, rain and hurricane series (plus Polymarket's weather tag), filterable by city.
- **Monitor mode**: remembers the last-seen YES price per market in a named key-value store and outputs only markets that are new or moved at least N probability points.

## Cross-venue spread mode (Kalshi vs Polymarket)

Set `"mode": "spread"` and the actor pairs the same question on both exchanges and prices the gap. One row per pair:

- `kalshi_yes_price`, `polymarket_yes_price`, `spread_pts` (Kalshi minus Polymarket, in probability points)
- `arb_edge_pts`: the executable edge before fees, from the books: buy YES at the cheaper venue's ask and buy NO at the other venue (1 minus its YES bid); both legs together pay $1 at settlement, so edge = bid minus ask. `arb_direction` says which way (`yes_kalshi_no_polymarket` or `yes_polymarket_no_kalshi`); null when there is no positive edge
- `kalshi_fee_est_pts` (Kalshi taker fee, 7% x P x (1-P) rounded up to the cent) and `net_edge_pts` = edge minus that fee
- `match_score` (0-1) and `match_method` (`auto` or `explicit`), both venues' links, volumes, close times, settlement rules

Pairing is deliberately conservative. Events are paired when their titles agree after normalisation (NYC = New York City, Sep = September, years dropped) and their dates do not conflict; markets inside a paired event are matched by bracket ("80-81", "79 or below", "80 or higher"), by 1:1 events, or by outcome label. Anything uncertain is left out. For questions the matcher cannot pair, pass them yourself:

```json
{
  "mode": "spread",
  "pairs": ["KXHIGHNY-26SEP08-T80=will-the-highest-temperature-in-new-york-city-be-between-80-81f-on-september-8"]
}
```

**All weather brackets for three cities, biggest gaps first:**

```json
{ "mode": "spread", "weatherPreset": true, "cities": ["NYC", "Chicago", "Miami"] }
```

**Spread monitor, scheduled every 15 minutes, alert when a gap moves 3+ points or a new pair appears:**

```json
{ "mode": "spread", "weatherPreset": true, "changesOnly": true, "minPriceMovePts": 3, "monitorStoreName": "weather-spreads" }
```

Add an Apify integration (Slack, email, webhook) on the actor's Integrations tab and you have an alert feed without any extra code.

Read the rules before trading a gap: the two venues do not always settle on the same source. Miami daily highs, for example, can close at 99% on different brackets because Kalshi and Polymarket read different weather stations. `kalshi_settlement_station`, `kalshi_rules` and `polymarket_rules` are on every row for that check. Rows are sorted by absolute spread, then net edge; `minSpreadPts` drops small gaps; `minMatchScore` (default 60) loosens or tightens auto pairing. Spread rows are billed as spread records (see Pricing).

## Who it's for

- Traders and quant researchers comparing prices across venues: spread mode gives the matched pairs, the gap and the executable edge, ready to schedule.
- Weather-market traders who need every temperature bracket for a city with orderbook depth, in one call, reliably.
- Analysts and journalists tracking probability moves on elections, Fed decisions, earnings, sports and crypto.
- AI agents and alerting workflows that want a scheduled "what changed" feed instead of re-reading the whole board.

## Input examples

**Top 100 markets by 24h volume, both venues (default):**

```json
{ "maxItems": 100 }
```

**NYC and Chicago weather brackets with orderbook and last 5 trades:**

```json
{
  "weatherPreset": true,
  "cities": ["NYC", "Chicago"],
  "includeOrderbook": true,
  "orderbookDepth": 10,
  "includeRecentTrades": true,
  "tradesLimit": 5
}
```

**Everything mentioning the Fed or interest rates:**

```json
{ "searchQueries": ["Fed", "interest rate"], "maxItems": 50 }
```

**A specific Kalshi series and a specific Polymarket event:**

```json
{
  "kalshiSeriesTickers": ["KXHIGHNY"],
  "polymarketEventSlugs": ["presidential-election-winner-2028"]
}
```

**Settled weather markets from the last 30 days (with results):**

```json
{ "status": "settled", "weatherPreset": true, "settledLookbackDays": 30 }
```

**Monitor mode: schedule every 15 minutes, only report moves of 3+ points:**

```json
{
  "polymarketTags": ["politics"],
  "kalshiCategories": ["Politics", "Elections"],
  "changesOnly": true,
  "minPriceMovePts": 3,
  "monitorStoreName": "politics-watch"
}
```

The first monitor run outputs every market (all are "new") and seeds the store. Later runs output only changes. Use a different `monitorStoreName` per watchlist.

## Output

One row per market. Prices are fractions of a dollar (0 to 1); `implied_probability` is the same number and can be read as P(yes).

| Field | Description |
|---|---|
| `source` | `kalshi` or `polymarket` |
| `id` | Kalshi market ticker or Polymarket condition ID |
| `url` | Link to the market's series page (Kalshi) or the market page (Polymarket) |
| `event_url` | Kalshi only: link to the event page holding all brackets of this market. Built from the tickers; kalshi.com blocks automated link checks, so report a dead link in Issues |
| `title`, `outcome_label` | Market question and the specific outcome (e.g. temperature bracket) |
| `event_id`, `event_title`, `series_id`, `series_title` | Grouping above the market |
| `category`, `tags` | Exchange category and tags |
| `status` | `open`, `closed` (no longer trading, not yet resolved) or `settled` |
| `market_type` | Kalshi market type, or `binary` / `multi` for Polymarket |
| `yes_bid`, `yes_ask`, `spread`, `last_price` | Top of book and last trade |
| `yes_price`, `no_price`, `implied_probability` | Mid price (or last price when no book) |
| `volume`, `volume_24h`, `open_interest`, `liquidity` | Activity. Kalshi volume is contracts; Polymarket volume is USD |
| `open_time`, `close_time`, `expiration_time`, `settled_time` | ISO 8601 timestamps |
| `result`, `settlement_value` | `yes` / `no` (or scalar) once settled |
| `strike_type`, `floor_strike`, `cap_strike`, `settlement_station` | Kalshi bracket definition and the weather station used for settlement |
| `rules` | Settlement rules text (Kalshi) or market description (Polymarket) |
| `price_change_24h_pts` | Polymarket 24h move in probability points |
| `outcomes`, `outcome_prices`, `yes_token_id` | Polymarket outcome list, prices and CLOB token for the YES side |
| `orderbook` | With `includeOrderbook`: `{ "bids": [{"price", "size"}, ...], "asks": [{"price", "size"}, ...] }` on the YES side, best price first |
| `recent_trades` | With `includeRecentTrades`: list of `{ "time", "yes_price", "size", "taker_side" }`, newest first |
| `is_new`, `previous_yes_price`, `previous_seen_at`, `price_move_pts` | Monitor mode only (spread mode: `previous_spread_pts`, `spread_move_pts`) |
| `spread_pts`, `arb_edge_pts`, `arb_direction`, `kalshi_fee_est_pts`, `net_edge_pts`, `match_score`, `match_method`, `kalshi_*`, `polymarket_*` | Spread mode only; see the spread section |
| `enrichment_error` | Set if an orderbook/trades call failed; the base row is still returned |
| `raw` | With `includeRaw`: the untouched upstream object |
| `fetched_at` | When the row was fetched (UTC) |

Export as JSON, CSV, Excel or via the API like any Apify dataset.

## Pricing

Pay per result. You are charged only for rows written to the dataset.

| Event | Price | When |
|---|---|---|
| Market record | $1.50 per 1,000 rows | Standard run |
| Enriched market record | $4.00 per 1,000 rows | Run with `includeOrderbook` and/or `includeRecentTrades` |
| Cross-venue spread record | $5.00 per 1,000 rows | `mode: spread`; one row per matched Kalshi/Polymarket pair |

Examples: a top-500 board costs $0.75. A monitor run that finds 12 moved markets costs $0.018. All NYC + Chicago weather brackets with orderbooks costs about $0.16.

Rows are billed by what they contain: a row whose orderbook/trades call failed (it carries `enrichment_error`) is billed at the base rate, never the enriched rate.

The first run in monitor mode charges every row, because every market is new to the store. Later runs charge only the rows that changed.

Set **Maximum total charge** on the run to cap spend; the actor stops cleanly at the cap.

## Limits and notes

- Kalshi exposes tens of thousands of markets. A keyword search with no category, series or ticker filter scans the whole board (roughly 100+ requests, 20 to 30 seconds). Use `kalshiCategories`, `kalshiSeriesTickers`, or the weather preset to make runs fast.
- `settledLookbackDays` is capped at 365. Kalshi settlement history is fetched per series; Polymarket via closed markets.
- Volume units differ by venue: Kalshi `volume` and `volume_24h` are contracts, Polymarket's are USD. Sorting both venues by volume in one run compares different units; filter by `source` first if that matters.
- `kalshiCategories` values are the exact strings Kalshi uses (dropdown in the UI): Politics, Elections, Economics, Financials, Companies, Crypto, Commodities, Climate and Weather, Science and Technology, Health, World, Sports, Entertainment, Mentions, Social, Transportation, Exotics.
- Polymarket `open_interest` is not published by the API and is always null.
- Kalshi `result` is empty until settlement; `settlement_value` holds the observed value for scalar (temperature, index) markets.
- Enrichment makes one or two extra API calls per market. With 500+ enriched markets, expect a minute or two.
- The `url` for Kalshi points at the series page, which lists all its markets.
- Monitor state is per `monitorStoreName`. Do not run two monitor runs against the same store at the same time.
- Data is provided as-is from the exchanges' public APIs. Not financial advice.

## Use from an AI agent (MCP)

Every Apify actor is available as a tool through the [Apify MCP server](https://mcp.apify.com). Add it to Claude, Cursor, or any MCP client and call this actor by name with the JSON input above. Suggested agent pattern: run once with `changesOnly: true` on a schedule, then act only on the rows returned.

## Support

Open an issue on the actor page with the run ID and input. Both exchanges' APIs are versioned; if a field disappears upstream, the actor keeps the row and nulls the field rather than failing the run.
