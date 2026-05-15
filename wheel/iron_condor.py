"""
Wheel Strategy — Tier 3: Sell Iron Condor

Neutral strategy for ETF-eligible symbols. Sells an OTM put spread + an OTM call spread
in the same expiry for a combined credit. Profits when underlying stays within range.

Guardrails enforced:
  - iron_condor_eligible: true required in watchlist (ETFs only — no earnings risk)
  - IV Rank >= IV_RANK_PREFERRED_MIN (premium must be rich enough)
  - Short put delta ≈ CONDOR_SHORT_DELTA below spot; short call delta ≈ CONDOR_SHORT_DELTA above
  - Wing width: CONDOR_WING_WIDTH strikes on each side
  - Net credit >= CONDOR_MIN_CREDIT_PCT × (CONDOR_WING_WIDTH × 100) per share
  - Portfolio BP cap including all strategy types

Called by: monitor.py dispatcher or directly by Claude after screener run.
Returns: mleg _mcp_call descriptor (4-leg) or None if no valid condor found.
"""
import json
from datetime import date
from pathlib import Path

ROOT = Path(__file__).parent.parent
STATE_FILE     = ROOT / "wheel" / "state.json"
LOG_FILE       = ROOT / "trades" / "wheel_log.md"
WATCHLIST_FILE = ROOT / "wheel" / "watchlist.json"

from config import (
    DELTA_MIN, DELTA_MAX, DTE_MIN, DTE_MAX,
    MAX_BP_COMMITTED,
    DEFAULT_MAX_POSITION_PCT,
    IV_RANK_PREFERRED_MIN,
    CONDOR_SHORT_DELTA,
    CONDOR_WING_WIDTH,
    CONDOR_MIN_CREDIT_PCT,
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


def _is_condor_eligible(symbol: str) -> bool:
    try:
        with open(WATCHLIST_FILE) as f:
            data = json.load(f)
        for item in data.get("symbols", []):
            if isinstance(item, dict) and item.get("symbol") == symbol:
                return bool(item.get("iron_condor_eligible", False))
    except Exception:
        pass
    return False


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
    """Sum of max_risk across all open strategies."""
    wheel   = sum(s.get("max_risk", 0) or 0 for s in state.get("symbols", {}).values() if s.get("stage") == 1)
    spreads = sum(s.get("max_risk", 0) or 0 for s in state.get("spread_positions", []))
    condors = sum(c.get("max_risk", 0) or 0 for c in state.get("condor_positions", []))
    return wheel + spreads + condors


def _find_leg(
    contracts: dict,
    expiry_tag: str,
    target_delta: float,
    delta_tol: float = 0.08,
) -> dict | None:
    """
    Find an OTM contract near target_delta with a positive bid.
    delta_tol: accepted range is [target_delta - tol, target_delta + tol]
    """
    candidates = []
    for contract, data in contracts.items():
        if expiry_tag not in contract:
            continue
        try:
            strike = parse_strike(contract)
        except Exception:
            continue
        delta = abs(data.get("greeks", {}).get("delta", 0) or 0)
        bid   = data.get("latestQuote", {}).get("bp", 0) or 0
        ask   = data.get("latestQuote", {}).get("ap", 0) or 0
        if bid <= 0:
            continue
        if abs(delta - target_delta) <= delta_tol:
            candidates.append({"contract": contract, "strike": strike, "bid": bid, "ask": ask, "delta": delta})

    if not candidates:
        return None
    candidates.sort(key=lambda x: abs(x["delta"] - target_delta))
    return candidates[0]


def _find_protection_leg(
    contracts: dict,
    expiry_tag: str,
    short_strike: float,
    wing_width: float,
    side: str,
) -> dict | None:
    """Find protection (long) leg CONDOR_WING_WIDTH away from short leg."""
    if side == "put":
        target_strike = short_strike - wing_width
    else:
        target_strike = short_strike + wing_width

    for contract, data in contracts.items():
        if expiry_tag not in contract:
            continue
        try:
            strike = parse_strike(contract)
        except Exception:
            continue
        if strike != target_strike:
            continue
        ask = data.get("latestQuote", {}).get("ap", 0) or 0
        bid = data.get("latestQuote", {}).get("bp", 0) or 0
        if ask > 0:
            return {"contract": contract, "strike": strike, "bid": bid, "ask": ask}
    return None


def run(
    symbol: str,
    current_price: float,
    puts: dict,
    calls: dict,
    trading_days: list,
    portfolio_value: float = 0,
    iv_rank: float | None = None,
    dry_run: bool = False,
) -> dict | None:
    """
    symbol:          ticker (must be iron_condor_eligible in watchlist)
    current_price:   latest spot price
    puts:            option chain puts dict {contract: snapshot}
    calls:           option chain calls dict {contract: snapshot}
    trading_days:    calendar list from mcp__alpaca__get_calendar
    portfolio_value: total portfolio value for cap checks (0 = skip)
    iv_rank:         current IV rank (0–1); must be >= IV_RANK_PREFERRED_MIN
    dry_run:         if True, no state written, no MCP call placed

    Returns: mleg _mcp_call dict (4-leg) or None
    """
    # ── ETF eligibility gate ───────────────────────────────────────────────────
    if not _is_condor_eligible(symbol):
        print(f"[iron_condor] {symbol}: not iron_condor_eligible — skipping")
        return None

    # ── IV Rank gate ───────────────────────────────────────────────────────────
    if iv_rank is not None and iv_rank < IV_RANK_PREFERRED_MIN:
        print(
            f"[iron_condor] {symbol}: IV rank {iv_rank:.0%} < "
            f"{IV_RANK_PREFERRED_MIN:.0%} preferred minimum — skipping"
        )
        return None

    # ── Duplicate guard ────────────────────────────────────────────────────────
    state_check = load_state()
    if any(
        c.get("symbol") == symbol and c.get("order_status") == "pending_fill"
        for c in state_check.get("condor_positions", [])
    ):
        print(f"[iron_condor] {symbol}: condor already pending fill — skipping")
        return None

    expiry = find_expiry(trading_days, DTE_MIN, DTE_MAX)
    if not expiry:
        print(f"[iron_condor] {symbol}: no valid expiry in {DTE_MIN}–{DTE_MAX} DTE window")
        return None

    tag = _expiry_tag(expiry)

    # ── Find four legs ─────────────────────────────────────────────────────────
    short_put  = _find_leg(puts,  tag, CONDOR_SHORT_DELTA)
    short_call = _find_leg(calls, tag, CONDOR_SHORT_DELTA)
    if not short_put or not short_call:
        print(f"[iron_condor] {symbol}: no short put/call near delta {CONDOR_SHORT_DELTA:.2f}")
        return None

    long_put  = _find_protection_leg(puts,  tag, short_put["strike"],  CONDOR_WING_WIDTH, "put")
    long_call = _find_protection_leg(calls, tag, short_call["strike"], CONDOR_WING_WIDTH, "call")
    if not long_put or not long_call:
        print(f"[iron_condor] {symbol}: no protection legs at ±{CONDOR_WING_WIDTH} width")
        return None

    # ── Credit calculation ─────────────────────────────────────────────────────
    put_credit  = round(short_put["bid"]  - long_put["ask"],  2)
    call_credit = round(short_call["bid"] - long_call["ask"], 2)
    net_credit  = round(put_credit + call_credit, 2)
    max_risk    = round((CONDOR_WING_WIDTH * 100) - (net_credit * 100), 2)

    # ── Minimum credit gate ────────────────────────────────────────────────────
    min_credit = CONDOR_MIN_CREDIT_PCT * CONDOR_WING_WIDTH  # per share
    if net_credit < min_credit:
        print(
            f"[iron_condor] {symbol}: net credit ${net_credit:.2f} < "
            f"minimum ${min_credit:.2f} — skipping"
        )
        return None

    # ── Position size cap ──────────────────────────────────────────────────────
    if portfolio_value > 0:
        max_pct = _max_position_pct(symbol)
        if max_risk > portfolio_value * max_pct:
            print(
                f"[iron_condor] {symbol}: max_risk ${max_risk:.0f} exceeds "
                f"{max_pct:.0%} position cap — skipping"
            )
            return None

    # ── Portfolio BP committed cap ─────────────────────────────────────────────
    if portfolio_value > 0:
        state_now = load_state()
        committed = _total_committed(state_now)
        if (committed + max_risk) / portfolio_value > MAX_BP_COMMITTED:
            print(
                f"[iron_condor] {symbol}: total committed ${committed:,.0f} + "
                f"new ${max_risk:,.0f} = {(committed+max_risk)/portfolio_value:.0%} "
                f"exceeds {MAX_BP_COMMITTED:.0%} portfolio cap — skipping"
            )
            return None

    print(
        f"[iron_condor] {symbol}: condor "
        f"put spread ${short_put['strike']}/${long_put['strike']} | "
        f"call spread ${short_call['strike']}/${long_call['strike']} | "
        f"net credit ${net_credit:.2f} | max_risk ${max_risk:,.0f} | expiry {expiry}"
    )

    if dry_run:
        return {
            "dry_run": True,
            "short_put_contract":  short_put["contract"],
            "long_put_contract":   long_put["contract"],
            "short_call_contract": short_call["contract"],
            "long_call_contract":  long_call["contract"],
            "net_credit": net_credit,
            "max_risk":   max_risk,
        }

    order_result = {
        "_mcp_call": "place_option_order",
        "order_class": "mleg",
        "qty": "1",
        "type": "limit",
        "limit_price": str(-net_credit),
        "legs": [
            {"symbol": short_put["contract"],  "ratio_qty": "1", "side": "sell", "position_intent": "sell_to_open"},
            {"symbol": long_put["contract"],   "ratio_qty": "1", "side": "buy",  "position_intent": "buy_to_open"},
            {"symbol": short_call["contract"], "ratio_qty": "1", "side": "sell", "position_intent": "sell_to_open"},
            {"symbol": long_call["contract"],  "ratio_qty": "1", "side": "buy",  "position_intent": "buy_to_open"},
        ],
        "_meta": {
            "action": "open_iron_condor",
            "symbol": symbol,
            "short_put_contract":  short_put["contract"],
            "long_put_contract":   long_put["contract"],
            "short_call_contract": short_call["contract"],
            "long_call_contract":  long_call["contract"],
            "net_credit": net_credit,
            "max_risk":   max_risk,
        },
    }

    today_str   = date.today().isoformat()
    position_id = (
        f"{symbol}-condor-{expiry.replace('-','')}"
        f"-{int(short_put['strike'])}-{int(short_call['strike'])}"
    )

    state = load_state()
    if "condor_positions" not in state:
        state["condor_positions"] = []

    state["condor_positions"].append({
        "id": position_id,
        "symbol": symbol,
        "strategy": "iron_condor",
        "stage": "open",
        "short_put_contract":  short_put["contract"],
        "long_put_contract":   long_put["contract"],
        "short_call_contract": short_call["contract"],
        "long_call_contract":  long_call["contract"],
        "short_put_strike":  short_put["strike"],
        "long_put_strike":   long_put["strike"],
        "short_call_strike": short_call["strike"],
        "long_call_strike":  long_call["strike"],
        "wing_width":  CONDOR_WING_WIDTH,
        "net_credit":  net_credit,
        "max_risk":    max_risk,
        "expiry_date": expiry,
        "cycle_start": today_str,
        "order_status": "pending_fill",
        "adjustment_count": 0,
        "entry_iv_rank": round(iv_rank, 4) if iv_rank is not None else None,
        "entry_delta_short_put":  round(short_put["delta"],  3),
        "entry_delta_short_call": round(short_call["delta"], 3),
    })
    save_state(state)

    append_log(
        f"## {today_str} — Iron Condor Opened\n"
        f"Symbol: {symbol} | "
        f"Put spread: {short_put['contract']} / {long_put['contract']} | "
        f"Call spread: {short_call['contract']} / {long_call['contract']} | "
        f"Expiry: {expiry} | Net credit: ${net_credit:.2f} | Max risk: ${max_risk:,.0f}"
        + (f" | IV rank: {iv_rank:.0%}" if iv_rank is not None else "")
    )

    return order_result


if __name__ == "__main__":
    print("iron_condor.py: invoke via Claude Code with injected MCP data.")
