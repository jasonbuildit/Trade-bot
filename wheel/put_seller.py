"""
Wheel Strategy — Stage 1: Sell Cash-Secured Put
Finds the best put contract and places a sell-to-open limit order.

Guardrails enforced:
  - Cash guard: non_marginable_buying_power >= strike × 100
  - Position size cap: strike × 100 <= max_position_pct × portfolio_value (default 10%)
  - Earnings gate: delegated to screener; put_seller trusts the contract passed in
  - DTE window: 21–45 (theta decay sweet spot)

Called by: monitor.py (on new cycle start) or manually by Claude.
MCP tools used: get_account_info, get_option_chain, get_calendar, place_option_order
"""
import json
from datetime import date
from pathlib import Path

ROOT = Path(__file__).parent.parent
STATE_FILE = ROOT / "wheel" / "state.json"
LOG_FILE = ROOT / "trades" / "wheel_log.md"
WATCHLIST_FILE = ROOT / "wheel" / "watchlist.json"

DEFAULT_MAX_POSITION_PCT = 0.10  # 10% of portfolio per position


def load_state() -> dict:
    with open(STATE_FILE) as f:
        return json.load(f)


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def append_log(entry: str):
    with open(LOG_FILE, "a") as f:
        f.write(f"\n{entry}\n")


def _max_position_pct(symbol: str) -> float:
    try:
        with open(WATCHLIST_FILE) as f:
            data = json.load(f)
        for item in data.get("symbols", []):
            if isinstance(item, dict) and item.get("symbol") == symbol:
                return item.get("max_position_pct", DEFAULT_MAX_POSITION_PCT)
    except Exception:
        pass
    return DEFAULT_MAX_POSITION_PCT


def find_expiry(trading_days: list, min_dte: int = 21, max_dte: int = 45) -> str | None:
    today = date.today()
    # Prefer a Friday
    for day in trading_days:
        d = date.fromisoformat(day["date"])
        delta = (d - today).days
        if min_dte <= delta <= max_dte and d.weekday() == 4:
            return day["date"]
    # Fallback: any trading day in range
    for day in trading_days:
        d = date.fromisoformat(day["date"])
        delta = (d - today).days
        if min_dte <= delta <= max_dte:
            return day["date"]
    return None


def find_put_contract(puts: dict, target_strike: float) -> dict | None:
    """
    puts: {contract_symbol: snapshot}
    Returns best contract near target_strike with a valid bid.
    """
    candidates = []
    for contract, data in puts.items():
        try:
            strike = int(contract[-8:]) / 1000
        except Exception:
            continue
        bid = data.get("latestQuote", {}).get("bp", 0) or 0
        if bid <= 0:
            continue
        candidates.append({"contract": contract, "strike": strike, "bid": bid, "data": data})

    if not candidates:
        return None

    candidates.sort(key=lambda x: abs(x["strike"] - target_strike))
    return candidates[0]


def run(
    symbol: str,
    current_price: float,
    buying_power: float,
    puts: dict,
    trading_days: list,
    portfolio_value: float = 0,
    dry_run: bool = False,
) -> dict | None:
    """
    symbol:          ticker
    current_price:   latest trade/quote price
    buying_power:    non_marginable_buying_power from account
    puts:            option chain puts dict from mcp__alpaca__get_option_chain
    trading_days:    calendar list from mcp__alpaca__get_calendar
    portfolio_value: total portfolio value for position size cap (0 = skip cap check)
    dry_run:         if True, logs intent but does not place order

    Returns: order details dict or None
    """
    target_strike = round(current_price * 0.90)
    expiry = find_expiry(trading_days)

    if not expiry:
        print(f"[put_seller] {symbol}: no valid expiry found in 21–45 DTE window")
        return None

    # Filter puts to target expiry
    expiry_puts = {k: v for k, v in puts.items() if expiry.replace("-", "") in k}
    if not expiry_puts:
        expiry_puts = puts

    contract_info = find_put_contract(expiry_puts, target_strike)
    if not contract_info:
        print(f"[put_seller] {symbol}: no put contracts with valid bid near ${target_strike}")
        return None

    contract = contract_info["contract"]
    strike = contract_info["strike"]
    bid = contract_info["bid"]
    cash_required = strike * 100

    # ── Position size cap ─────────────────────────────────────────────────────
    if portfolio_value > 0:
        max_pct = _max_position_pct(symbol)
        max_cash = portfolio_value * max_pct
        if cash_required > max_cash:
            # Walk down strikes to find one that fits
            print(
                f"[put_seller] {symbol}: strike ${strike} = ${cash_required:,.0f} exceeds "
                f"{max_pct*100:.0f}% cap (${max_cash:,.0f}) — trying lower strikes"
            )
            fallback = find_put_contract(
                {k: v for k, v in expiry_puts.items()
                 if int(k[-8:]) / 1000 * 100 <= max_cash},
                target_strike,
            )
            if not fallback:
                print(f"[put_seller] {symbol}: no strike fits within position size cap — skipping")
                return None
            contract = fallback["contract"]
            strike = fallback["strike"]
            bid = fallback["bid"]
            cash_required = strike * 100
            print(f"[put_seller] {symbol}: using lower strike ${strike} = ${cash_required:,.0f}")

    # ── Cash guard ────────────────────────────────────────────────────────────
    if buying_power < cash_required:
        print(
            f"[put_seller] {symbol}: insufficient cash "
            f"(need ${cash_required:,.0f}, have ${buying_power:,.0f})"
        )
        return None

    print(
        f"[put_seller] {symbol}: selling {contract} | strike ${strike} | "
        f"bid ${bid} | expiry {expiry} | cash required ${cash_required:,.0f}"
    )

    if dry_run:
        print(f"[put_seller] DRY RUN — no order placed")
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

    # Derived analytics
    breakeven = round(strike - bid, 2)
    max_risk = round((strike - bid) * 100, 2)

    # Update state
    state = load_state()
    today_str = date.today().isoformat()
    existing = state["symbols"].get(symbol, {})
    prev_premium = existing.get("total_premium_all_cycles", 0.0)
    cycle_number = existing.get("cycle_number", 0) + 1

    state["symbols"][symbol] = {
        "stage": 1,
        "option_symbol": contract,
        "premium_collected": bid,
        "fill_price": None,
        "cost_basis": None,
        "shares_qty": 0,
        "cycle_start": today_str,
        "expiry_date": expiry,
        "breakeven": breakeven,
        "max_risk": max_risk,
        "total_premium_all_cycles": round(prev_premium + bid, 4),
        "roll_count": 0,
        "cycle_number": cycle_number,
        "order_status": "pending_fill",
    }
    state["account_summary"]["total_premium_collected"] = round(
        sum(s.get("total_premium_all_cycles", 0) for s in state["symbols"].values()), 4
    )
    save_state(state)

    append_log(
        f"## {today_str} — Stage 1 Entry (Cycle #{cycle_number})\n"
        f"Symbol: {symbol} | Sold put: {contract} | Strike: ${strike} | "
        f"Expiry: {expiry} | Premium: ${bid} | Cash reserved: ${cash_required:,.0f} | "
        f"Breakeven: ${breakeven} | Max risk: ${max_risk:,.0f}"
    )

    return order_result


if __name__ == "__main__":
    print("put_seller.py: invoke via Claude Code with injected MCP data.")
