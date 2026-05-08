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
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import put_seller
import call_seller
import roller

ROOT = Path(__file__).parent.parent
STATE_FILE = ROOT / "wheel" / "state.json"
LOG_FILE = ROOT / "trades" / "wheel_log.md"
ET = ZoneInfo("America/New_York")

PROFIT_CLOSE_PCT = 0.50   # close when unrealized_pl >= 50% of premium received
LOSS_LIMIT_PCT   = 2.00   # close when unrealized loss >= 200% of premium received
ROLL_PUT_THRESH  = 0.03   # roll put when stock is within 3% of strike (approaching ITM)
ROLL_CALL_THRESH = 0.05   # roll call up when stock is 5%+ above call strike (deeply ITM call)


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


def _close_order(option_sym: str) -> dict:
    return {
        "_mcp_call": "place_option_order",
        "symbol": option_sym, "side": "buy", "qty": "1",
        "position_intent": "buy_to_close", "type": "market",
    }


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
    buying_power = float(account.get("non_marginable_buying_power", 0))
    portfolio_value = float(account.get("portfolio_value", 0))

    print(
        f"[monitor] {now_et.strftime('%Y-%m-%d %H:%M ET')} | "
        f"{len(state['symbols'])} tracked symbols | "
        f"buying power ${buying_power:,.0f}"
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

        # ── Stage 1: short put ─────────────────────────────────────────────
        if stage == 1:
            if option_pos is None and share_pos is not None:
                # ASSIGNED — put was exercised, we now own shares
                cost_basis = float(share_pos["avg_entry_price"])
                premiums = sym_state.get("total_premium_all_cycles", 0)
                print(f"[monitor] {symbol}: ASSIGNED at ${cost_basis:.2f} — moving to Stage 2")

                calls = option_chains.get(symbol, {}).get("calls", {})
                result = call_seller.run(
                    symbol, cost_basis, premiums, calls, trading_days, dry_run=dry_run
                )
                actions.append({"symbol": symbol, "action": "assigned_to_stage2", "order": result})

            elif option_pos is None:
                # Put expired worthless — sell a new one
                print(f"[monitor] {symbol}: put expired worthless — selling new put")
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
                    close_order = _close_order(option_sym)
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

                # 50% profit rule
                elif is_50pct_profit(option_pos):
                    print(f"[monitor] {symbol}: 50% profit rule triggered on {option_sym}")
                    close_order = _close_order(option_sym)
                    if not dry_run:
                        append_log(
                            f"## {today} — 50% Profit Close (Stage 1)\n"
                            f"Symbol: {symbol} | Closed put: {option_sym} | P/L: ${pl:.2f}"
                        )
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
                        option_strike = int(option_sym[-8:]) / 1000
                    except Exception:
                        pass

                    if (current_price > 0 and option_strike > 0 and
                            current_price < option_strike * (1 + ROLL_PUT_THRESH)):
                        print(
                            f"[monitor] {symbol}: put roll trigger — "
                            f"price ${current_price:.2f} within {ROLL_PUT_THRESH*100:.0f}% of "
                            f"strike ${option_strike:.0f}"
                        )
                        # Current ask on put = cost to close (use mid as proxy)
                        current_ask = abs(float(option_pos.get("cost_basis", 0))) / 100
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
            if option_pos is None and share_pos is None:
                # CALLED AWAY — shares sold, back to Stage 1
                cost_basis = sym_state.get("cost_basis", 0) or 0
                total_premium = sym_state.get("total_premium_all_cycles", 0)
                cycle_number = sym_state.get("cycle_number", 1)
                cycle_pl = total_premium * 100  # premium is per-share, × 100 for contract

                print(f"[monitor] {symbol}: CALLED AWAY — back to Stage 1")
                state["symbols"][symbol]["stage"] = 1
                state["symbols"][symbol]["cost_basis"] = None
                state["symbols"][symbol]["shares_qty"] = 0
                save_state(state)

                puts = option_chains.get(symbol, {}).get("puts", {})
                result = put_seller.run(
                    symbol, current_price, buying_power, puts, trading_days,
                    portfolio_value=portfolio_value, dry_run=dry_run,
                )
                actions.append({"symbol": symbol, "action": "called_away_to_stage1", "order": result})

                if not dry_run:
                    append_log(
                        f"## {today} — Cycle Complete: {symbol} Cycle #{cycle_number}\n"
                        f"Called away | Cost basis: ${cost_basis:.2f} | "
                        f"Total premium collected: ${total_premium:.2f} | "
                        f"Cycle premium P/L: +${cycle_pl:.2f}"
                    )

            elif option_pos:
                pl = float(option_pos["unrealized_pl"])

                # Loss limit on covered call
                if is_loss_limit(option_pos):
                    print(
                        f"[monitor] {symbol}: LOSS LIMIT triggered on call {option_sym} | "
                        f"P/L ${pl:.2f} — closing call, holding shares"
                    )
                    close_order = _close_order(option_sym)
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

                # 50% profit on covered call
                elif is_50pct_profit(option_pos):
                    print(f"[monitor] {symbol}: 50% profit rule triggered on call {option_sym}")
                    cost_basis = sym_state.get("cost_basis", 0)
                    premiums = sym_state.get("total_premium_all_cycles", 0)
                    close_order = _close_order(option_sym)
                    if not dry_run:
                        append_log(
                            f"## {today} — 50% Profit Close (Stage 2)\n"
                            f"Symbol: {symbol} | Closed call: {option_sym} | P/L: ${pl:.2f}"
                        )
                    calls = option_chains.get(symbol, {}).get("calls", {})
                    new_call = call_seller.run(
                        symbol, cost_basis, premiums, calls, trading_days, dry_run=dry_run
                    )
                    actions.append({
                        "symbol": symbol, "action": "50pct_close_call",
                        "close_order": close_order, "new_order": new_call,
                    })

                else:
                    # Check for roll-up trigger: stock significantly above call strike
                    try:
                        call_strike = int(option_sym[-8:]) / 1000
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
                        current_ask = abs(float(option_pos.get("cost_basis", 0))) / 100
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

    # ── Daily summary (run at 15:55 ET) ───────────────────────────────────────
    if now_et.hour == 15 and now_et.minute >= 55:
        last_summary = state["account_summary"].get("last_daily_summary")
        if last_summary != today:
            _write_daily_summary(state, pos_map, account, today)
            state["account_summary"]["last_daily_summary"] = today
            save_state(state)

    return {"status": "ok", "actions": actions}


def _write_daily_summary(state: dict, pos_map: dict, account: dict, today: str):
    portfolio_value = float(account.get("portfolio_value", 0))
    total_premium = state["account_summary"].get("total_premium_collected", 0)

    # Risk metrics
    total_max_risk = sum(
        s.get("max_risk", 0) or 0
        for s in state["symbols"].values()
        if s.get("stage") == 1
    )
    bp_utilization = (total_max_risk / portfolio_value * 100) if portfolio_value else 0

    open_count = len(state["symbols"])

    lines = [f"## {today} 15:55 ET — Daily Summary"]
    lines.append(f"Portfolio: ${portfolio_value:,.2f} | Premium collected (all time): ${total_premium:.2f}")
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
        lines.append(
            f"  {symbol}: {stage_label} | Option: {option_sym} | Expiry: {expiry} | "
            f"Premium: ${premium_cycle:.2f} | P/L: {pl_str} | Rolls: {roll_count}"
        )

    summary = "\n".join(lines)
    print(f"\n{'='*60}\n{summary}\n{'='*60}")
    with open(LOG_FILE, "a") as f:
        f.write(f"\n{summary}\n")


if __name__ == "__main__":
    print("monitor.py: invoke via Claude Code with injected MCP data.")
    print("State:", json.dumps(load_state(), indent=2))
