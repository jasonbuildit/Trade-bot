# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Trade-bot is an automated options-income system running a **flywheel wheel strategy** (cash-secured puts → covered calls) on an Alpaca paper account (`PA38PL3UVB0D`). Claude Code is the orchestrator: it calls Alpaca MCP tools directly, feeds the results into the Python modules in `wheel/`, and places orders. There is no long-running process — Claude acts as the runtime.

The full strategy specification lives in `flywheel_options_strategy.md`. The shorter quick-reference rules are in `flywheel.md`. When they differ, `flywheel_options_strategy.md` is authoritative.

## Testing and Diagnostics

All commands run from `C:\workspace\Trade-bot\wheel\` so that relative imports resolve.

```powershell
# Unit tests (all)
cd C:\workspace\Trade-bot\wheel
python -m pytest tests/ -v

# Single test file
python -m pytest tests/test_monitor.py -v

# Single test
python -m pytest tests/test_screener.py::TestDeltaFilter::test_delta_zero_blocked -v

# Smoke tests — full monitor.run() path scenarios with dry_run=True
python smoke_test.py

# State integrity check — run before market open or after any manual state edit
python check_state.py
```

`conftest.py` inserts `wheel/` and `wheel/tests/` into `sys.path`. `tests/fixtures.py` provides shared test data (tickers, OCC symbols, position/account/clock dicts, `make_state()`).

## Module Architecture

```
wheel/
  config.py            — all strategy constants (single source of truth)
  occ.py               — OCC symbol utilities: parse_strike, parse_expiry, find_expiry, expiry_tag
  screener.py          — ranks watchlist puts by annualized yield; returns best candidate
  volatility.py        — HV/IV rank computation; builds volatility_cache.json for entry gating
  put_seller.py        — Tier 1: selects contract, enforces capital guards, emits _mcp_call
  put_spread_seller.py — Tier 2: put credit spread entry; defined-risk CSP alternative
  iron_condor.py       — Tier 3: iron condor entry for ETF-eligible symbols
  call_seller.py       — Stage 2 (wheel): covered call above effective basis, emits _mcp_call
  roller.py            — roll_put_down_and_out / roll_call_up_and_out; returns mleg _mcp_call
  monitor.py           — orchestrator: profit closes, loss limits, assignments, rolls, daily summary
  spread_monitor.py    — Tier 2: monitors open put spreads (profit/loss/expiry/gamma)
  condor_monitor.py    — Tier 3: monitors open condors (profit/loss/expiry/untested-side roll)
  check_state.py       — validates state.json integrity (no live calls needed)
  smoke_test.py        — 16 end-to-end scenarios using mocked I/O
```

Data flow: Claude fetches all MCP data → passes to `monitor.run()` → monitor calls put_seller/call_seller/roller as needed → returns `actions` list → Claude dispatches each `_mcp_call`.

## Alpaca MCP Tools

All brokerage interaction uses the `mcp__alpaca__*` tool namespace — no HTTP client needed.

| Group | Key tools |
|---|---|
| Account | `get_account_info`, `get_account_config`, `get_portfolio_history`, `get_account_activities` |
| Orders | `place_stock_order`, `place_crypto_order`, `place_option_order`, `get_orders`, `cancel_order_by_id` |
| Positions | `get_all_positions`, `get_open_position`, `close_position`, `close_all_positions` |
| Market data — stocks | `get_stock_bars`, `get_stock_quotes`, `get_stock_snapshot`, `get_stock_latest_trade` |
| Market data — options | `get_option_chain`, `get_option_contract`, `get_option_snapshot` |
| Screeners | `get_most_active_stocks`, `get_market_movers` |
| Watchlists | `create_watchlist`, `get_watchlists`, `add_asset_to_watchlist_by_id` |
| Calendar/clock | `get_clock`, `get_calendar` |

Always call `get_clock` before any order. If `is_open: false`, exit immediately — the monitor enforces this too.

## Python Environment

- Language: Python 3.12+
- Shared `.venv` at workspace root (`C:\workspace\.venv`)
- The `wheel/` modules are run **from the `wheel/` directory** so that relative imports (`from config import ...`) resolve correctly.

In practice, Claude calls the MCP tools to collect all data, then passes the result dicts directly into `monitor.run(...)` or `screener.run(...)` — the scripts are not run as CLI subprocesses during normal operation.

## Wheel Automation Flow

```
State: CASH
  → put_seller.run()       → Stage 1: short put open
  → monitor detects 50%/75% profit → close, re-sell put
  → monitor detects 200% loss      → close, manual review
  → monitor detects assignment     → call_seller.run() → Stage 2
  → monitor detects 50%/75% profit → close call, re-sell call
  → monitor detects shares called away → back to Stage 1
```

All state transitions are persisted to `wheel/state.json`. All trades are appended to `trades/wheel_log.md`.

### How Claude orchestrates a monitor cycle

1. Call `get_clock` — exit if closed.
2. Call `get_account_info`, `get_all_positions`, `get_orders(status="open")`.
3. For each symbol in `wheel/watchlist.json` (enabled only): call `get_stock_snapshot` and `get_option_chain`.
4. Call `get_calendar` for trading day list.
5. Pass all results to `monitor.run(clock, positions, open_orders, account, option_chains, trading_days, snapshots)`.
6. Execute any `_mcp_call` entries in the returned `actions` list.

### How Claude orchestrates the screener

1. Collect `get_stock_snapshot` for each enabled symbol.
2. Collect `get_option_chain` (puts) for each enabled symbol.
3. Collect `get_stock_bars` (21 daily bars) for SMA-21 trend filter.
4. Call `screener.run(snapshots, option_chains, bars)`.

## Key Configuration (`wheel/config.py`)

All strategy constants — delta range, DTE window, profit/loss thresholds, drawdown levels, position caps, roll thresholds — live in `config.py`. Change values there; never hard-code them in other modules.

| Constant | Value | Meaning |
|---|---|---|
| `DELTA_MIN / DELTA_MAX` | 0.16 / 0.35 | Put delta acceptance range |
| `DTE_MIN / DTE_MAX` | 21 / 45 | Option expiry window |
| `PROFIT_CLOSE_PCT` | 0.50 | Close at 50% of premium received |
| `FAST_PROFIT_CLOSE_PCT` | 0.75 | Close early at 75% profit |
| `LOSS_LIMIT_PCT` | 2.00 | Close at 200% loss (no auto-resell) |
| `DRAWDOWN_PAUSE/REDUCE/DISABLE` | 5% / 8% / 12% | Portfolio drawdown gates |
| `MAX_BP_COMMITTED` | 0.50 | Max 50% of portfolio in open puts |
| `MIN_CASH_RESERVE_PCT` | 0.25 | Always keep 25% cash |
| `DEFAULT_MAX_POSITION_PCT` | 0.30 | Per-symbol assignment cap (overridable in watchlist) |
| `MAX_SECTOR_EXPOSURE` | 0.30 | Max 30% of portfolio in puts on correlated names |
| `ROLL_PUT_THRESH` | 0.03 | Roll put when stock within 3% of strike |
| `ROLL_CALL_THRESH` | 0.05 | Roll call when stock 5%+ above call strike |
| `MARKET_OPEN_BUFFER_MIN` | 15 | Skip first 15 min after open |
| `MARKET_CLOSE_BUFFER_MIN` | 15 | Skip last 15 min before close |
| `ORDER_ADJUSTMENT_MAX` | 3 | Max GTC limit-price adjustments per entry order |

## State and Log Files

| File | Purpose |
|---|---|
| `wheel/state.json` | Per-symbol wheel stage, option contract, cost basis, premiums, cycle metadata |
| `wheel/watchlist.json` | Symbols eligible for the wheel — `enabled`, `sector`, `earnings_date`, `ex_dividend_date`, `max_position_pct` override |
| `trades/wheel_log.md` | Append-only strategy action log |
| `trades/trades_log.md` | Portfolio log (replicated from Fidelity export) |

`watchlist.json` `sector` field drives the sector exposure cap (`MAX_SECTOR_EXPOSURE`). Per-symbol `max_position_pct` overrides `DEFAULT_MAX_POSITION_PCT` (e.g. QQQ uses 0.07 due to its high strike price).

`state.json` schema per symbol:
```json
{
  "stage": 1,
  "option_symbol": "AAPL260612P00285000",
  "premium_collected": 4.30,
  "fill_price": 4.30,
  "entry_price": 294.38,
  "cost_basis": null,
  "shares_qty": 0,
  "cycle_start": "2026-05-12",
  "expiry_date": "2026-06-12",
  "breakeven": 280.70,
  "max_risk": 28070.0,
  "total_premium_all_cycles": 4.30,
  "roll_count": 0,
  "cycle_number": 1,
  "order_status": "pending_fill",
  "adjustment_count": 0,
  "alpaca_order_id": "..."
}
```

`order_status` values: `"pending_fill"` (GTC order open) → `"filled"` (confirmed in positions). Monitor's `_reconcile_pending_fills` drives this transition; `_adjust_pending_entries` steps the limit price toward bid each cycle until `adjustment_count` reaches `ORDER_ADJUSTMENT_MAX`, then cancels.

## OCC Contract Symbol Parsing

The code derives strike and expiry directly from OCC symbols — no chain lookup needed:

```python
strike = int(contract[-8:]) / 1000        # last 8 digits = strike × 1000
exp    = "20" + contract[len(symbol):len(symbol)+6]  # YYMMDD after ticker
```

All OCC utilities live in `occ.py`: `parse_strike`, `parse_expiry`, `expiry_tag`, `find_expiry`.

## Order Execution Pattern

Python modules never call MCP tools directly — they return an order descriptor dict tagged with `_mcp_call`. Claude reads this and executes the actual tool call:

```python
# Module returns:
{"_mcp_call": "place_option_order", "symbol": contract, "side": "sell",
 "qty": "1", "position_intent": "sell_to_open", "type": "limit", "limit_price": "0.91"}

# Claude executes:
mcp__alpaca__place_option_order(symbol=contract, side="sell", qty="1",
    position_intent="sell_to_open", type="limit", limit_price="0.91")
```

Multi-leg rolls use `order_class="mleg"` with a `legs` list. The `limit_price` is negative for credits.

Full `_mcp_call` dispatch table:

| `_mcp_call` value | Claude executes |
|---|---|
| `place_option_order` | `mcp__alpaca__place_option_order(symbol, side, qty, position_intent, type, limit_price, time_in_force)` |
| `replace_order_by_id` | `mcp__alpaca__replace_order_by_id(order_id, limit_price)` |
| `cancel_order_by_id` | `mcp__alpaca__cancel_order_by_id(order_id)` |
| `create_gmail_draft` | `mcp__claude_ai_Gmail__create_draft(to, subject, body)` |

## Scheduled Skills

| Skill / Command | Trigger | Action |
|---|---|---|
| `/schedule` agent | Every 15 min during market hours | Full monitor cycle |
| `/premarket` | Pre-market (manual or scheduled) | Research brief; creates Gmail draft to `jasonbuildit@gmail.com` |

## Critical Safety Rules (enforced in code — do not bypass)

1. **Cash guard** — `non_marginable_buying_power >= strike × 100` before every put sale.
2. **Call strike guard** — call strike must exceed `cost_basis - premiums_collected` (effective basis).
3. **50% profit rule** — close short option when `unrealized_pl >= 50%` of premium received.
4. **200% loss rule** — close immediately; do **not** auto-resell; flag for manual review.
5. **Drawdown gates** — at 5% drawdown: pause new entries. At 12%: disable strategy entirely.
6. **Market hours guard** — `get_clock().is_open == false` → exit, no orders.
7. **Earnings gate** — skip any expiry with earnings before expiration date.
8. **No debit rolls** — rolls must produce net credit ≥ $0.05.
9. **Position cap** — single symbol assignment value ≤ 30% of portfolio (overridable per symbol in watchlist).
10. **Sector cap** — no more than 30% of portfolio in puts on same-sector names.
