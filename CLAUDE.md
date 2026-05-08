# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Trade-bot is an automated trading bot project. The environment includes an **Alpaca MCP server** with tools for interacting with the Alpaca brokerage API directly from Claude Code — no separate HTTP client needed for market data or order management.

## Alpaca MCP Tools

The following capability groups are available via the `mcp__alpaca__*` tool namespace:

| Group | Example tools |
|---|---|
| Account | `get_account_info`, `get_account_config`, `get_portfolio_history`, `get_account_activities` |
| Orders | `place_stock_order`, `place_crypto_order`, `place_option_order`, `get_orders`, `cancel_order_by_id` |
| Positions | `get_all_positions`, `get_open_position`, `close_position`, `close_all_positions` |
| Market data — stocks | `get_stock_bars`, `get_stock_quotes`, `get_stock_snapshot`, `get_stock_latest_trade` |
| Market data — crypto | `get_crypto_bars`, `get_crypto_quotes`, `get_crypto_snapshot` |
| Market data — options | `get_option_chain`, `get_option_contract`, `get_option_snapshot` |
| Screeners | `get_most_active_stocks`, `get_market_movers` |
| Watchlists | `create_watchlist`, `get_watchlists`, `add_asset_to_watchlist_by_id` |
| Calendar/clock | `get_clock`, `get_calendar` |

Use `get_clock` to check whether the market is currently open before placing orders.

## Conventions

- **Language:** Python 3.12+ (shared `.venv` at workspace root)
- **Secrets:** Alpaca API key/secret in `.env` — never commit
- **State:** `wheel/state.json` tracks per-symbol wheel stage and premium totals
- **Logs:** `trades/wheel_log.md` is append-only; `trades/trades_log.md` is the portfolio log

## Wheel Strategy

The wheel strategy is defined in `flywheel.md`. The automation lives in `wheel/`:

| File | Role |
|---|---|
| `wheel/watchlist.json` | Symbols eligible for the wheel |
| `wheel/state.json` | Per-symbol stage, option contract, premium tracking |
| `wheel/screener.py` | Rank candidates by put premium yield |
| `wheel/put_seller.py` | Stage 1: sell cash-secured put |
| `wheel/call_seller.py` | Stage 2: sell covered call after assignment |
| `wheel/monitor.py` | 15-min loop: 50% rule, assignment detection, daily summary |

### Running the monitor (via Claude)

The monitor is invoked by Claude Code's `/schedule` agent every 15 minutes during market hours. To run manually, ask Claude:

> "Run the wheel monitor: check positions, apply rules, update state."

Claude will call the Alpaca MCP tools, pass results to `monitor.py`, and place any orders.

### Key rules enforced in code

- Cash guard before every put sale (`non_marginable_buying_power >= strike × 100`)
- Call strike must exceed effective cost basis (entry price minus premiums collected)
- 50% profit rule: close option early when unrealized P/L ≥ 50% of premium received
- Market-hours guard: monitor exits immediately if `get_clock()` returns `is_open: false`
