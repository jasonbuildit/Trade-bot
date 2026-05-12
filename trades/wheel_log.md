# Wheel Strategy Log

Append-only action log for the automated wheel strategy.
Account: PA38PL3UVB0D (Alpaca paper)

---

## 2026-05-08 — Stage 1 Entry
Symbol: AAPL | Sold put: AAPL260605P00260000 | Strike: $260 | Expiry: 2026-06-05 | Premium: $0.91 | Cash reserved: $26,000
Order ID: 7bfafdee-25d3-45c4-84c0-512474fb2b68 | Status: pending fill (limit $0.91, day order)
Context: VIX 17.32, no earnings risk (Q2 reported May 1), WWDC Jun 8 after expiry. Best yield pick from screener (0.365% / 28 DTE).

## 2026-05-12 — Stage 1 Entry (Cycle #1)
Symbol: AAPL | Sold put: AAPL260612P00285000 | Strike: $285 | Expiry: 2026-06-12 (31 DTE) | Limit: $4.30 (bid $4.13 / ask $4.47) | Cash reserved: $28,500
Order ID: 9b284731-5788-478c-adea-e35afa14d297 | Status: pending fill (day order, expires 4 PM ET today)
Breakeven: $280.70 | Max risk: $28,070 | Delta: -0.299 | IV: 25.4%
Context: AAPL @ $294.38, SMA21 $273.25 (uptrend). Strike 3% OTM. Config: DEFAULT_MAX_POSITION_PCT raised 10%→30%, MAX_SECTOR_EXPOSURE raised 25%→30% for paper account sizing.

## 2026-05-09 — Order Expired (No Fill)
Symbol: AAPL | Order AAPL260605P00260000 expired at EOD 2026-05-08 — never filled (limit $0.91 day order).
state.json cleared. Ready for fresh screener run on Monday 2026-05-12 market open.
