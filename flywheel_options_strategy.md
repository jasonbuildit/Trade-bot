# Flywheel Options Strategy

**Version:** 2026-05-08  
**Mode:** Paper trading strategy specification  
**Primary account target:** Alpaca paper trading  
**Primary instruments:** U.S.-listed stocks, ETFs, and listed equity/ETF options  
**Excluded by default:** Futures, futures options, naked short calls, uncovered short puts, binary-event trades

>The objective is to build a disciplined options-income flywheel with explicit risk controls, not to maximize trade frequency.

---

## 1. Strategy Thesis

The **flywheel options strategy** is an expanded version of the classic wheel strategy:

1. Hold cash.
2. Sell a cash-secured put on a high-quality, liquid underlying.
3. Collect premium.
4. If assigned, take shares.
5. Sell covered calls above adjusted cost basis.
6. If called away, return to cash.
7. Repeat only when the setup still satisfies risk and liquidity rules.

The core idea is to convert available cash and stock ownership into a repeatable premium-collection process while avoiding the common failure modes of premium selling:

- Selling too much notional exposure.
- Selling options on poor-quality or illiquid underlyings.
- Ignoring earnings, binary events, or volatility shocks.
- Rolling losing trades indefinitely without a capital rule.
- Selling calls below economic cost basis.
- Treating paper profit from premium as free money while the underlying position is losing value.

The strategy is intentionally conservative. It is designed to behave more like a **cash-secured income engine** than a speculative options system.

---

## 2. Historical Reference Model

This flywheel is inspired by systematic option-writing approaches that have been used as benchmark strategies:

- **Put-write model:** The Cboe S&P 500 PutWrite Index tracks a collateralized strategy that sells SPX put options while holding Treasury-bill collateral. The benchmark version sells monthly S&P 500 put options and maintains collateral rather than using uncontrolled leverage.
- **Buy-write model:** The Cboe S&P 500 BuyWrite Index tracks a covered-call strategy on the S&P 500, holding the underlying index exposure while writing monthly call options.
- **Practical lesson:** The historically durable version of premium selling is not random high-yield option selling. It is rules-based, collateralized, liquid, and diversified.

### What to borrow from historically successful structures

| Principle | Application to This Flywheel |
|---|---|
| Collateralized short puts | Every short put must be cash-secured. |
| Covered calls only | Every short call must be backed by 100 shares. |
| Monthly or near-month tenor | Prefer 21–45 DTE; avoid ultra-short-term gambling. |
| Liquid underlying | Prefer high-volume stocks/ETFs with tight option spreads. |
| Systematic rules | Use pre-defined entry, exit, roll, and stop rules. |
| Risk-adjusted income | Premium is not profit until underlying exposure is managed. |

---

## 3. Alpaca Compatibility Notes

Alpaca supports paper trading and listed equity/ETF options. This strategy should therefore be implemented against:

- U.S. stocks.
- ETFs.
- Listed U.S. equity and ETF options.

Do **not** assume futures or futures options are available through the same Alpaca workflow. Futures require a different brokerage, margin model, contract specification, risk engine, and market-hours model.

### Alpaca paper-trading assumptions

- Paper mode only.
- No live capital deployment.
- No autonomous live execution.
- Orders should be logged and reviewable.
- Strategy should default to **alert-first / paper-execute-second** until all calculations are validated.

---

## 4. Strategy Objectives

### Primary objective

Generate recurring option premium while preserving capital and avoiding forced liquidation.

### Secondary objectives

- Accumulate shares only at pre-approved prices.
- Sell shares only at or above adjusted cost basis.
- Track premium across full cycles, not isolated trades.
- Measure return on committed capital.
- Reduce assignment surprises.
- Avoid trading during unstable or information-poor conditions.

### Non-objectives

This strategy is **not** designed to:

- Predict short-term direction.
- Chase high implied volatility blindly.
- Sell naked options.
- Trade around earnings surprises.
- Trade futures unless a separate futures-capable broker module is created.
- Maximize daily trade count.

---

## 5. Eligible Underlying Filter

Only trade underlyings that pass all required filters.

### 5.1 Required filters

| Filter | Rule |
|---|---|
| Tradability | Underlying must be active and optionable. |
| Liquidity | Average stock volume should be high enough to avoid slippage. Prefer large-cap stocks and ETFs. |
| Option liquidity | Option chain must have tight bid/ask spreads and real open interest. |
| Spread quality | Avoid contracts with bid/ask spread greater than 5%–10% of option mid-price. |
| Price quality | Avoid very low-priced stocks unless specifically approved. |
| Event risk | No new short premium entry immediately before earnings, FDA decisions, merger votes, litigation rulings, or major binary events. |
| Portfolio exposure | Underlying must not exceed per-symbol risk cap. |
| Thesis quality | Only sell puts on stocks/ETFs the account is willing to own. |

### 5.2 Preferred underlying types

Rank candidates in this order:

1. Broad-market ETFs.
2. Sector ETFs with deep option markets.
3. Mega-cap liquid stocks.
4. High-quality large-cap stocks with strong options liquidity.
5. Everything else is disabled by default.

### 5.3 Avoid list

Avoid short premium on:

- Meme stocks.
- Thinly traded names.
- Stocks with wide option spreads.
- Low-float stocks.
- Earnings week trades.
- Distressed companies.
- Single names with pending binary news.
- Leveraged ETFs unless explicitly allowed.
- Futures and futures options inside this Alpaca strategy.

---

## 6. Capital Allocation Rules

### 6.1 Per-position risk cap

For paper trading, define a notional cap before execution:

```yaml
risk_limits:
  max_account_buying_power_committed: 0.50
  min_cash_reserve: 0.25
  max_single_symbol_assignment_value: 0.10
  max_sector_exposure: 0.25
  max_open_short_puts_per_symbol: 1
  max_open_short_calls_per_symbol: 1
```

Interpretation:

- Do not commit more than 50% of account value to potential assignment.
- Keep at least 25% cash reserve.
- Do not let one assigned stock position exceed 10% of account value unless manually approved.
- Avoid stacking multiple correlated positions.

### 6.2 Cash-secured put requirement

For every short put:

```text
required_cash = strike_price * 100 * number_of_contracts
```

The trade is valid only if:

```text
available_cash_after_trade >= required_cash + min_cash_reserve
```

### 6.3 Covered-call requirement

For every short call:

```text
required_shares = 100 * number_of_contracts
```

The trade is valid only if the account owns at least that many unrestricted shares.

---

## 7. Preferred Option Selection

### 7.1 Put entry targets

| Parameter | Preferred Rule |
|---|---|
| Expiration | 21–45 DTE preferred; 14–60 DTE allowed. |
| Delta | Conservative: -0.16 to -0.25. Balanced: -0.25 to -0.35. Aggressive: -0.35 to -0.45. |
| Strike | Usually 5%–15% below spot, but delta and support matter more than a fixed percentage. |
| Premium | Must provide acceptable return on risk after fees and slippage. |
| Liquidity | Tight spread, meaningful volume/open interest. |
| Events | Avoid earnings and binary events before expiration. |

### 7.2 Call entry targets

| Parameter | Preferred Rule |
|---|---|
| Expiration | 21–45 DTE preferred; 14–60 DTE allowed. |
| Delta | Conservative: 0.16 to 0.25. Balanced: 0.25 to 0.35. Aggressive: 0.35 to 0.45. |
| Strike | Must be above adjusted cost basis unless manual override exists. |
| Premium | Must justify capping upside. |
| Liquidity | Tight spread, meaningful volume/open interest. |
| Dividend risk | Check ex-dividend date before selling calls. |

### 7.3 Entry price discipline

Never sell at market.

Preferred order type:

```text
limit order near mid-price
```

Execution ladder:

1. Start at mid-price.
2. Wait 60–120 seconds.
3. Move slightly toward natural price if still attractive.
4. Cancel if the risk/reward no longer satisfies thresholds.

---

## 8. Stage 0 — Preflight Scan

Before any new position is opened, the system must complete a preflight check.

### 8.1 Market check

```yaml
market_conditions:
  trade_only_regular_market_hours: true
  no_new_entries_first_minutes_after_open: 15
  no_new_entries_last_minutes_before_close: 15
  no_trading_outside_market_hours: true
```

### 8.2 Underlying check

Required:

- Current price available.
- Underlying is tradable.
- Option chain available.
- No imminent earnings event.
- Bid/ask spreads acceptable.
- Volume and open interest acceptable.
- Position cap not exceeded.

### 8.3 Portfolio check

Required:

- Cash available.
- No duplicate short put on same symbol unless approved.
- No uncovered call exposure.
- Max committed capital not exceeded.
- Max correlated exposure not exceeded.
- Existing positions reconciled with broker account.

---

## 9. Stage 1 — Sell Cash-Secured Put

### 9.1 Entry rule

Open a short put only when all are true:

```text
underlying_passes_filter == true
cash_available >= strike * 100 * contracts
expiration_dte between 21 and 45 preferred
put_delta within configured range
bid_ask_spread acceptable
no_binary_event_before_expiration
portfolio_risk_limits_not_exceeded
```

### 9.2 Strike selection hierarchy

Use this hierarchy instead of blindly selecting 10% below spot:

1. Avoid earnings and binary-event expirations.
2. Select 21–45 DTE.
3. Filter for liquid contracts.
4. Select target delta range.
5. Check strike relative to support zones and moving averages.
6. Check assignment value against account risk cap.
7. Select the best return on risk among acceptable candidates.

### 9.3 Position record

For every short put, track:

```yaml
position_record:
  symbol: null
  stage: short_put
  contracts: 1
  put_strike: null
  expiration: null
  entry_credit: null
  current_option_value: null
  max_profit: entry_credit * 100
  assignment_value: put_strike * 100
  breakeven: put_strike - entry_credit
  opened_at: null
  thesis: null
```

### 9.4 Put management rules

| Condition | Action |
|---|---|
| 50% profit reached | Buy to close and look for a new qualified entry. |
| 75%+ profit reached quickly | Close early; do not wait for pennies. |
| Expiration near and OTM | Let expire only if assignment risk is acceptable and liquidity is poor. Otherwise close. |
| Delta rises above 0.50 against position | Review; consider roll, accept assignment, or close. |
| Underlying breaks risk level | Stop opening new puts; evaluate whether to close or accept assignment. |
| Earnings added before expiration | Consider closing before event. |
| Assignment occurs | Move to Stage 2. |

### 9.5 Put roll rules

Rolling is not mandatory. A roll is only valid when it improves the position without increasing risk beyond limits.

A put roll is allowed only when:

```text
new_trade_credit >= 0
new_assignment_value <= allowed_position_cap
new_expiration <= 45 additional DTE preferred
underlying_still_passes_ownership_filter == true
```

Do not roll solely to avoid admitting a loss.

---

## 10. Stage 2 — Assignment and Cost Basis

If assigned, the account buys 100 shares per short put contract.

### 10.1 Economic cost basis

Track two forms of basis:

#### Tax/accounting basis

```text
share_basis = assignment_strike
```

#### Strategy economic basis

```text
economic_basis = assignment_strike - put_premium_received - prior_realized_premiums_allocated_to_cycle
```

Use economic basis for strategy tracking. Use broker/tax basis for tax reporting.

### 10.2 Assignment record

```yaml
assignment_record:
  symbol: null
  shares: 100
  assigned_from_put_strike: null
  put_premium_received: null
  economic_basis: null
  assignment_date: null
  unrealized_stock_pnl: null
  total_cycle_premium: null
```

---

## 11. Stage 3 — Sell Covered Call

### 11.1 Entry rule

Open a short covered call only when all are true:

```text
shares_owned >= 100 * contracts
call_strike > economic_basis
expiration_dte between 21 and 45 preferred
call_delta within configured range
bid_ask_spread acceptable
no_unwanted_dividend_assignment_risk
```

### 11.2 Covered-call strike rule

The call strike should be selected using this hierarchy:

1. Strike must be above economic cost basis.
2. Strike should produce acceptable total return if called away.
3. Strike should fall in target delta range.
4. Contract must have sufficient liquidity.
5. Expiration should preferably be 21–45 DTE.

### 11.3 Covered-call management rules

| Condition | Action |
|---|---|
| 50% profit reached | Buy to close and sell a new qualified covered call. |
| Underlying rallies through call strike | Decide whether to accept assignment or roll up/out for credit. |
| Call strike below basis | Invalid; do not place trade. |
| Ex-dividend risk appears | Review early-assignment risk. |
| Expiration OTM | Let expire or close cheaply. Sell another qualified call. |
| Shares called away | Realize sale, close cycle, return to Stage 1. |

### 11.4 Covered-call roll rules

A covered call may be rolled only when one of these is true:

- The roll increases strike and produces a net credit.
- The roll preserves a profitable called-away outcome.
- The trader explicitly wants to keep shares and the roll does not violate capital rules.

Avoid rolling a covered call down below basis just to collect premium.

---

## 12. Stage 4 — Cycle Close and Reinvestment

A full cycle closes when:

- Put expires worthless.
- Put is closed for profit/loss.
- Assigned shares are called away.
- Shares are manually sold after risk review.

### 12.1 Cycle return calculation

```text
cycle_realized_pnl = put_premiums + call_premiums + stock_sale_pnl - commissions - fees - slippage
```

```text
return_on_committed_capital = cycle_realized_pnl / max_capital_committed
```

```text
annualized_return_estimate = return_on_committed_capital * (365 / days_in_cycle)
```

Annualized return estimates are for comparison only. They should not be treated as expected returns.

---

## 13. Profit-Taking Rules

### 13.1 Default option profit rule

If a short option reaches **50% of max profit**, close it.

Example:

```text
entry_credit = $2.00
max_profit = $200 per contract
50_percent_profit_target = $1.00 option value
close_when_option_mark <= $1.00
```

### 13.2 Why close early

Closing early can:

- Reduce tail risk.
- Free capital.
- Avoid holding short gamma near expiration.
- Reduce assignment surprises.
- Let the strategy redeploy into a cleaner setup.

### 13.3 When not to redeploy immediately

After closing a profitable option, do not automatically open another trade if:

- Market is near close.
- Underlying has earnings soon.
- IV has collapsed.
- Spread quality is poor.
- Portfolio exposure is already high.
- Market is in a sharp selloff or gap-risk regime.

---

## 14. Loss-Control Rules

The wheel can hide losses because premium collection feels positive while the underlying position deteriorates. This strategy requires hard loss-control checks.

### 14.1 Position-level controls

| Trigger | Action |
|---|---|
| Underlying down 8%–12% from put entry | Review ownership thesis; stop adding exposure. |
| Underlying below major support | Do not sell additional puts. |
| Assignment would breach risk cap | Close or roll down/out only if risk improves. |
| Stock position down 15%+ from basis | Covered calls may continue, but no additional put exposure on same symbol. |
| Strategy drawdown limit reached | Disable new entries. |

### 14.2 Portfolio-level controls

```yaml
portfolio_drawdown_controls:
  pause_new_entries_at_drawdown: 0.05
  reduce_position_size_at_drawdown: 0.08
  disable_strategy_at_drawdown: 0.12
```

Interpretation:

- At 5% portfolio drawdown, pause new entries and review.
- At 8% drawdown, reduce position size.
- At 12% drawdown, disable new premium selling until manually reviewed.

---

## 15. Volatility and Regime Filter

Premium selling tends to look attractive when implied volatility is high, but risk is also higher. Use a volatility regime filter.

### 15.1 Preferred setup

Best conditions for this strategy:

- Neutral to moderately bullish market.
- Elevated but not panic-level implied volatility.
- Stable or rising underlying trend.
- Liquid options market.
- No binary catalyst.

### 15.2 Poor setup

Avoid new entries when:

- Market is in disorderly decline.
- Spreads are widening.
- VIX or broad volatility is spiking aggressively.
- The underlying has an imminent event.
- Liquidity disappears.
- The only reason for the trade is unusually high premium.

---

## 16. Automation Logic

### 16.1 State machine

```text
CASH
  -> SHORT_PUT_OPEN
  -> PUT_CLOSED_PROFIT -> CASH
  -> PUT_EXPIRED_WORTHLESS -> CASH
  -> ASSIGNED_SHARES
  -> COVERED_CALL_OPEN
  -> CALL_CLOSED_PROFIT -> ASSIGNED_SHARES
  -> CALL_EXPIRED_WORTHLESS -> ASSIGNED_SHARES
  -> SHARES_CALLED_AWAY -> CASH
```

### 16.2 Check interval

During regular market hours:

```text
check_positions_every = 15 minutes
```

Outside regular market hours:

```text
no_new_orders = true
risk_report_only = true
```

### 16.3 Required automation checks

Every cycle:

1. Reconcile account cash.
2. Reconcile open stock positions.
3. Reconcile open option positions.
4. Recalculate economic cost basis.
5. Recalculate option profit percentage.
6. Check 50% profit target.
7. Check assignment risk.
8. Check earnings/event risk.
9. Check order eligibility.
10. Log decision.

---

## 17. Order Rules

### 17.1 Allowed orders

Allowed:

- Sell-to-open cash-secured put.
- Buy-to-close short put.
- Sell-to-open covered call.
- Buy-to-close covered call.
- Sell shares only when consistent with strategy rules.

Not allowed:

- Naked call.
- Unsecured put.
- Market order for option entry.
- Averaging down through additional short puts without approval.
- Short options outside market hours.
- Futures trades in the Alpaca module.

### 17.2 Limit-order model

```yaml
order_model:
  entry_order_type: limit
  exit_order_type: limit
  starting_price: mid
  max_price_adjustments: 3
  adjustment_interval_seconds: 90
  cancel_if_not_filled: true
```

---

## 18. Daily Market-Close Summary

Generate a report after market close.

```markdown
# Daily Flywheel Options Summary

**Date:** YYYY-MM-DD  
**Account:** Alpaca Paper  
**Strategy Mode:** Paper / Alert / Disabled  

## Current Stage

- Symbol:
- Stage:
- Shares owned:
- Open short puts:
- Open covered calls:

## Premium Collected

| Source | Today | Cycle-to-Date | Strategy-to-Date |
|---|---:|---:|---:|
| Put premium | $0.00 | $0.00 | $0.00 |
| Call premium | $0.00 | $0.00 | $0.00 |
| Total premium | $0.00 | $0.00 | $0.00 |

## Open Positions

| Symbol | Position | Strike | Expiration | Entry Credit | Current Value | P/L | Profit % |
|---|---|---:|---|---:|---:|---:|---:|
|  |  |  |  |  |  |  |  |

## Risk Status

- Cash committed:
- Cash reserve:
- Max assignment exposure:
- Largest single-symbol exposure:
- Portfolio drawdown:
- Rule violations:

## Actions Taken

- None

## Actions Queued for Next Market Session

- None
```

---

## 19. Strategy Configuration Example

```yaml
strategy:
  name: flywheel_options_strategy
  mode: paper
  broker: alpaca
  trade_window:
    regular_market_hours_only: true
    avoid_first_minutes: 15
    avoid_last_minutes: 15
  scan_interval_minutes: 15

underlyings:
  allow_list:
    - SPY
    - QQQ
    - IWM
    - XLF
    - XLK
  block_list: []

puts:
  enabled: true
  dte_min: 21
  dte_max: 45
  delta_min_abs: 0.16
  delta_max_abs: 0.35
  target_profit_take: 0.50
  max_bid_ask_spread_pct: 0.10
  avoid_earnings: true

calls:
  enabled: true
  dte_min: 21
  dte_max: 45
  delta_min: 0.16
  delta_max: 0.35
  target_profit_take: 0.50
  strike_must_exceed_economic_basis: true
  max_bid_ask_spread_pct: 0.10

risk:
  max_account_buying_power_committed: 0.50
  min_cash_reserve: 0.25
  max_single_symbol_assignment_value: 0.10
  max_sector_exposure: 0.25
  pause_new_entries_at_drawdown: 0.05
  reduce_size_at_drawdown: 0.08
  disable_at_drawdown: 0.12

execution:
  entry_order_type: limit
  exit_order_type: limit
  use_market_orders: false
  start_at_mid_price: true
  max_order_adjustments: 3
  adjustment_interval_seconds: 90
```

---

## 20. Pseudocode

```python
while market_is_open():
    account = get_account()
    positions = get_positions()
    option_positions = get_option_positions()

    reconcile_state(account, positions, option_positions)
    update_cost_basis()
    update_cycle_metrics()

    for symbol in configured_underlyings:
        if not passes_underlying_filter(symbol):
            continue

        state = get_symbol_state(symbol)

        if state == "CASH":
            candidate_put = find_best_cash_secured_put(symbol)
            if candidate_put and passes_risk_checks(candidate_put):
                place_limit_sell_to_open_put(candidate_put)

        elif state == "SHORT_PUT_OPEN":
            short_put = get_short_put(symbol)
            if profit_percent(short_put) >= 0.50:
                place_limit_buy_to_close(short_put)
            elif assignment_or_roll_review_required(short_put):
                evaluate_put_exit_roll_or_assignment(short_put)

        elif state == "ASSIGNED_SHARES":
            candidate_call = find_best_covered_call(symbol)
            if candidate_call and candidate_call.strike > economic_basis(symbol):
                place_limit_sell_to_open_call(candidate_call)

        elif state == "COVERED_CALL_OPEN":
            short_call = get_short_call(symbol)
            if profit_percent(short_call) >= 0.50:
                place_limit_buy_to_close(short_call)
            elif call_assignment_or_roll_review_required(short_call):
                evaluate_call_exit_roll_or_assignment(short_call)

    sleep(15_minutes)

if market_is_closed():
    generate_daily_summary()
```

---

## 21. Backtesting Requirements

Before running this strategy unattended in paper mode, backtest or simulate:

- At least one calm market period.
- One high-volatility selloff.
- One sideways market.
- One strong bull market.
- One gap-down scenario.
- One assignment-heavy scenario.

### Required metrics

| Metric | Required |
|---|---|
| Total return | Yes |
| Return on committed capital | Yes |
| Max drawdown | Yes |
| Assignment rate | Yes |
| Average days in trade | Yes |
| Win rate | Yes |
| Premium collected | Yes |
| Stock P/L | Yes |
| Option P/L | Yes |
| Slippage estimate | Yes |
| Largest losing cycle | Yes |
| Correlation exposure | Yes |

---

## 22. Failure Modes to Avoid

### 22.1 The premium trap

A high premium is often compensation for high risk. Do not treat premium size alone as a reason to trade.

### 22.2 The assignment denial trap

If you are not willing to own the underlying at the strike, do not sell the put.

### 22.3 The roll-forever trap

Rolling can defer loss recognition while increasing time and exposure. Rolls must improve risk-adjusted outcome.

### 22.4 The covered-call regret trap

If the stock rallies hard, the covered call caps upside. That is not a failure if the trade meets planned return.

### 22.5 The concentration trap

Multiple positions in correlated names can behave like one oversized position during a selloff.

---

## 23. Human Approval Gates

Require manual review before:

- First live deployment.
- Increasing position size.
- Adding a new underlying.
- Trading earnings week.
- Rolling a losing put.
- Selling a call below economic basis.
- Re-enabling strategy after drawdown pause.
- Enabling any futures or futures-options module.

---

## 24. Final Operating Rules

1. Trade only during regular market hours.
2. Use paper trading until validated through logs and backtests.
3. Sell only cash-secured puts.
4. Sell only covered calls.
5. Prefer 21–45 DTE.
6. Close short options at 50% profit when available.
7. Avoid earnings and binary events.
8. Never sell calls below economic basis unless explicitly approved.
9. Keep cash reserves.
10. Track premium, stock P/L, option P/L, and total cycle return separately.
11. Disable new entries during portfolio drawdown events.
12. Treat assignment as an expected branch, not an error.
13. Do not trade futures inside this Alpaca strategy.
14. Review the daily summary after every market close.

---

## 25. References

- Cboe S&P 500 PutWrite Index methodology: collateralized monthly SPX put-writing benchmark.
- Cboe S&P 500 BuyWrite Index methodology: covered-call benchmark on the S&P 500.
- Alpaca options documentation: paper options trading and listed U.S. equity/ETF options support.
- Options Industry Council: cash-secured put and covered-call education, including risk warnings.

