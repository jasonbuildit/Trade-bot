# Pre-Market Research — Wheel Strategy Daily Brief

Run deep pre-market research to inform today's wheel strategy decisions.
Use WebSearch and WebFetch for all data. Output a structured brief.

---

## Step 1 — Market Open Check

Search: "US stock market open today $CURRENT_DATE holidays"
- Is the NYSE open today?
- If closed (holiday), stop and note: "Market closed today — no action needed."

---

## Step 2 — Watchlist News (Wheel Candidates)

For each symbol: **AAPL, KO, QQQ, ECC, APLE**
Search: "[SYMBOL] stock news premarket today"

Flag anything that could affect wheel decisions:
- Earnings reports or guidance
- Analyst upgrades/downgrades
- Dividend changes (critical for KO, ECC, APLE)
- Product launches, legal events, macro sector moves
- Large premarket price moves (>2%)

---

## Step 3 — Active Positions

Search news for: **GDX, gold miners, gold price**
- Is gold/GDX trending up or down premarket?
- Any major catalyst (Fed speakers, geopolitical, dollar moves)?
- This matters for the open GDX $95/$100 bull call spread

---

## Step 4 — Earnings Calendar

Search: "earnings reports today [DATE]" and "earnings before market open [DATE]"
- List all S&P 500 companies reporting today
- Flag any that could move QQQ significantly (mega-cap tech, etc.)
- Flag any direct watchlist names

---

## Step 5 — Economic Calendar

Search: "economic calendar today [DATE] US"
Key events to flag:
- Fed speakers or FOMC minutes
- CPI / PPI / PCE inflation data
- Jobs data (NFP, jobless claims)
- GDP releases
- Any event rated "high impact"

---

## Step 6 — Pre-Market Sentiment

Search: "premarket futures today [DATE]" and "S&P 500 futures premarket"
- Are futures up or down? By how much?
- VIX level — elevated (>20) means caution on selling new puts
- Any overnight news driving big moves

---

## Step 7 — Wheel Strategy Recommendation

Based on all research above, produce a clear recommendation:

**PROCEED** — Normal day, sell puts as planned on best screener pick
**CAUTION** — Notable risk (earnings nearby, high VIX, big macro event) — tighten strikes or reduce size
**HOLD** — Do not open new positions today (market holiday, extreme volatility, major earnings for watchlist stock)

---

## Output Format

Produce a concise daily brief in this exact format:

```
═══════════════════════════════════════════════
WHEEL STRATEGY — PRE-MARKET BRIEF [DATE]
═══════════════════════════════════════════════

MARKET STATUS: Open / Closed (reason)
FUTURES: S&P [+/-X%] | QQQ [+/-X%] | VIX [X]
RECOMMENDATION: PROCEED / CAUTION / HOLD

── WATCHLIST ──────────────────────────────────
AAPL:  [news summary, premarket move if any]
KO:    [news summary]
QQQ:   [news summary]
ECC:   [news summary]
APLE:  [news summary]

── ACTIVE POSITIONS ───────────────────────────
GDX:   [gold/miner news, spread impact]

── EARNINGS TODAY ─────────────────────────────
[List of notable earnings, flag if affects QQQ]

── KEY EVENTS ─────────────────────────────────
[Economic releases, Fed, etc.]

── WHEEL ACTION ───────────────────────────────
Today's plan: [specific action or "hold"]
Reason: [1-2 sentences]
═══════════════════════════════════════════════
```

If invoked manually during the session, print the brief directly.
If invoked via scheduled agent, also create a Gmail draft to jasonbuildit@gmail.com
with subject "🔔 Wheel Brief [DATE]" containing the full brief.
