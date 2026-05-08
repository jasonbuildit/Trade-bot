# Wheel Strategy

Automated put-selling and covered-call loop on Alpaca paper account PA38PL3UVB0D.

---

## STAGE 1: SELL PUTS

- Sell a cash-secured put on **[STOCK]** with strike ~10% below current price (delta 0.15–0.30).
- Pick an expiration **21–45 DTE** (theta decay sweet spot; Friday preferred).
- Collect the premium.

**If the put expires worthless:** sell another one — keep collecting premium.

**If assigned (shares purchased):** move to Stage 2.

---

## STAGE 2: SELL CALLS

- Once shares are held, sell a covered call with strike ~10% above cost basis.
- Pick an expiration **21–45 DTE**.
- Collect premium.

**If the call expires worthless:** sell another one.

**If shares get called away (sold):** go back to Stage 1 and repeat.

---

## RULES

### Capital Safety
- **Cash guard:** never sell a put without `non_marginable_buying_power >= strike × 100`
- **Position size cap:** no single put may reserve more than **10% of portfolio value** (per-symbol override in `watchlist.json`)
- **Call strike guard:** never sell a call below effective cost basis (entry price minus all premiums collected)

### Risk Management
- **50% profit rule:** if a short option reaches 50% profit before expiration, close it early and sell a new one
- **200% loss limit:** if a short option reaches 2× the premium received as a loss, close immediately — do NOT auto-re-sell; requires manual review

### Entry Filters (enforced in screener)
- **Earnings gate:** never sell a put with earnings before the expiry date
- **Delta cap:** only sell puts with delta between 0.05 and 0.35
- **Bid-ask spread:** skip contracts with spread > 15% of mid (illiquid)
- **IV floor:** flag and skip symbols with ATM IV < 20% (cheap premium environment)
- **Trend filter:** skip symbols trading below 21-day SMA (downtrend)

### Rolling
- **Roll put down-and-out:** if stock falls within 3% of put strike (approaching ITM), roll to same or lower strike at a later expiry for **net credit ≥ $0.05**
- **Roll call up-and-out:** if stock rallies >5% above covered call strike, roll to a higher strike at a later expiry for **net credit ≥ $0.05**
- Never roll for a debit

### Operations
- Check positions every **15 minutes during market hours** (`monitor.py`)
- Do nothing outside market hours (`get_clock()` guard)
- Track total premium collected across all cycles in `state.json`
- Write **daily summary at 15:55 ET** including: stage, premium, P/L, capital at risk, BP utilization

---

## EXECUTION

- All Alpaca interaction via MCP tools (`mcp__alpaca__*`) — no HTTP client needed
- Strategy lives in `wheel/` — orchestrated by Claude Code `/schedule` agent
- State persisted in `wheel/state.json`; log in `trades/wheel_log.md`
- Run screener: ask Claude "run the wheel screener" — it fetches live data and returns best pick
- Run monitor: ask Claude "run the wheel monitor" — checks all positions and acts

---

## FILES

| File | Role |
|---|---|
| `wheel/watchlist.json` | Symbols eligible for wheel (with earnings dates, enabled flag) |
| `wheel/state.json` | Per-symbol stage, option contract, breakeven, max risk, premium tracking |
| `wheel/screener.py` | Rank candidates by annualized put yield; apply all entry filters |
| `wheel/put_seller.py` | Stage 1: find strike, check cash + position size cap, place order |
| `wheel/call_seller.py` | Stage 2: find call strike above effective basis, place order |
| `wheel/roller.py` | Roll puts down/out and calls up/out for credit when triggered |
| `wheel/monitor.py` | 15-min loop: profit rule, loss limit, assignment detection, roll triggers |
