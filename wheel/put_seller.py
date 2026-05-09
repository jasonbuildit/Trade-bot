"""
Wheel Strategy — Stage 1: Sell Cash-Secured Put
Finds the best put contract and places a sell-to-open limit order.

Guardrails enforced:
  - Duplicate guard: skip if active Stage 1 position already pending fill
  - Portfolio BP cap: total open puts + new put <= 50% of portfolio value
  - Min cash reserve: buying_power after trade >= 25% of portfolio value
  - Position size cap: strike × 100 <= max_position_pct × portfolio_value (default 10%)
  - Cash guard: non_marginable_buying_power >= strike × 100
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

from config import (
    DEFAULT_MAX_POSITION_PCT,
    MAX_BP_COMMITTED,
    MIN_CASH_RESERVE_PCT,
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
        ask = data.get("latestQuote", {}).get("ap", 0) or 0
        if bid <= 0:
            continue
        candidates.append({"contract": contract, "strike": strike, "bid": bid, "ask": ask, "data": data})

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
    # ── Duplicate position guard ──────────────────────────────────────────────
    state_check = load_state()
    existing_sym = state_check["symbols"].get(symbol, {})
    if existing_sym.get("stage") == 1 and existing_sym.get("order_status") == "pending_fill":
        print(
            f"[put_seller] {symbol}: Stage 1 position already pending fill "
            f"({existing_sym.get('option_symbol')}) — skipping duplicate entry"
        )
        return None

    target_strike = round(current_price * 0.90)
    expiry = find_expiry(trading_days)

    if not expiry:
        print(f"[put_seller] {symbol}: no valid expiry found in 21–45 DTE window")
        return None

    # Filter puts to target expiry
    expiry_puts = {k: v for k, v in puts.items() if expiry.replace("-", "")[2:] in k}
    if not expiry_puts:
        expiry_puts = puts

    contract_info = find_put_contract(expiry_puts, target_strike)
    if not contract_info:
        print(f"[put_seller] {symbol}: no put contracts with valid bid near ${target_strike}")
        return None

    contract = contract_info["contract"]
    strike = contract_info["strike"]
    bid = contract_info["bid"]
    ask = contract_info.get("ask", 0)
    limit_price = round((bid + ask) / 2, 2) if ask > bid else bid
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
            ask = fallback.get("ask", 0)
            limit_price = round((bid + ask) / 2, 2) if ask > bid else bid
            cash_required = strike * 100
            print(f"[put_seller] {symbol}: using lower strike ${strike} = ${cash_required:,.0f}")

    # ── Portfolio-level BP committed cap (50%) ────────────────────────────────
    if portfolio_value > 0:
        state_now = load_state()
        total_committed = sum(
            s.get("max_risk", 0) or 0
            for s in state_now["symbols"].values()
            if s.get("stage") == 1
        )
        if (total_committed + cash_required) / portfolio_value > MAX_BP_COMMITTED:
            print(
                f"[put_seller] {symbol}: total open puts ${total_committed:,.0f} + "
                f"new ${cash_required:,.0f} = {(total_committed + cash_required)/portfolio_value:.0%} "
                f"exceeds {MAX_BP_COMMITTED:.0%} portfolio cap — skipping"
            )
            return None

    # ── Min cash reserve (25%) ────────────────────────────────────────────────
    if portfolio_value > 0:
        min_reserve = portfolio_value * MIN_CASH_RESERVE_PCT
        if buying_power - cash_required < min_reserve:
            print(
                f"[put_seller] {symbol}: after trade, cash would drop below "
                f"{MIN_CASH_RESERVE_PCT:.0%} reserve (need ${min_reserve:,.0f} remaining) — skipping"
            )
            return None

    # ── Cash guard ────────────────────────────────────────────────────────────
    if buying_power < cash_required:
        print(
            f"[put_seller] {symbol}: insufficient cash "
            f"(need ${cash_required:,.0f}, have ${buying_power:,.0f})"
        )
        return None

    print(
        f"[put_seller] {symbol}: selling {contract} | strike ${strike} | "
        f"limit ${limit_price} (bid ${bid} / ask ${ask}) | expiry {expiry} | cash required ${cash_required:,.0f}"
    )

    if dry_run:
        print(f"[put_seller] DRY RUN — no order placed")
        return {"dry_run": True, "contract": contract, "strike": strike, "limit_price": limit_price}

    # Place order — Claude calls this MCP tool:
    # mcp__alpaca__place_option_order(symbol=contract, side="sell", qty="1",
    #   position_intent="sell_to_open", type="limit", limit_price=str(limit_price))
    order_result = {
        "_mcp_call": "place_option_order",
        "symbol": contract, "side": "sell",
        "qty": "1", "position_intent": "sell_to_open",
        "type": "limit", "limit_price": str(limit_price),
    }

    # Derived analytics (use limit_price as expected premium)
    breakeven = round(strike - limit_price, 2)
    max_risk = round((strike - limit_price) * 100, 2)

    # Update state
    state = load_state()
    today_str = date.today().isoformat()
    existing = state["symbols"].get(symbol, {})
    prev_premium = existing.get("total_premium_all_cycles", 0.0)
    cycle_number = existing.get("cycle_number", 0) + 1

    state["symbols"][symbol] = {
        "stage": 1,
        "option_symbol": contract,
        "premium_collected": limit_price,
        "fill_price": None,
        "entry_price": current_price,
        "cost_basis": None,
        "shares_qty": 0,
        "cycle_start": today_str,
        "expiry_date": expiry,
        "breakeven": breakeven,
        "max_risk": max_risk,
        "total_premium_all_cycles": round(prev_premium + limit_price, 4),
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
        f"Expiry: {expiry} | Limit: ${limit_price} (bid ${bid} / ask ${ask}) | Cash reserved: ${cash_required:,.0f} | "
        f"Breakeven: ${breakeven} | Max risk: ${max_risk:,.0f}"
    )

    return order_result


if __name__ == "__main__":
    print("put_seller.py: invoke via Claude Code with injected MCP data.")
