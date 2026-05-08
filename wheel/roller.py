"""
Wheel Strategy — Rolling Module

Handles two rolling scenarios:
  roll_put_down_and_out  — put approaching ITM: roll to lower strike / further expiry for credit
  roll_call_up_and_out   — covered call deeply ITM: roll to higher strike for credit

Rolling rules:
  - Only roll for a NET CREDIT >= $0.05 (never pay a debit to roll)
  - Prefer same-strike / next-expiry first (pure time roll = less risk change)
  - If no credit at same strike, try 1-2 strikes lower (for puts) or higher (for calls)
  - If no credit possible: return None — let monitor handle it (don't force a bad roll)

Called by: monitor.py when roll trigger conditions are met.
MCP tools used (by Claude): place_option_order with order_class="mleg"
"""
from datetime import date
from pathlib import Path


def _find_expiry(trading_days: list, min_dte: int = 21, max_dte: int = 45) -> str | None:
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


def _parse_strike(contract: str) -> float:
    return int(contract[-8:]) / 1000


def _parse_expiry(contract: str, symbol: str) -> str:
    exp = "20" + contract[len(symbol):len(symbol)+6]
    return f"{exp[:4]}-{exp[4:6]}-{exp[6:8]}"


def roll_put_down_and_out(
    symbol: str,
    current_contract: str,
    current_bid: float,
    puts: dict,
    trading_days: list,
    min_credit: float = 0.05,
) -> dict | None:
    """
    Close current short put and open a new put at same or lower strike, further expiry.

    Returns mleg order dict for Claude to execute, or None if no credit roll is possible.

    current_contract: OCC symbol of the existing short put
    current_bid:      current ask price on current contract (cost to buy it back)
    puts:             full put chain for the symbol {contract: snapshot}
    trading_days:     from get_calendar
    min_credit:       minimum net credit required (default $0.05)
    """
    current_strike = _parse_strike(current_contract)
    current_expiry = _parse_expiry(current_contract, symbol)

    # Find new expiry: must be later than current expiry AND in 21-45 DTE window
    new_expiry = _find_expiry(trading_days)
    if not new_expiry or new_expiry <= current_expiry:
        print(f"[roller] {symbol}: no later expiry found in 21-45 DTE — cannot roll")
        return None

    expiry_tag = new_expiry.replace("-", "")

    # Candidate replacement contracts: same or lower strike, new expiry, valid bid
    candidates = []
    for contract, data in puts.items():
        if expiry_tag not in contract:
            continue
        try:
            strike = _parse_strike(contract)
        except Exception:
            continue
        if strike > current_strike:
            continue  # only same or lower strike
        bid = (data.get("latestQuote", {}).get("bp", 0) or 0)
        if bid <= 0:
            continue
        candidates.append({"contract": contract, "strike": strike, "bid": bid})

    if not candidates:
        print(f"[roller] {symbol}: no replacement put contracts found for roll")
        return None

    # Prefer same strike first, then next lower
    candidates.sort(key=lambda x: (abs(x["strike"] - current_strike), -x["strike"]))

    for cand in candidates:
        # Net credit = new put bid - current put ask (cost to close)
        net_credit = round(cand["bid"] - current_bid, 2)
        if net_credit >= min_credit:
            print(
                f"[roller] {symbol}: rolling put {current_contract} → {cand['contract']} | "
                f"net credit ${net_credit:.2f} | new strike ${cand['strike']} | expiry {new_expiry}"
            )
            return {
                "_mcp_call": "place_option_order",
                "order_class": "mleg",
                "qty": "1",
                "type": "limit",
                "limit_price": str(-net_credit),  # negative = credit
                "legs": [
                    {
                        "symbol": current_contract,
                        "ratio_qty": "1",
                        "side": "buy",
                        "position_intent": "buy_to_close",
                    },
                    {
                        "symbol": cand["contract"],
                        "ratio_qty": "1",
                        "side": "sell",
                        "position_intent": "sell_to_open",
                    },
                ],
                "_meta": {
                    "action": "roll_put_down_and_out",
                    "symbol": symbol,
                    "old_contract": current_contract,
                    "new_contract": cand["contract"],
                    "net_credit": net_credit,
                },
            }

    print(
        f"[roller] {symbol}: no roll available for credit >= ${min_credit:.2f} — holding"
    )
    return None


def roll_call_up_and_out(
    symbol: str,
    current_contract: str,
    current_bid: float,
    calls: dict,
    trading_days: list,
    effective_basis: float,
    min_credit: float = 0.05,
) -> dict | None:
    """
    Close current short call and open a new call at a higher strike, further expiry.

    Returns mleg order dict for Claude to execute, or None if no credit roll is possible.

    current_contract: OCC symbol of the existing short call
    current_bid:      current ask price on current contract (cost to buy it back)
    calls:            full call chain for the symbol {contract: snapshot}
    trading_days:     from get_calendar
    effective_basis:  cost_basis - premiums_collected (new strike must stay above this)
    min_credit:       minimum net credit required (default $0.05)
    """
    current_strike = _parse_strike(current_contract)
    current_expiry = _parse_expiry(current_contract, symbol)

    new_expiry = _find_expiry(trading_days)
    if not new_expiry or new_expiry <= current_expiry:
        print(f"[roller] {symbol}: no later expiry found in 21-45 DTE — cannot roll call")
        return None

    expiry_tag = new_expiry.replace("-", "")

    # Candidate replacement contracts: higher strike, above effective_basis, new expiry
    candidates = []
    for contract, data in calls.items():
        if expiry_tag not in contract:
            continue
        try:
            strike = _parse_strike(contract)
        except Exception:
            continue
        if strike <= current_strike:
            continue  # only higher strikes
        if strike < effective_basis:
            continue  # never sell call below effective basis
        bid = (data.get("latestQuote", {}).get("bp", 0) or 0)
        if bid <= 0:
            continue
        candidates.append({"contract": contract, "strike": strike, "bid": bid})

    if not candidates:
        print(f"[roller] {symbol}: no higher-strike call candidates found for roll")
        return None

    # Prefer nearest strike above current
    candidates.sort(key=lambda x: x["strike"])

    for cand in candidates:
        net_credit = round(cand["bid"] - current_bid, 2)
        if net_credit >= min_credit:
            print(
                f"[roller] {symbol}: rolling call {current_contract} → {cand['contract']} | "
                f"net credit ${net_credit:.2f} | new strike ${cand['strike']} | expiry {new_expiry}"
            )
            return {
                "_mcp_call": "place_option_order",
                "order_class": "mleg",
                "qty": "1",
                "type": "limit",
                "limit_price": str(-net_credit),
                "legs": [
                    {
                        "symbol": current_contract,
                        "ratio_qty": "1",
                        "side": "buy",
                        "position_intent": "buy_to_close",
                    },
                    {
                        "symbol": cand["contract"],
                        "ratio_qty": "1",
                        "side": "sell",
                        "position_intent": "sell_to_open",
                    },
                ],
                "_meta": {
                    "action": "roll_call_up_and_out",
                    "symbol": symbol,
                    "old_contract": current_contract,
                    "new_contract": cand["contract"],
                    "net_credit": net_credit,
                },
            }

    print(
        f"[roller] {symbol}: no call roll available for credit >= ${min_credit:.2f} — holding"
    )
    return None


if __name__ == "__main__":
    print("roller.py: invoke via Claude Code with injected MCP data.")
