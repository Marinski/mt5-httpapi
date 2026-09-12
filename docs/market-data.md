# Market data API — get the fucking bars

Find symbols, inspect the contract details that brokers love making weird, pull ticks or OHLCV bars, and bolt on server-side TA in the same request.

## Contents

- [Symbols](#symbols)
- [OHLCV rates](#ohlcv-rates)
- [Rates with technical analysis](#rates-with-technical-analysis)
- [Tick history](#tick-history)
- [REST routing and authentication](rest-api.md#api)
- [Technical-analysis workflows](clients-and-examples.md#technical-analysis)

## Symbols

| Method | Endpoint                 | Description                               |
| ------ | ------------------------ | ----------------------------------------- |
| GET    | `/symbols`               | List symbols (`?group=*USD*`). **Refused with 409 on a `mode: backtest` terminal** — it never attaches the SDK; see below. |
| POST   | `/symbols/import`        | Prime the symbol cache from a JSON body without touching the SDK — the safe path on a `mode: backtest` terminal. |
| GET    | `/symbols/:symbol`       | Symbol details                            |
| GET    | `/symbols/:symbol/tick`  | Latest tick                               |
| GET    | `/symbols/:symbol/rates` | OHLCV candles (`?timeframe=H1&count=100`, `?timeframe=H1&from=<unix>&count=-100`, or `?timeframe=H1&from=<unix>&to=<unix>`) |
| POST   | `/symbols/:symbol/rates/ta` | Same query params as `/rates`; JSON body `{indicators: {...}, recentBars?: N}`. Returns bars + wickworks TA analysis. |
| GET    | `/symbols/:symbol/ticks` | Tick data (`?count=100`, `?from=<unix>&count=-100`, or `?from=<unix>&to=<unix>`)                                            |

**GET `/symbols`** — array of symbol names:

```json
["EURUSD", "GBPUSD", "ADAUSD", "BTCUSD", "..."]
```

On success (unfiltered, i.e. no `?group=`) this also persists the list to that
terminal's symbol cache, which the backtest INI builder reads to decide
whether a broker's `symbol_suffix` applies (see [Backtesting](backtesting.md)).
`GET /symbols` calls the MT5 SDK, so on a `mode: backtest` terminal — which
never attaches the SDK — it is refused with 409 instead of triggering a full
`mt5.initialize()` that would spawn `terminal64.exe` and hold the tester's
single-instance lock, leaving the next backtest run with an empty report.

**POST `/symbols/import`** — prime a terminal's symbol cache directly, with no
MT5 SDK call at all. This is the only supported way to prime a `mode: backtest`
terminal: get the list from a live terminal on the same broker/account (`GET
/symbols` there) or from the broker's own documentation, then post it here.

```bash
curl -X POST -H "Authorization: Bearer $MT5_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"symbols": ["EURUSD", "GBPUSD", "XAUUSD"]}' \
  "$MT5_API_URL/symbols/import"
```

```json
{"imported": 3}
```

The body is bounded on three axes, each configurable at the top level of
`config/config.yaml` or under the same name uppercased in the environment:

| Setting | Default | Enforced |
| ------- | ------- | -------- |
| `symbol_import_max_body_bytes` | `2097152` (2 MiB) | `413` from the declared `Content-Length`, before the body is parsed. A request with no `Content-Length` at all (chunked) is refused with `411`. |
| `symbol_import_max_symbols` | `20000` | `400` — entries in `symbols`, counted before deduplication. |
| `symbol_import_max_symbol_length` | `64` | `400` — characters per symbol, measured on the whitespace-stripped name. |

The defaults sit far above any real broker book — the widest seen in this fleet
is 841 symbols, and MT5 itself caps a symbol name at 31 characters — so they
only fire on input that was never going to be a usable symbol list. Nothing is
written to the cache when a request is refused.

**GET `/symbols/:symbol`** — full symbol info:

```json
{
    "name": "EURUSD",
    "description": "Euro vs US Dollar",
    "path": "Markets\\Forex\\Major\\EURUSD",
    "currency_base": "EUR",
    "currency_profit": "USD",
    "currency_margin": "EUR",
    "digits": 5,
    "point": 1e-05,
    "spread": 30,
    "spread_float": true,
    "trade_contract_size": 100000.0,
    "trade_tick_size": 1e-05,
    "trade_tick_value": 1.0,
    "trade_tick_value_profit": 1.0,
    "trade_tick_value_loss": 1.0,
    "volume_min": 0.01,
    "volume_max": 100.0,
    "volume_step": 0.01,
    "volume_limit": 0.0,
    "trade_mode": 4,
    "trade_calc_mode": 0,
    "trade_exemode": 2,
    "trade_stops_level": 1,
    "trade_freeze_level": 0,
    "swap_long": -11.0,
    "swap_short": 1.14064,
    "swap_mode": 1,
    "swap_rollover3days": 3,
    "margin_initial": 0.0,
    "margin_maintenance": 0.0,
    "margin_hedged": 50000.0,
    "filling_mode": 3,
    "expiration_mode": 15,
    "order_gtc_mode": 0,
    "order_mode": 127,
    "bid": 1.18672,
    "ask": 1.18702,
    "bidhigh": 1.18845,
    "bidlow": 1.1847,
    "askhigh": 1.1885,
    "asklow": 1.18475,
    "last": 0.0,
    "time": 1771027139,
    "select": true,
    "visible": true,
    "custom": false,
    "session_deals": 0,
    "session_buy_orders": 0,
    "session_sell_orders": 0,
    "session_open": 1.1869,
    "session_close": 1.18698,
    "price_change": -0.0219,
    "bank": "",
    "basis": "",
    "category": "",
    "exchange": "",
    "isin": "",
    "..."
}
```

There's a shitload of fields — these are the ones you'll actually use:

| Field                                     | What it is                                 |
| ----------------------------------------- | ------------------------------------------ |
| `bid`, `ask`                              | Current prices                             |
| `digits`                                  | Price decimal places                       |
| `point`                                   | Smallest price change                      |
| `trade_tick_size`                         | Minimum price movement                     |
| `trade_tick_value`                        | Profit/loss per tick per 1 lot             |
| `trade_contract_size`                     | Contract size (100000 for forex)           |
| `volume_min`, `volume_max`, `volume_step` | Lot size constraints                       |
| `spread`                                  | Current spread in points                   |
| `swap_long`, `swap_short`                 | Overnight swap rates                       |
| `trade_stops_level`                       | Min distance for SL/TP from price (points) |

**GET `/symbols/:symbol/tick`**:

```json
{
  "time": 1771150549,
  "bid": 0.3001,
  "ask": 0.3004,
  "last": 0.0,
  "volume": 0,
  "time_msc": 1771150549145,
  "flags": 1030,
  "volume_real": 0.0
}
```

### OHLCV rates

**GET `/symbols/:symbol/rates`** returns an array of OHLCV candles:

Timeframes: `M1` `M2` `M3` `M4` `M5` `M6` `M10` `M12` `M15` `M20` `M30` `H1` `H2` `H3` `H4` `H6` `H8` `H12` `D1` `W1` `MN1`

```json
{
  "time": 1771128000,
  "open": 0.2962,
  "high": 0.3006,
  "low": 0.2922,
  "close": 0.2979,
  "tick_volume": 4755,
  "spread": 30,
  "real_volume": 0
}
```

`time` is the candle open time, unix epoch seconds.

Query params (rates):

| Param | Behavior |
| --- | --- |
| `timeframe` | Defaults `M1` |
| `count` | Signed integer (default `100`). Positive = N forward from `from` (or last N if no `from`). Negative = `\|N\|` ending at `from`. Zero = empty result. Mutually exclusive with `to`. |
| `from` | Anchor (real UTC). Omitted = now. Accepts unix seconds, `YYYY_MM_DD_HH_MM_SS`, or `YYYY_MM_DD` (midnight UTC). |
| `to` | Range end (real UTC, same formats as `from`). Requires `from`. Returns all bars in `[from, to]`, no count cap beyond `terminal_info().maxbars`. Mutually exclusive with `count`. |

Examples:
- `?timeframe=H1&count=100` — last 100 H1 candles up to current bar
- `?timeframe=H1&from=1700000000&count=100` — 100 candles forward from anchor
- `?timeframe=H1&from=2024_01_15&count=-100` — 100 candles ending at midnight UTC on 2024-01-15
- `?timeframe=H1&from=2024_01_15_09_30_00&to=2024_01_15_16_00_00` — every H1 candle in the window

Use `count` when you want exactly N bars and do not care about the precise end time. Use `to` when you have a real window and want the whole damn thing. `count` adds weekend/holiday padding internally; `to` passes the range straight to `copy_rates_range`.

**MaxBars cap:** MT5 returns at most `terminal_info().maxbars` rows per request (default 100,000 — visible at `GET /terminal`). For long backfills (e.g. M1 over a year ≈ 525k bars) chunk the time range client-side and stitch the results.

Symbols are auto-selected into MarketWatch on first access — backfilling rarely-traded instruments works without a manual select step.

### Rates with technical analysis

**POST `/symbols/:symbol/rates/ta`** fetches candles exactly like `GET /symbols/:symbol/rates`, throws them at the [wickworks](https://github.com/psyb0t/docker-wickworks) sidecar, and gives you the bars and calculated TA together:

```json
{
  "symbol": "EURUSD",
  "timeframe": "H1",
  "bars": [ { "time": 1771146000, "open": 1.0832, "high": 1.0840, "low": 1.0828, "close": 1.0835, "tick_volume": 1234, "spread": 1, "real_volume": 0 } ],
  "ta": { "indicators": { "rsi": [ ... ], "macd": { ... } }, "...": "wickworks response" }
}
```

JSON body:

| Field | Required | Meaning |
| --- | --- | --- |
| `indicators` | yes | Non-empty object — wickworks indicator spec. Each entry maps an output key to either `true` (run with defaults) or a flat params object (e.g. `{"length": 20, "std": 2}`); add `"type": "<name>"` only when the output key differs from the indicator name (e.g. `{"rsi21": {"type": "rsi", "length": 21}}` to run a second RSI under a custom key). See the [wickworks indicator catalog](https://github.com/psyb0t/docker-wickworks#available-indicators) for the full list of types, params, and output shapes. |
| `recentBars` | no | Accepted for mt5-httpapi compatibility but not sent to Wickworks. To get fewer bars back, lower `count` on the query string or slice client-side. |

The sidecar runs inside the mt5 container's net namespace with no published ports — only the mt5 process (and by extension this API) can reach it. Configure via `wickworks:` in `config.yaml` (defaults to `http://20.20.20.1:8000/`, the dockurr gateway IP seen from inside the Windows VM).

Example:

```bash
curl -X POST -H "Authorization: Bearer $MT5_API_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"indicators":{"rsi":true,"macd":true},"recentBars":50}' \
     "$MT5_API_URL/symbols/EURUSD/rates/ta?timeframe=H1&count=200"
```

### Tick history

**GET `/symbols/:symbol/ticks`** returns an array of ticks:

```json
{
  "time": 1771146325,
  "bid": 0.2973,
  "ask": 0.2976,
  "last": 0.0,
  "volume": 0,
  "time_msc": 1771146325123,
  "flags": 6,
  "volume_real": 0.0
}
```

Query params (ticks): same `count` / `from` / `to` model as rates — positive `count` = forward from `from`, negative = backward, `from+to` = range, `count` and `to` mutually exclusive, `to` requires `from`. Plus:

| Param | Values | Default | Meaning |
| --- | --- | --- | --- |
| `flags` | `ALL`, `INFO`, `TRADE` | `ALL` | `INFO` = bid/ask changes only (~10× smaller payload), `TRADE` = trades only, `ALL` = everything |

Examples:
- `?count=100` — last 100 ticks up to now
- `?from=2024_01_15_14_30_00&count=500` — 500 ticks forward from that timestamp
- `?from=1700000000&count=-500` — 500 ticks ending at anchor
- `?from=2024_01_15_09_00_00&to=2024_01_15_10_00_00` — every tick in that 1-hour window

Tick-density warning: a liquid pair can shit out 10–100 ticks/sec. A one-hour `from+to` window can become millions of rows, so prefer `count` unless you genuinely need every twitch in the window.

Responses are gzip-compressed when the client sends `Accept-Encoding: gzip` — typically a 5–10× bandwidth reduction for large rate/tick fetches. `curl` honors this if you pass `--compressed`.
