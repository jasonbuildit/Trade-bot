"""
Wheel Strategy — Tier 3: Monitor open iron condor positions.

Processes each condor in state["condor_positions"] every 15-minute cycle.
Returns a list of _mcp_call descriptor dicts for Claude to dispatch.

Exit triggers (in priority order):
  1. Expiry cleanup      — DTE ≤ 0 → remove from state
  2. Loss limit          — current loss ≥ SPREAD_MAX_LOSS_MULT × max_risk → 4-leg close
  3. 50% profit          — debit-to-close ≤ 50% of net_credit → 4-leg close
  4. Untested-side roll  — one side is tested (>50% of wing at risk) → roll the OTHER side
     closer to collect more credit (partial defense, no additional capital)
  5. Fill reconciliation (pending_fill only) — mark filled when legs appear in positions
"""
import json
from datetime import date
from pathlib import Path

ROOT = Path(__file__).parent.parent
STATE_FILE = ROOT / "wheel" / "state.json"
LOG_FILE   = ROOT / "trades" / "wheel_log.md"

from config import (
    PROFIT_CLOSE_PCT,
    SPREAD_MAX_LOSS_MULT,
    CONDOR_WING_WIDTH,
)


def load_state() -> dict:
    with open(STATE_FILE) as f:
        return json.load(f)


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def append_log(entry: str):
    with open(LOG_FILE, "a") as f:
        f.write(f"\n{entry}\n")


def _days_to_expiry(expiry_str: str | None) -> int | None:
    if not expiry_str:
        return None
    try:
        return (date.fromisoformat(expiry_str) - date.today()).days
    except Exception:
        return None


def _lookup_quote(contract: str, option_chains: dict) -> dict:
    for sym_chains in option_chains.values():
        for side in ("puts", "calls"):
            contracts = sym_chains.get(side, {})
            if contract in contracts:
                return contracts[contract].get("latestQuote", {}) or {}
    return {}


def _current_spread_debit(short_contract: str, long_contract: str, option_chains: dict) -> float | None:
    """Cost to close one spread leg-pair: buy back short at ask, sell long at bid."""
    short_q = _lookup_quote(short_contract, option_chains)
    long_q  = _lookup_quote(long_contract,  option_chains)
    short_ask = short_q.get("ap", 0) or 0
    long_bid  = long_q.get("bp", 0) or 0
    if short_ask <= 0 and long_bid <= 0:
        return None
    return round(short_ask - long_bid, 2)


def _close_all_order(condor: dict, reason: str) -> dict:
    """4-leg market order to close the full condor."""
    return {
        "_mcp_call": "place_option_order",
        "order_class": "mleg",
        "qty": "1",
        "type": "market",
        "legs": [
            {"symbol": condor["short_put_contract"],  "ratio_qty": "1", "side": "buy",  "position_intent": "buy_to_close"},
            {"symbol": condor["long_put_contract"],   "ratio_qty": "1", "side": "sell", "position_intent": "sell_to_close"},
            {"symbol": condor["short_call_contract"], "ratio_qty": "1", "side": "buy",  "position_intent": "buy_to_close"},
            {"symbol": condor["long_call_contract"],  "ratio_qty": "1", "side": "sell", "position_intent": "sell_to_close"},
        ],
        "_meta": {
            "action": "close_iron_condor",
            "reason": reason,
            "condor_id": condor["id"],
            "symbol": condor["symbol"],
        },
    }


def _is_side_tested(short_contract: str, long_contract: str, option_chains: dict, wing_width: float) -> bool:
    """
    A wing is 'tested' when the per-share debit-to-close exceeds 50% of the
    per-share wing width (i.e., the spread has lost more than half its max value).
    """
    debit = _current_spread_debit(short_contract, long_contract, option_chains)
    if debit is None:
        return False
    return debit > wing_width * 0.50


def process(
    condor: dict,
    option_chains: dict,
    positions: list,
    trading_days: list,
    dry_run: bool = False,
) -> list[dict]:
    """
    Evaluate one condor position and return list of _mcp_call dicts.

    condor:        one entry from state["condor_positions"]
    option_chains: {symbol: {"puts": {...}, "calls": {...}}} from monitor cycle
    positions:     live positions list from get_all_positions
    trading_days:  calendar list from get_calendar
    dry_run:       if True, return descriptors but don't mutate state
    """
    actions: list[dict] = []
    today_str  = date.today().isoformat()
    condor_id  = condor["id"]
    symbol     = condor["symbol"]
    net_credit = condor.get("net_credit", 0)
    max_risk   = condor.get("max_risk", 0)
    expiry_str = condor.get("expiry_date")
    wing_width = condor.get("wing_width", CONDOR_WING_WIDTH)

    dte = _days_to_expiry(expiry_str)

    # ── Fill reconciliation ────────────────────────────────────────────────────
    if condor.get("order_status") == "pending_fill":
        pos_syms = {p["symbol"] for p in positions}
        if condor.get("short_put_contract", "") in pos_syms:
            if not dry_run:
                state = load_state()
                for c in state.get("condor_positions", []):
                    if c["id"] == condor_id:
                        c["order_status"] = "filled"
                        break
                save_state(state)
                append_log(
                    f"## {today_str} — Iron Condor Fill Confirmed\n"
                    f"ID: {condor_id} | Symbol: {symbol}"
                )
            print(f"[condor_monitor] {symbol}: condor {condor_id} fill confirmed")
        return actions

    # ── Expiry cleanup ─────────────────────────────────────────────────────────
    if dte is not None and dte <= 0:
        print(f"[condor_monitor] {symbol}: condor {condor_id} expired — removing")
        if not dry_run:
            state = load_state()
            state["condor_positions"] = [c for c in state.get("condor_positions", []) if c["id"] != condor_id]
            save_state(state)
            append_log(
                f"## {today_str} — Iron Condor Expired\n"
                f"ID: {condor_id} | Symbol: {symbol} | Expiry: {expiry_str}"
            )
        actions.append({"action": "condor_expired", "condor_id": condor_id, "symbol": symbol})
        return actions

    put_debit  = _current_spread_debit(condor["short_put_contract"],  condor["long_put_contract"],  option_chains)
    call_debit = _current_spread_debit(condor["short_call_contract"], condor["long_call_contract"], option_chains)

    if put_debit is None and call_debit is None:
        print(f"[condor_monitor] {symbol}: condor {condor_id} — no quote data, skipping")
        return actions

    total_debit  = round((put_debit or 0) + (call_debit or 0), 2)
    current_loss = round((total_debit - net_credit) * 100, 2)

    # ── Loss limit ─────────────────────────────────────────────────────────────
    if current_loss >= SPREAD_MAX_LOSS_MULT * max_risk:
        reason = f"loss limit (loss=${current_loss:,.0f} >= {SPREAD_MAX_LOSS_MULT:.0%} × max_risk=${max_risk:,.0f})"
        print(f"[condor_monitor] {symbol}: {reason} — closing condor {condor_id}")
        if not dry_run:
            state = load_state()
            state["condor_positions"] = [c for c in state.get("condor_positions", []) if c["id"] != condor_id]
            save_state(state)
            append_log(
                f"## {today_str} — Iron Condor Closed (Loss Limit)\n"
                f"ID: {condor_id} | Symbol: {symbol} | Loss: ${current_loss:,.0f} | Max risk: ${max_risk:,.0f}"
            )
        actions.append(_close_all_order(condor, reason))
        return actions

    # ── 50% profit close ──────────────────────────────────────────────────────
    if total_debit <= net_credit * PROFIT_CLOSE_PCT:
        reason = f"50% profit (debit=${total_debit:.2f} <= {PROFIT_CLOSE_PCT:.0%} × credit=${net_credit:.2f})"
        print(f"[condor_monitor] {symbol}: {reason} — closing condor {condor_id}")
        if not dry_run:
            state = load_state()
            state["condor_positions"] = [c for c in state.get("condor_positions", []) if c["id"] != condor_id]
            save_state(state)
            append_log(
                f"## {today_str} — Iron Condor Closed (50% Profit)\n"
                f"ID: {condor_id} | Symbol: {symbol} | "
                f"Net credit: ${net_credit:.2f} | Debit-to-close: ${total_debit:.2f}"
            )
        actions.append(_close_all_order(condor, reason))
        return actions

    # ── Untested-side roll ─────────────────────────────────────────────────────
    put_tested  = _is_side_tested(condor["short_put_contract"],  condor["long_put_contract"],  option_chains, wing_width)
    call_tested = _is_side_tested(condor["short_call_contract"], condor["long_call_contract"], option_chains, wing_width)

    if put_tested and not call_tested:
        # Put side is threatened — roll the call side closer to collect more credit
        print(f"[condor_monitor] {symbol}: put side tested — emitting untested-side (call) roll for {condor_id}")
        actions.append({
            "_meta": {
                "action": "roll_untested_side",
                "side": "call",
                "condor_id": condor_id,
                "symbol": symbol,
                "note": "Roll short call spread closer to current price; new roll order requires fresh chain data.",
            },
        })
    elif call_tested and not put_tested:
        print(f"[condor_monitor] {symbol}: call side tested — emitting untested-side (put) roll for {condor_id}")
        actions.append({
            "_meta": {
                "action": "roll_untested_side",
                "side": "put",
                "condor_id": condor_id,
                "symbol": symbol,
                "note": "Roll short put spread closer to current price; new roll order requires fresh chain data.",
            },
        })

    return actions
