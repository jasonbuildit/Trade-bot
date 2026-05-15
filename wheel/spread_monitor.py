"""
Wheel Strategy — Tier 2: Monitor open put credit spreads.

Processes each spread in state["spread_positions"] every 15-minute cycle.
Returns a list of _mcp_call descriptor dicts for Claude to dispatch.

Exit triggers (in priority order):
  1. Expiry cleanup  — DTE ≤ 0 → mark closed/expired, remove from state
  2. Gamma-DTE close — DTE < GAMMA_DTE_THRESHOLD and spread has intrinsic loss → close
  3. Loss limit      — current loss ≥ SPREAD_MAX_LOSS_MULT × max_risk → close
  4. 50% profit      — debit-to-close ≤ 50% of net_credit → close
  5. Fill reconciliation (pending_fill only) — mark filled when contract appears in positions
"""
import json
from datetime import date
from pathlib import Path

ROOT = Path(__file__).parent.parent
STATE_FILE = ROOT / "wheel" / "state.json"
LOG_FILE = ROOT / "trades" / "wheel_log.md"

from config import (
    PROFIT_CLOSE_PCT,
    SPREAD_MAX_LOSS_MULT,
    GAMMA_DTE_THRESHOLD,
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
    """Return latestQuote dict for a contract across all option_chains."""
    for sym_chains in option_chains.values():
        for side in ("puts", "calls"):
            contracts = sym_chains.get(side, {})
            if contract in contracts:
                return contracts[contract].get("latestQuote", {}) or {}
    return {}


def _current_debit(spread: dict, option_chains: dict) -> float | None:
    """
    Current cost to close the spread (buy back short, sell long).
    debit = short_ask - long_bid
    Returns None if quote data is unavailable.
    """
    short_q = _lookup_quote(spread["short_contract"], option_chains)
    long_q  = _lookup_quote(spread["long_contract"],  option_chains)

    short_ask = short_q.get("ap", 0) or 0
    long_bid  = long_q.get("bp", 0) or 0

    if short_ask <= 0 and long_bid <= 0:
        return None

    return round(short_ask - long_bid, 2)


def _close_order(spread: dict, reason: str) -> dict:
    """2-leg mleg buy-back order descriptor."""
    return {
        "_mcp_call": "place_option_order",
        "order_class": "mleg",
        "qty": "1",
        "type": "market",
        "legs": [
            {
                "symbol": spread["short_contract"],
                "ratio_qty": "1",
                "side": "buy",
                "position_intent": "buy_to_close",
            },
            {
                "symbol": spread["long_contract"],
                "ratio_qty": "1",
                "side": "sell",
                "position_intent": "sell_to_close",
            },
        ],
        "_meta": {
            "action": "close_put_spread",
            "reason": reason,
            "spread_id": spread["id"],
            "symbol": spread["symbol"],
        },
    }


def process(
    spread: dict,
    option_chains: dict,
    positions: list,
    trading_days: list,
    dry_run: bool = False,
) -> list[dict]:
    """
    Evaluate one spread position and return list of _mcp_call dicts.

    spread:        one entry from state["spread_positions"]
    option_chains: {symbol: {"puts": {...}, "calls": {...}}} from monitor cycle
    positions:     live positions list from get_all_positions
    trading_days:  calendar list from get_calendar
    dry_run:       if True, return descriptors but don't mutate state
    """
    actions: list[dict] = []
    today_str = date.today().isoformat()
    spread_id = spread["id"]
    symbol    = spread["symbol"]
    net_credit = spread.get("net_credit", 0)
    max_risk   = spread.get("max_risk", 0)
    expiry_str = spread.get("expiry_date")

    dte = _days_to_expiry(expiry_str)

    # ── Fill reconciliation ────────────────────────────────────────────────────
    if spread.get("order_status") == "pending_fill":
        pos_syms = {p["symbol"] for p in positions}
        short_c  = spread.get("short_contract", "")
        # Either leg confirmed in live positions means spread filled
        if short_c in pos_syms or spread.get("long_contract", "") in pos_syms:
            if not dry_run:
                state = load_state()
                for s in state.get("spread_positions", []):
                    if s["id"] == spread_id:
                        s["order_status"] = "filled"
                        break
                save_state(state)
                append_log(
                    f"## {today_str} — Put Spread Fill Confirmed\n"
                    f"ID: {spread_id} | Symbol: {symbol} | "
                    f"Short: {spread['short_contract']} | Long: {spread['long_contract']}"
                )
            print(f"[spread_monitor] {symbol}: spread {spread_id} fill confirmed")
        # Still pending — nothing else to do this cycle
        return actions

    # ── Expiry cleanup ─────────────────────────────────────────────────────────
    if dte is not None and dte <= 0:
        print(f"[spread_monitor] {symbol}: spread {spread_id} expired — removing from state")
        if not dry_run:
            state = load_state()
            state["spread_positions"] = [
                s for s in state.get("spread_positions", []) if s["id"] != spread_id
            ]
            save_state(state)
            append_log(
                f"## {today_str} — Put Spread Expired\n"
                f"ID: {spread_id} | Symbol: {symbol} | Expiry: {expiry_str} | "
                f"Net credit: ${net_credit:.2f} | Max risk: ${max_risk:,.0f}"
            )
        actions.append({
            "action": "spread_expired",
            "spread_id": spread_id,
            "symbol": symbol,
        })
        return actions

    debit = _current_debit(spread, option_chains)

    # ── Gamma-DTE close ────────────────────────────────────────────────────────
    if dte is not None and dte < GAMMA_DTE_THRESHOLD:
        # Close any spread with intrinsic risk near expiry (debit > 0 = cost to close)
        has_risk = debit is None or debit > 0
        if has_risk:
            reason = f"gamma-DTE close (DTE={dte}, debit=${debit if debit is not None else '?'})"
            print(f"[spread_monitor] {symbol}: {reason} — closing spread {spread_id}")
            if not dry_run:
                state = load_state()
                state["spread_positions"] = [
                    s for s in state.get("spread_positions", []) if s["id"] != spread_id
                ]
                save_state(state)
                append_log(
                    f"## {today_str} — Put Spread Closed (Gamma-DTE)\n"
                    f"ID: {spread_id} | Symbol: {symbol} | DTE: {dte} | "
                    f"Debit-to-close: ${debit if debit is not None else '?'}"
                )
            actions.append(_close_order(spread, reason))
            return actions

    if debit is None:
        print(f"[spread_monitor] {symbol}: spread {spread_id} — no quote data, skipping")
        return actions

    current_loss = round((debit - net_credit) * 100, 2)  # positive = loss

    # ── Loss limit ────────────────────────────────────────────────────────────
    if current_loss >= SPREAD_MAX_LOSS_MULT * max_risk:
        reason = f"loss limit (loss=${current_loss:,.0f} >= {SPREAD_MAX_LOSS_MULT:.0%} × max_risk=${max_risk:,.0f})"
        print(f"[spread_monitor] {symbol}: {reason} — closing spread {spread_id}")
        if not dry_run:
            state = load_state()
            state["spread_positions"] = [
                s for s in state.get("spread_positions", []) if s["id"] != spread_id
            ]
            save_state(state)
            append_log(
                f"## {today_str} — Put Spread Closed (Loss Limit)\n"
                f"ID: {spread_id} | Symbol: {symbol} | "
                f"Debit: ${debit:.2f} | Loss: ${current_loss:,.0f} | Max risk: ${max_risk:,.0f}"
            )
        actions.append(_close_order(spread, reason))
        return actions

    # ── 50% profit close ──────────────────────────────────────────────────────
    profit_target = net_credit * PROFIT_CLOSE_PCT
    if debit <= profit_target:
        reason = f"50% profit (debit=${debit:.2f} <= {PROFIT_CLOSE_PCT:.0%} × net_credit=${net_credit:.2f})"
        print(f"[spread_monitor] {symbol}: {reason} — closing spread {spread_id}")
        if not dry_run:
            state = load_state()
            state["spread_positions"] = [
                s for s in state.get("spread_positions", []) if s["id"] != spread_id
            ]
            save_state(state)
            append_log(
                f"## {today_str} — Put Spread Closed (50% Profit)\n"
                f"ID: {spread_id} | Symbol: {symbol} | "
                f"Net credit: ${net_credit:.2f} | Debit-to-close: ${debit:.2f} | "
                f"Profit: ${(net_credit - debit) * 100:.0f}"
            )
        actions.append(_close_order(spread, reason))
        return actions

    return actions
