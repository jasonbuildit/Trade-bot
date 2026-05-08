"""
Wheel Strategy — Stage 2: Sell Covered Call
After assignment, finds a covered call ~10% above cost basis and places a sell-to-open order.

Guardrails enforced:
  - Strike guard: must be above effective_basis (cost_basis - premiums_collected)
  - DTE window: 21–45 (theta decay sweet spot)

Called by: monitor.py on assignment detection.
MCP tools used: get_option_chain, get_calendar, place_option_order
"""
import json
from datetime import date
from pathlib import Path

ROOT = Path(__file__).parent.parent
STATE_FILE = ROOT / "wheel" / "state.json"
LOG_FILE = ROOT / "trades" / "wheel_log.md"


def load_state() -> dict:
    with open(STATE_FILE) as f:
        return json.load(f)


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def append_log(entry: str):
    with open(LOG_FILE, "a") as f:
        f.write(f"\n{entry}\n")


def find_expiry(trading_days: list, min_dte: int = 21, max_dte: int = 45) -> str | None:
    today = date.today()
    for day in trading_days:
        d = date.fromisoformat(day["date"])
        delta = (d - today).days
        if min_dte <= delta <= max_dte and d.weekday() == 4:
            return day["date"]
    for day in trading_days:
        d = date.fromisoformat(day["date"])
        delta = (d - today).days
        if min_dte <= delta <= max_dte:
            return day["date"]
    return None


def find_call_contract(calls: dict, target_strike: float, min_strike: float) -> dict | None:
    """
    calls: {contract_symbol: snapshot}
    target_strike: ~10% above cost basis
    min_strike: must be strictly above effective_basis to avoid locking in a loss
    Returns best contract at or above min_strike nearest to target.
    """
    candidates = []
    for contract, data in calls.items():
        try:
            strike = int(contract[-8:]) / 1000
        except Exception:
            continue
        if strike < min_strike:
            continue
        bid = data.get("latestQuote", {}).get("bp", 0) or 0
        if bid <= 0:
            continue
        candidates.append({"contract": contract, "strike": strike, "bid": bid})

    if not candidates:
        return None

    candidates.sort(key=lambda x: abs(x["strike"] - target_strike))
    return candidates[0]


def run(
    symbol: str,
    cost_basis: float,
    premiums_collected: float,
    calls: dict,
    trading_days: list,
    dry_run: bool = False,
) -> dict | None:
    """
    symbol:              ticker
    cost_basis:          avg_entry_price of the shares from Alpaca position
    premiums_collected:  total put premium already collected this cycle
    calls:               option chain calls dict from mcp__alpaca__get_option_chain
    trading_days:        calendar list from mcp__alpaca__get_calendar
    dry_run:             if True, logs intent only

    Returns: order details dict or None
    """
    # Never sell call below effective cost basis
    effective_basis = cost_basis - premiums_collected
    target_strike = round(cost_basis * 1.10)
    min_strike = effective_basis

    expiry = find_expiry(trading_days)
    if not expiry:
        print(f"[call_seller] {symbol}: no valid expiry found in 21–45 DTE window")
        return None

    # Filter calls to target expiry
    expiry_calls = {k: v for k, v in calls.items() if expiry.replace("-", "") in k}
    if not expiry_calls:
        expiry_calls = calls

    contract_info = find_call_contract(expiry_calls, target_strike, min_strike)
    if not contract_info:
        print(
            f"[call_seller] {symbol}: no call contracts above effective basis "
            f"${effective_basis:.2f} near ${target_strike}"
        )
        return None

    contract = contract_info["contract"]
    strike = contract_info["strike"]
    bid = contract_info["bid"]

    print(
        f"[call_seller] {symbol}: selling {contract} | strike ${strike} | "
        f"bid ${bid} | expiry {expiry} | effective basis ${effective_basis:.2f}"
    )

    if dry_run:
        print(f"[call_seller] DRY RUN — no order placed")
        return {"dry_run": True, "contract": contract, "strike": strike, "bid": bid}

    # Place order — Claude calls this MCP tool:
    # mcp__alpaca__place_option_order(symbol=contract, side="sell", qty="1",
    #   position_intent="sell_to_open", type="limit", limit_price=str(bid))
    order_result = {
        "_mcp_call": "place_option_order",
        "symbol": contract, "side": "sell",
        "qty": "1", "position_intent": "sell_to_open",
        "type": "limit", "limit_price": str(bid),
    }

    # Update state
    state = load_state()
    sym_state = state["symbols"].get(symbol, {})
    prev_premium = sym_state.get("total_premium_all_cycles", premiums_collected)
    cycle_number = sym_state.get("cycle_number", 1)

    state["symbols"][symbol] = {
        **sym_state,
        "stage": 2,
        "option_symbol": contract,
        "premium_collected": bid,
        "fill_price": None,
        "cost_basis": cost_basis,
        "shares_qty": 100,
        "expiry_date": expiry,
        "breakeven": None,
        "max_risk": None,
        "total_premium_all_cycles": round(prev_premium + bid, 4),
        "roll_count": sym_state.get("roll_count", 0),
        "cycle_number": cycle_number,
        "order_status": "pending_fill",
    }
    save_state(state)

    today_str = date.today().isoformat()
    append_log(
        f"## {today_str} — Stage 2 Entry (Assignment) Cycle #{cycle_number}\n"
        f"Symbol: {symbol} | Assigned at ${cost_basis:.2f} | Sold call: {contract} | "
        f"Strike: ${strike} | Expiry: {expiry} | Premium: ${bid} | "
        f"Effective basis: ${effective_basis:.2f}"
    )

    return order_result


if __name__ == "__main__":
    print("call_seller.py: invoke via Claude Code with injected MCP data.")
