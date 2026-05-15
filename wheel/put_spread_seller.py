"""
Wheel Strategy — Tier 2: Sell Put Credit Spread (defined-risk CSP alternative)

Sells an OTM put and buys a lower-strike put for protection.
Capital requirement: (spread_width × 100) - net_credit  vs  strike × 100 for naked CSP.
~30× more capital-efficient for same delta exposure.

Guardrails enforced:
  - Same delta range as naked CSP (DELTA_MIN to DELTA_MAX)
  - Net credit >= SPREAD_MIN_CREDIT_PCT × spread_width × 100
  - Portfolio BP cap including all strategy types
  - Duplicate guard: no second spread opened while one is pending_fill

Called by: monitor.py dispatcher or directly by Claude after screener run.
Returns: mleg _mcp_call descriptor (2-leg) or None if no valid spread found.
"""
import json
from datetime import date
from pathlib import Path

ROOT = Path(__file__).parent.parent
STATE_FILE  = ROOT / "wheel" / "state.json"
LOG_FILE    = ROOT / "trades" / "wheel_log.md"
WATCHLIST_FILE = ROOT / "wheel" / "watchlist.json"

from config import (
    DELTA_MIN, DELTA_MAX, DTE_MIN, DTE_MAX,
    MAX_BP_COMMITTED,
    DEFAULT_MAX_POSITION_PCT,
    SPREAD_WIDTH_DEFAULT,
    SPREAD_MIN_CREDIT_PCT,
)
from occ import parse_strike, find_expiry, expiry_tag as _expiry_tag


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


def _total_committed(state: dict) -> float:
    """Sum of max_risk across all open strategies (wheel puts + spreads + condors)."""
    wheel = sum(
        s.get("max_risk", 0) or 0
        for s in state.get("symbols", {}).values()
        if s.get("stage") == 1
    )
    spreads = sum(s.get("max_risk", 0) or 0 for s in state.get("spread_positions", []))
    condors = sum(c.get("max_risk", 0) or 0 for c in state.get("condor_positions", []))
    return wheel + spreads + condors


def find_spread_contracts(
    puts: dict,
    short_strike: float,
    long_strike: float,
    expiry: str,
) -> dict | None:
    """
    Find short_contract (near short_strike) and long_contract (near long_strike)
    in the given expiry. Returns {short, long} info or None.
    """
    tag = _expiry_tag(expiry)
    short_info = long_info = None

    for contract, data in puts.items():
        if tag not in contract:
            continue
        try:
            s = parse_strike(contract)
        except Exception:
            continue
        bid = data.get("latestQuote", {}).get("bp", 0) or 0
        ask = data.get("latestQuote", {}).get("ap", 0) or 0
        delta = abs(data.get("greeks", {}).get("delta", 0) or 0)

        if s == short_strike and bid > 0 and DELTA_MIN <= delta <= DELTA_MAX:
            short_info = {"contract": contract, "strike": s, "bid": bid, "ask": ask, "delta": delta}
        if s == long_strike and ask > 0:
            long_info = {"contract": contract, "strike": s, "bid": bid, "ask": ask}

    if not short_info or not long_info:
        return None
    return {"short": short_info, "long": long_info}


def run(
    symbol: str,
    current_price: float,
    puts: dict,
    trading_days: list,
    portfolio_value: float = 0,
    iv_rank: float | None = None,
    dry_run: bool = False,
) -> dict | None:
    """
    symbol:          ticker
    current_price:   latest trade/quote price
    puts:            option chain puts dict {contract: snapshot}
    trading_days:    calendar list from mcp__alpaca__get_calendar
    portfolio_value: total portfolio value for cap checks (0 = skip)
    iv_rank:         current IV rank (0–1) for logging only
    dry_run:         if True, no MCP call placed

    Returns: mleg _mcp_call dict or None
    """
    # ── Duplicate guard ───────────────────────────────────────────────────────
    state_check = load_state()
    existing_spreads = state_check.get("spread_positions", [])
    if any(s.get("symbol") == symbol and s.get("order_status") == "pending_fill"
           for s in existing_spreads):
        print(f"[spread_seller] {symbol}: spread already pending fill — skipping")
        return None

    expiry = find_expiry(trading_days, DTE_MIN, DTE_MAX)
    if not expiry:
        print(f"[spread_seller] {symbol}: no valid expiry in {DTE_MIN}–{DTE_MAX} DTE window")
        return None

    # Target short strike: ~10% below spot (same as put_seller)
    short_strike = round(current_price * 0.90)
    long_strike  = short_strike - SPREAD_WIDTH_DEFAULT

    info = find_spread_contracts(puts, short_strike, long_strike, expiry)
    if not info:
        # Walk down in $5 increments to find a valid pair
        for offset in range(5, 26, 5):
            short_strike = round(current_price * 0.90) - offset
            long_strike  = short_strike - SPREAD_WIDTH_DEFAULT
            info = find_spread_contracts(puts, short_strike, long_strike, expiry)
            if info:
                break

    if not info:
        print(
            f"[spread_seller] {symbol}: no valid put spread found "
            f"(target short ${round(current_price*0.90)}, width ${SPREAD_WIDTH_DEFAULT})"
        )
        return None

    short_contract = info["short"]["contract"]
    long_contract  = info["long"]["contract"]
    short_bid  = info["short"]["bid"]
    long_ask   = info["long"]["ask"]
    net_credit = round(short_bid - long_ask, 2)
    max_risk   = round((SPREAD_WIDTH_DEFAULT * 100) - (net_credit * 100), 2)

    # ── Minimum credit gate ───────────────────────────────────────────────────
    min_credit = SPREAD_MIN_CREDIT_PCT * SPREAD_WIDTH_DEFAULT * 100 / 100  # per share
    if net_credit < min_credit:
        print(
            f"[spread_seller] {symbol}: net credit ${net_credit:.2f} < "
            f"minimum ${min_credit:.2f} — skipping"
        )
        return None

    # ── Position size cap ─────────────────────────────────────────────────────
    if portfolio_value > 0:
        max_pct = _max_position_pct(symbol)
        if max_risk > portfolio_value * max_pct:
            print(
                f"[spread_seller] {symbol}: max_risk ${max_risk:.0f} exceeds "
                f"{max_pct:.0%} position cap (${portfolio_value*max_pct:,.0f}) — skipping"
            )
            return None

    # ── Portfolio BP committed cap (50%) ──────────────────────────────────────
    if portfolio_value > 0:
        state_now = load_state()
        committed = _total_committed(state_now)
        if (committed + max_risk) / portfolio_value > MAX_BP_COMMITTED:
            print(
                f"[spread_seller] {symbol}: total committed ${committed:,.0f} + "
                f"new ${max_risk:,.0f} = {(committed+max_risk)/portfolio_value:.0%} "
                f"exceeds {MAX_BP_COMMITTED:.0%} portfolio cap — skipping"
            )
            return None

    print(
        f"[spread_seller] {symbol}: spread {short_contract} / {long_contract} | "
        f"short ${short_strike} / long ${long_strike} | "
        f"net credit ${net_credit:.2f} | max_risk ${max_risk:,.0f} | expiry {expiry}"
    )

    if dry_run:
        return {
            "dry_run": True,
            "short_contract": short_contract,
            "long_contract": long_contract,
            "net_credit": net_credit,
            "max_risk": max_risk,
        }

    order_result = {
        "_mcp_call": "place_option_order",
        "order_class": "mleg",
        "qty": "1",
        "type": "limit",
        "limit_price": str(-net_credit),  # negative = credit received
        "legs": [
            {
                "symbol": short_contract,
                "ratio_qty": "1",
                "side": "sell",
                "position_intent": "sell_to_open",
            },
            {
                "symbol": long_contract,
                "ratio_qty": "1",
                "side": "buy",
                "position_intent": "buy_to_open",
            },
        ],
        "_meta": {
            "action": "open_put_spread",
            "symbol": symbol,
            "short_contract": short_contract,
            "long_contract": long_contract,
            "net_credit": net_credit,
            "max_risk": max_risk,
        },
    }

    # ── Update state ──────────────────────────────────────────────────────────
    today_str = date.today().isoformat()
    position_id = f"{symbol}-put-spread-{expiry.replace('-','')}-{int(short_strike)}-{int(long_strike)}"

    state = load_state()
    if "spread_positions" not in state:
        state["spread_positions"] = []

    state["spread_positions"].append({
        "id": position_id,
        "symbol": symbol,
        "strategy": "put_spread",
        "stage": "open",
        "short_contract": short_contract,
        "long_contract": long_contract,
        "short_strike": short_strike,
        "long_strike": long_strike,
        "spread_width": SPREAD_WIDTH_DEFAULT,
        "net_credit": net_credit,
        "max_risk": max_risk,
        "expiry_date": expiry,
        "cycle_start": today_str,
        "order_status": "pending_fill",
        "adjustment_count": 0,
        "entry_iv_rank": round(iv_rank, 4) if iv_rank is not None else None,
        "entry_delta_short": round(info["short"]["delta"], 3),
    })
    save_state(state)

    append_log(
        f"## {today_str} — Put Spread Opened\n"
        f"Symbol: {symbol} | Short: {short_contract} (${short_strike}) | "
        f"Long: {long_contract} (${long_strike}) | "
        f"Expiry: {expiry} | Net credit: ${net_credit:.2f} | Max risk: ${max_risk:,.0f}"
        + (f" | IV rank: {iv_rank:.0%}" if iv_rank is not None else "")
    )

    return order_result


if __name__ == "__main__":
    print("put_spread_seller.py: invoke via Claude Code with injected MCP data.")
