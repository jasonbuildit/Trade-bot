"""
Wheel Strategy Monitor — runs every 15 minutes during market hours.
Checks positions, applies 50% profit rule and 200% loss limit, detects
assignments/call-aways, triggers rolls when needed, writes daily summary at close.

Invoked by Claude Code /schedule agent. Claude calls MCP tools and passes
results to the run() function below.

MCP tools used:
  get_clock, get_all_positions, get_orders,
  get_account_info, get_option_chain, get_calendar,
  place_option_order
"""
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import put_seller
import call_seller
import roller
from occ import parse_strike
from config import (
    PROFIT_CLOSE_PCT,
    FAST_PROFIT_CLOSE_PCT,
    LOSS_LIMIT_PCT,
    ROLL_PUT_THRESH,
    ROLL_CALL_THRESH,
    DRAWDOWN_PAUSE,
    DRAWDOWN_REDUCE,
    DRAWDOWN_DISABLE,
    MARKET_OPEN_BUFFER_MIN,
    MARKET_CLOSE_BUFFER_MIN,
    UNDERLYING_WARN_PCT,
    UNDERLYING_BLOCK_PCT,
    EARNINGS_PROXIMITY_DAYS,
)

ROOT = Path(__file__).parent.parent
STATE_FILE = ROOT / "wheel" / "state.json"
LOG_FILE = ROOT / "trades" / "wheel_log.md"
WATCHLIST_FILE = ROOT / "wheel" / "watchlist.json"
ET = ZoneInfo("America/New_York")


def load_state() -> dict:
    with open(STATE_FILE) as f:
        return json.load(f)


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def append_log(entry: str):
    with open(LOG_FILE, "a") as f:
        f.write(f"\n{entry}\n")


def positions_by_symbol(positions: list) -> dict:
    return {p["symbol"]: p for p in positions}


def _load_watchlist_meta() -> dict:
    """Returns {symbol: metadata_dict} for quick lookup."""
    try:
        with open(WATCHLIST_FILE) as f:
            data = json.load(f)
        return {s["symbol"]: s for s in data.get("symbols", []) if isinstance(s, dict)}
    except Exception:
        return {}


def check_drawdown(account: dict, state: dict) -> float:
    """
    Computes portfolio drawdown from peak equity.
    Updates peak_equity in state (caller must save state).
    Returns drawdown as a fraction (0.10 = 10%).
    """
    equity = float(account.get("equity", 0))
    peak = state["account_summary"].get("peak_equity") or equity  # None → use current equity
    if equity > peak:
        state["account_summary"]["peak_equity"] = equity
        peak = equity
    return (peak - equity) / peak if peak > 0 else 0.0


def _strategy_mode(drawdown: float) -> str:
    if drawdown >= DRAWDOWN_DISABLE:
        return "disabled"
    if drawdown >= DRAWDOWN_REDUCE:
        return "reduce"
    if drawdown >= DRAWDOWN_PAUSE:
        return "pause"
    return "active"


def _in_trading_window(now_et: datetime) -> bool:
    """True if current time is past the open buffer and before the close buffer."""
    open_buffer  = now_et.replace(hour=9,  minute=30, second=0, microsecond=0) + timedelta(minutes=MARKET_OPEN_BUFFER_MIN)
    close_buffer = now_et.replace(hour=16, minute=0,  second=0, microsecond=0) - timedelta(minutes=MARKET_CLOSE_BUFFER_MIN)
    return open_buffer <= now_et <= close_buffer


def _reconcile_pending_fills(state: dict, pos_map: dict, open_orders: list) -> None:
    """
    Cross-references pending_fill orders against live positions and open orders.
    - Option found in pos_map → order filled; record fill price.
    - Option absent from both pos_map and open orders → expired/canceled; remove from state.
    - Option in open orders but not yet in positions → still pending; leave unchanged.
    Mutates state in place; caller must save.
    """
    open_option_syms = {
        o["symbol"] for o in open_orders
        if o.get("asset_class") == "us_option"
    }
    today_str = date.today().isoformat()

    for symbol in list(state["symbols"].keys()):
        sym_state = state["symbols"].get(symbol, {})
        if sym_state.get("order_status") != "pending_fill":
            continue
        option_sym = sym_state.get("option_symbol")
        if not option_sym:
            continue

        if option_sym in pos_map:
            fill_price = float(pos_map[option_sym].get("avg_entry_price", 0))
            state["symbols"][symbol]["order_status"] = "filled"
            state["symbols"][symbol]["fill_price"] = fill_price
            print(f"[monitor] {symbol}: fill confirmed — {option_sym} at avg ${fill_price:.2f}")
            append_log(
                f"## {today_str} — Fill Confirmed\n"
                f"Symbol: {symbol} | Option: {option_sym} | Fill price: ${fill_price:.2f}"
            )
        elif option_sym not in open_option_syms:
            print(
                f"[monitor] {symbol}: order expired/canceled — {option_sym} not found in "
                f"positions or open orders. Clearing state, ready for fresh screener run."
            )
            append_log(
                f"## {today_str} — Order Expired/Canceled\n"
                f"Symbol: {symbol} | Option: {option_sym} | "
                f"Was pending_fill but not found in live positions or open orders. State cleared."
            )
            del state["symbols"][symbol]


def _can_redeploy(symbol: str, now_et: datetime, sym_meta: dict, drawdown: float) -> bool:
    """Gates whether a new position can be opened after a profit close."""
    if not _in_trading_window(now_et):
        print(f"[monitor] {symbol}: redeployment blocked — outside trading window")
        return False
    earnings_date = sym_meta.get("earnings_date")
    if earnings_date:
        days_to_earnings = (date.fromisoformat(earnings_date) - date.today()).days
        if 0 < days_to_earnings <= EARNINGS_PROXIMITY_DAYS:
            print(f"[monitor] {symbol}: redeployment blocked — earnings in {days_to_earnings} days")
            return False
    if drawdown >= DRAWDOWN_PAUSE:
        print(f"[monitor] {symbol}: redeployment blocked — portfolio drawdown {drawdown:.1%}")
        return False
    return True


def is_fast_profit(position: dict) -> bool:
    """Close early when 75%+ of max profit is reached — don't wait for pennies."""
    cost_basis = float(position.get("cost_basis", 0))
    unrealized_pl = float(position.get("unrealized_pl", 0))
    if cost_basis >= 0:
        return False
    return unrealized_pl >= abs(cost_basis) * FAST_PROFIT_CLOSE_PCT


def is_50pct_profit(position: dict) -> bool:
    """
    For a short option: unrealized_pl is positive when option lost value (good for us).
    Trigger when unrealized_pl >= 50% of original proceeds received.
    """
    cost_basis = float(position.get("cost_basis", 0))
    unrealized_pl = float(position.get("unrealized_pl", 0))
    if cost_basis >= 0:
        return False
    return unrealized_pl >= abs(cost_basis) * PROFIT_CLOSE_PCT


def is_loss_limit(position: dict) -> bool:
    """
    Trigger when unrealized loss >= 200% of premium received.
    unrealized_pl is negative when short option has gained value against us.
    """
    cost_basis = float(position.get("cost_basis", 0))
    unrealized_pl = float(position.get("unrealized_pl", 0))
    if cost_basis >= 0:
        return False
    # cost_basis is negative (proceeds received); loss limit = loss >= 2× proceeds
    return unrealized_pl <= cost_basis * LOSS_LIMIT_PCT


def _close_order(option_sym: str, limit_price: float | None = None) -> dict:
    """
    limit_price: use for profit closes (patient). Omit for loss-limit closes (urgent → market).
    """
    order: dict = {
        "_mcp_call": "place_option_order",
        "symbol": option_sym, "side": "buy", "qty": "1",
        "position_intent": "buy_to_close",
    }
    if limit_price is not None and limit_price > 0:
        order["type"] = "limit"
        order["limit_price"] = str(round(limit_price, 2))
    else:
        order["type"] = "market"
    return order


def _mark_price(option_pos: dict) -> float:
    """Current per-share mark of a short option from its position market_value."""
    return abs(float(option_pos.get("market_value", 0))) / 100


def run(
    clock: dict,
    positions: list,
    open_orders: list,
    account: dict,
    option_chains: dict,
    trading_days: list,
    snapshots: dict,
    dry_run: bool = False,
) -> dict:
    """
    Main monitor entry point. All MCP data injected by Claude.

    clock:         get_clock() result
    positions:     get_all_positions() result list
    open_orders:   get_orders(status="open") result list
    account:       get_account_info() result
    option_chains: {symbol: {calls:{}, puts:{}}} — pre-fetched chains for watchlist
    trading_days:  get_calendar() result
    snapshots:     {symbol: snapshot} — get_stock_snapshot per watchlist symbol
    dry_run:       skip actual order placement
    """
    actions = []
    now_et = datetime.now(ET)
    today = date.today().isoformat()

    # ── Market hours guard ────────────────────────────────────────────────────
    if not clock.get("is_open"):
        print(f"[monitor] Market closed — nothing to do.")
        return {"status": "market_closed", "actions": []}

    state = load_state()
    pos_map = positions_by_symbol(positions)
    _reconcile_pending_fills(state, pos_map, open_orders)
    save_state(state)
    buying_power = float(account.get("non_marginable_buying_power", 0))
    portfolio_value = float(account.get("portfolio_value", 0))
    watchlist_meta = _load_watchlist_meta()

    # ── Portfolio drawdown check ──────────────────────────────────────────────
    drawdown = check_drawdown(account, state)
    mode = _strategy_mode(drawdown)

    if mode == "disabled":
        print(
            f"[monitor] STRATEGY DISABLED — portfolio drawdown {drawdown:.1%} "
            f"exceeds {DRAWDOWN_DISABLE:.0%} threshold. Close/roll existing positions only."
        )
        append_log(
            f"## {today} — STRATEGY DISABLED\n"
            f"Portfolio drawdown {drawdown:.1%} >= {DRAWDOWN_DISABLE:.0%} threshold. "
            f"No new premium selling until manually reviewed and re-enabled."
        )
        # Save updated peak_equity even in disabled mode
        save_state(state)
        return {"status": "strategy_disabled", "drawdown": drawdown, "actions": []}

    if mode == "reduce":
        print(f"[monitor] DRAWDOWN ALERT: {drawdown:.1%} >= {DRAWDOWN_REDUCE:.0%} — consider reducing position size")
    elif mode == "pause":
        print(f"[monitor] DRAWDOWN PAUSE: {drawdown:.1%} >= {DRAWDOWN_PAUSE:.0%} — new entries blocked")

    save_state(state)  # persist updated peak_equity

    in_window = _in_trading_window(now_et)

    print(
        f"[monitor] {now_et.strftime('%Y-%m-%d %H:%M ET')} | "
        f"{len(state['symbols'])} tracked symbols | "
        f"buying power ${buying_power:,.0f} | "
        f"drawdown {drawdown:.1%} | mode {mode} | window {'OPEN' if in_window else 'BUFFER'}"
    )

    # ── Per-symbol checks ─────────────────────────────────────────────────────
    for symbol, sym_state in list(state["symbols"].items()):
        stage = sym_state.get("stage")
        option_sym = sym_state.get("option_symbol")
        option_pos = pos_map.get(option_sym) if option_sym else None
        share_pos = pos_map.get(symbol)

        snap = snapshots.get(symbol, {})
        current_price = float(
            snap.get("latestTrade", {}).get("p", 0) or
            snap.get("latestQuote", {}).get("ap", 0)
        )

        sym_meta = watchlist_meta.get(symbol, {})

        # ── Stage 1: short put ─────────────────────────────────────────────
        if stage == 1:
            # Underlying price health check vs put entry
            entry_price = sym_state.get("entry_price")
            if entry_price and current_price > 0:
                price_chg = (current_price - entry_price) / entry_price
                if price_chg <= -UNDERLYING_BLOCK_PCT:
                    print(
                        f"[monitor] {symbol}: ALERT — stock ${current_price:.2f} is "
                        f"{abs(price_chg):.1%} below put entry ${entry_price:.2f} — "
                        f"no new puts on this symbol"
                    )
                elif price_chg <= -UNDERLYING_WARN_PCT:
                    print(
                        f"[monitor] {symbol}: WARNING — stock ${current_price:.2f} is "
                        f"{abs(price_chg):.1%} below put entry ${entry_price:.2f} — monitoring"
                    )

            underlying_blocked = (
                entry_price and current_price > 0 and
                (current_price - entry_price) / entry_price <= -UNDERLYING_BLOCK_PCT
            )

            if option_pos is None and share_pos is not None and float(share_pos.get("qty", 0)) >= 100:
                # ASSIGNED — put was exercised, we now own ≥100 shares
                cost_basis = float(share_pos["avg_entry_price"])
                premiums = sym_state.get("total_premium_all_cycles", 0)
                print(f"[monitor] {symbol}: ASSIGNED at ${cost_basis:.2f} — moving to Stage 2")

                calls = option_chains.get(symbol, {}).get("calls", {})
                result = None
                if in_window and mode != "pause":
                    result = call_seller.run(
                        symbol, cost_basis, premiums, calls, trading_days, dry_run=dry_run
                    )
                else:
                    print(f"[monitor] {symbol}: assignment detected but new call blocked (mode={mode}, window={in_window})")
                actions.append({"symbol": symbol, "action": "assigned_to_stage2", "order": result})

            elif option_pos is None:
                # Put expired worthless — sell a new one if conditions allow
                print(f"[monitor] {symbol}: put expired worthless")
                if mode == "pause" or not in_window or underlying_blocked:
                    reason = "drawdown pause" if mode == "pause" else ("outside window" if not in_window else "underlying blocked")
                    print(f"[monitor] {symbol}: skipping new put — {reason}")
                else:
                    puts = option_chains.get(symbol, {}).get("puts", {})
                    result = put_seller.run(
                        symbol, current_price, buying_power, puts, trading_days,
                        portfolio_value=portfolio_value, dry_run=dry_run,
                    )
                    actions.append({"symbol": symbol, "action": "put_expired_resell", "order": result})

            elif option_pos:
                pl = float(option_pos["unrealized_pl"])

                # Loss limit — check first (higher urgency than profit close)
                if is_loss_limit(option_pos):
                    print(
                        f"[monitor] {symbol}: LOSS LIMIT triggered on {option_sym} | "
                        f"P/L ${pl:.2f} — closing immediately, NO auto-resell"
                    )
                    close_order = _close_order(option_sym)  # market — urgency over price
                    if not dry_run:
                        append_log(
                            f"## {today} — LOSS LIMIT (Stage 1)\n"
                            f"Symbol: {symbol} | Closed put: {option_sym} | "
                            f"P/L: ${pl:.2f} | Manual review required before re-entry"
                        )
                    actions.append({
                        "symbol": symbol, "action": "loss_limit_put",
                        "close_order": close_order,
                    })

                # 75% fast profit rule (check before 50%)
                elif is_fast_profit(option_pos):
                    print(f"[monitor] {symbol}: 75% FAST PROFIT on {option_sym} | P/L ${pl:.2f}")
                    close_order = _close_order(option_sym, limit_price=_mark_price(option_pos))
                    if not dry_run:
                        append_log(
                            f"## {today} — 75% Fast Profit Close (Stage 1)\n"
                            f"Symbol: {symbol} | Closed put: {option_sym} | P/L: ${pl:.2f}"
                        )
                    new_put = None
                    if _can_redeploy(symbol, now_et, sym_meta, drawdown) and not underlying_blocked:
                        puts = option_chains.get(symbol, {}).get("puts", {})
                        new_put = put_seller.run(
                            symbol, current_price, buying_power, puts, trading_days,
                            portfolio_value=portfolio_value, dry_run=dry_run,
                        )
                    actions.append({
                        "symbol": symbol, "action": "fast_profit_close_put",
                        "close_order": close_order, "new_order": new_put,
                    })

                # 50% profit rule
                elif is_50pct_profit(option_pos):
                    print(f"[monitor] {symbol}: 50% profit rule triggered on {option_sym}")
                    close_order = _close_order(option_sym, limit_price=_mark_price(option_pos))
                    if not dry_run:
                        append_log(
                            f"## {today} — 50% Profit Close (Stage 1)\n"
                            f"Symbol: {symbol} | Closed put: {option_sym} | P/L: ${pl:.2f}"
                        )
                    new_put = None
                    if _can_redeploy(symbol, now_et, sym_meta, drawdown) and not underlying_blocked:
                        puts = option_chains.get(symbol, {}).get("puts", {})
                        new_put = put_seller.run(
                            symbol, current_price, buying_power, puts, trading_days,
                            portfolio_value=portfolio_value, dry_run=dry_run,
                        )
                    actions.append({
                        "symbol": symbol, "action": "50pct_close_put",
                        "close_order": close_order, "new_order": new_put,
                    })

                else:
                    # Check for roll trigger: stock within ROLL_PUT_THRESH of strike
                    option_strike = sym_state.get("breakeven", 0) or 0
                    try:
                        option_strike = parse_strike(option_sym)
                    except Exception:
                        pass

                    if (current_price > 0 and option_strike > 0 and
                            current_price < option_strike * (1 + ROLL_PUT_THRESH)):
                        print(
                            f"[monitor] {symbol}: put roll trigger — "
                            f"price ${current_price:.2f} within {ROLL_PUT_THRESH*100:.0f}% of "
                            f"strike ${option_strike:.0f}"
                        )
                        current_ask = _mark_price(option_pos)  # current option value from market_value
                        puts = option_chains.get(symbol, {}).get("puts", {})
                        roll = roller.roll_put_down_and_out(
                            symbol, option_sym, current_ask, puts, trading_days
                        )
                        if roll:
                            if not dry_run:
                                state["symbols"][symbol]["roll_count"] = (
                                    sym_state.get("roll_count", 0) + 1
                                )
                                save_state(state)
                                append_log(
                                    f"## {today} — Put Roll (Stage 1)\n"
                                    f"Symbol: {symbol} | Rolled {option_sym} → "
                                    f"{roll['_meta']['new_contract']} | "
                                    f"Net credit: ${roll['_meta']['net_credit']:.2f}"
                                )
                            actions.append({"symbol": symbol, "action": "roll_put", "order": roll})
                        else:
                            print(f"[monitor] {symbol}: no credit roll available — holding")
                    else:
                        print(f"[monitor] {symbol}: Stage 1 holding | P/L ${pl:.2f}")

        # ── Stage 2: short call ────────────────────────────────────────────
        elif stage == 2:
            cost_basis_stage2 = sym_state.get("cost_basis", 0) or 0

            # Underlying health check vs assigned cost basis
            if cost_basis_stage2 > 0 and current_price > 0:
                stock_chg = (current_price - cost_basis_stage2) / cost_basis_stage2
                if stock_chg <= -UNDERLYING_BLOCK_PCT:
                    print(
                        f"[monitor] {symbol}: ALERT — assigned stock ${current_price:.2f} is "
                        f"{abs(stock_chg):.1%} below cost basis ${cost_basis_stage2:.2f} — "
                        f"no new puts on this symbol after call closes"
                    )

            if option_pos is None and share_pos is None:
                # CALLED AWAY — shares sold, back to Stage 1
                total_premium = sym_state.get("total_premium_all_cycles", 0)
                cycle_number = sym_state.get("cycle_number", 1)
                cycle_pl = total_premium * 100  # premium is per-share, × 100 for contract

                print(f"[monitor] {symbol}: CALLED AWAY — back to Stage 1")
                state["symbols"][symbol]["stage"] = 1
                state["symbols"][symbol]["cost_basis"] = None
                state["symbols"][symbol]["shares_qty"] = 0
                save_state(state)

                new_put = None
                if in_window and mode != "pause" and _can_redeploy(symbol, now_et, sym_meta, drawdown):
                    puts = option_chains.get(symbol, {}).get("puts", {})
                    new_put = put_seller.run(
                        symbol, current_price, buying_power, puts, trading_days,
                        portfolio_value=portfolio_value, dry_run=dry_run,
                    )
                else:
                    print(f"[monitor] {symbol}: called away — new put blocked (mode={mode}, window={in_window})")

                actions.append({"symbol": symbol, "action": "called_away_to_stage1", "order": new_put})

                if not dry_run:
                    append_log(
                        f"## {today} — Cycle Complete: {symbol} Cycle #{cycle_number}\n"
                        f"Called away | Cost basis: ${cost_basis_stage2:.2f} | "
                        f"Total premium collected: ${total_premium:.2f} | "
                        f"Cycle premium P/L: +${cycle_pl:.2f}"
                    )

            elif option_pos:
                pl = float(option_pos["unrealized_pl"])
                cost_basis_call = sym_state.get("cost_basis", 0)
                premiums = sym_state.get("total_premium_all_cycles", 0)

                # Loss limit on covered call
                if is_loss_limit(option_pos):
                    print(
                        f"[monitor] {symbol}: LOSS LIMIT triggered on call {option_sym} | "
                        f"P/L ${pl:.2f} — closing call, holding shares"
                    )
                    close_order = _close_order(option_sym)  # market — urgency over price
                    if not dry_run:
                        append_log(
                            f"## {today} — LOSS LIMIT (Stage 2 Call)\n"
                            f"Symbol: {symbol} | Closed call: {option_sym} | "
                            f"P/L: ${pl:.2f} | Shares retained, manual review required"
                        )
                    actions.append({
                        "symbol": symbol, "action": "loss_limit_call",
                        "close_order": close_order,
                    })

                # 75% fast profit on covered call
                elif is_fast_profit(option_pos):
                    print(f"[monitor] {symbol}: 75% FAST PROFIT on call {option_sym} | P/L ${pl:.2f}")
                    close_order = _close_order(option_sym, limit_price=_mark_price(option_pos))
                    if not dry_run:
                        append_log(
                            f"## {today} — 75% Fast Profit Close (Stage 2)\n"
                            f"Symbol: {symbol} | Closed call: {option_sym} | P/L: ${pl:.2f}"
                        )
                    new_call = None
                    if _can_redeploy(symbol, now_et, sym_meta, drawdown):
                        calls = option_chains.get(symbol, {}).get("calls", {})
                        new_call = call_seller.run(
                            symbol, cost_basis_call, premiums, calls, trading_days, dry_run=dry_run
                        )
                    actions.append({
                        "symbol": symbol, "action": "fast_profit_close_call",
                        "close_order": close_order, "new_order": new_call,
                    })

                # 50% profit on covered call
                elif is_50pct_profit(option_pos):
                    print(f"[monitor] {symbol}: 50% profit rule triggered on call {option_sym}")
                    close_order = _close_order(option_sym, limit_price=_mark_price(option_pos))
                    if not dry_run:
                        append_log(
                            f"## {today} — 50% Profit Close (Stage 2)\n"
                            f"Symbol: {symbol} | Closed call: {option_sym} | P/L: ${pl:.2f}"
                        )
                    new_call = None
                    if _can_redeploy(symbol, now_et, sym_meta, drawdown):
                        calls = option_chains.get(symbol, {}).get("calls", {})
                        new_call = call_seller.run(
                            symbol, cost_basis_call, premiums, calls, trading_days, dry_run=dry_run
                        )
                    actions.append({
                        "symbol": symbol, "action": "50pct_close_call",
                        "close_order": close_order, "new_order": new_call,
                    })

                else:
                    # Check for roll-up trigger: stock significantly above call strike
                    try:
                        call_strike = parse_strike(option_sym)
                    except Exception:
                        call_strike = 0

                    if (current_price > 0 and call_strike > 0 and
                            current_price > call_strike * (1 + ROLL_CALL_THRESH)):
                        print(
                            f"[monitor] {symbol}: call roll-up trigger — "
                            f"price ${current_price:.2f} vs strike ${call_strike:.0f}"
                        )
                        cost_basis = sym_state.get("cost_basis", 0) or 0
                        premiums = sym_state.get("total_premium_all_cycles", 0)
                        effective_basis = cost_basis - premiums
                        current_ask = _mark_price(option_pos)  # current option value from market_value
                        calls = option_chains.get(symbol, {}).get("calls", {})
                        roll = roller.roll_call_up_and_out(
                            symbol, option_sym, current_ask, calls,
                            trading_days, effective_basis=effective_basis,
                        )
                        if roll:
                            if not dry_run:
                                state["symbols"][symbol]["roll_count"] = (
                                    sym_state.get("roll_count", 0) + 1
                                )
                                save_state(state)
                                append_log(
                                    f"## {today} — Call Roll Up (Stage 2)\n"
                                    f"Symbol: {symbol} | Rolled {option_sym} → "
                                    f"{roll['_meta']['new_contract']} | "
                                    f"Net credit: ${roll['_meta']['net_credit']:.2f}"
                                )
                            actions.append({"symbol": symbol, "action": "roll_call_up", "order": roll})
                        else:
                            print(f"[monitor] {symbol}: no credit roll-up available — holding")
                    else:
                        print(f"[monitor] {symbol}: Stage 2 holding | P/L ${pl:.2f}")

    # ── Daily summary (run in final 15 min of trading day) ───────────────────
    close_summary_dt = now_et.replace(hour=15, minute=45, second=0, microsecond=0)
    if now_et >= close_summary_dt:
        last_summary = state["account_summary"].get("last_daily_summary")
        if last_summary != today:
            _write_daily_summary(state, pos_map, account, today)
            state["account_summary"]["last_daily_summary"] = today
            save_state(state)

    return {"status": "ok", "actions": actions}


def _write_daily_summary(state: dict, pos_map: dict, account: dict, today: str):
    portfolio_value = float(account.get("portfolio_value", 0))
    total_premium = state["account_summary"].get("total_premium_collected", 0)

    # Drawdown
    peak_equity = state["account_summary"].get("peak_equity") or portfolio_value
    drawdown = (peak_equity - portfolio_value) / peak_equity if peak_equity > 0 else 0.0
    mode = _strategy_mode(drawdown)

    # Risk metrics
    total_max_risk = sum(
        s.get("max_risk", 0) or 0
        for s in state["symbols"].values()
        if s.get("stage") == 1
    )
    bp_utilization = (total_max_risk / portfolio_value * 100) if portfolio_value else 0

    open_count = len(state["symbols"])

    now_str = datetime.now(ET).strftime("%H:%M")
    lines = [f"## {today} {now_str} ET — Daily Summary"]
    lines.append(
        f"Portfolio: ${portfolio_value:,.2f} | Peak: ${peak_equity:,.2f} | "
        f"Drawdown: {drawdown:.1%} | Mode: {mode.upper()}"
    )
    lines.append(f"Premium collected (all time): ${total_premium:.2f}")
    lines.append(
        f"Capital at risk (puts): ${total_max_risk:,.0f} | "
        f"BP utilization: {bp_utilization:.1f}% | "
        f"Open positions: {open_count}"
    )
    lines.append("")

    for symbol, sym_state in state["symbols"].items():
        stage = sym_state.get("stage")
        stage_label = "Stage 1 (short put)" if stage == 1 else "Stage 2 (covered call)"
        premium_cycle = sym_state.get("total_premium_all_cycles", 0)
        option_sym = sym_state.get("option_symbol", "—")
        expiry = sym_state.get("expiry_date", "—")
        roll_count = sym_state.get("roll_count", 0)
        pos = pos_map.get(option_sym)
        pl_str = f"${float(pos['unrealized_pl']):.2f}" if pos else "n/a"
        entry_price = sym_state.get("entry_price")
        cost_basis = sym_state.get("cost_basis")
        ref_price = cost_basis if stage == 2 and cost_basis else entry_price
        lines.append(
            f"  {symbol}: {stage_label} | Option: {option_sym} | Expiry: {expiry} | "
            f"Premium: ${premium_cycle:.2f} | P/L: {pl_str} | Rolls: {roll_count}"
            + (f" | Entry: ${ref_price:.2f}" if ref_price else "")
        )

    summary = "\n".join(lines)
    print(f"\n{'='*60}\n{summary}\n{'='*60}")
    with open(LOG_FILE, "a") as f:
        f.write(f"\n{summary}\n")


if __name__ == "__main__":
    print("monitor.py: invoke via Claude Code with injected MCP data.")
    print("State:", json.dumps(load_state(), indent=2))
