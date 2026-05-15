"""
Smoke tests for monitor.run() -full-path scenarios with dry_run=True.
No live MCP calls, no file I/O. Run after any commit to monitor.py.

Usage:
    cd C:\\workspace\\Trade-bot\\wheel
    python smoke_test.py
"""
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))

import monitor
import spread_monitor
import condor_monitor
from tests.fixtures import (
    TICKER, PUT_SYM, CALL_SYM, EXPIRY_OCC, EXPIRY_ISO,
    ACCOUNT, CLOCK_OPEN, CLOCK_CLOSED, SNAPSHOT,
    make_state, put_symbol_state, call_symbol_state,
    short_put_position, short_call_position, share_position,
    put_chain, call_chain, make_trading_days,
)

TRADING_DAYS = make_trading_days()
CHAINS = {"AAPL": {"puts": put_chain(strike=90), "calls": call_chain(strike=290)}}

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
_failures = []


def run_scenario(state, positions, open_orders=None, clock=CLOCK_OPEN, account=None, option_chains=None):
    with (
        patch("monitor.load_state", return_value=state),
        patch("monitor.save_state"),
        patch("monitor.append_log"),
        patch("put_seller.run", return_value={"dry_run": True}),
        patch("call_seller.run", return_value={"dry_run": True}),
        patch("put_seller.load_state", return_value=state),
        patch("put_seller.save_state"),
        patch("put_seller.append_log"),
        patch("call_seller.load_state", return_value=state),
        patch("call_seller.save_state"),
        patch("call_seller.append_log"),
    ):
        return monitor.run(
            clock=clock,
            positions=positions or [],
            open_orders=open_orders or [],
            account=account or ACCOUNT,
            option_chains=option_chains or CHAINS,
            trading_days=TRADING_DAYS,
            snapshots=SNAPSHOT,
            dry_run=True,
        )


def check(name, condition, detail=""):
    if condition:
        print(f"  {PASS}  {name}")
    else:
        print(f"  {FAIL}  {name}" + (f" -{detail}" if detail else ""))
        _failures.append(name)


def scenario(label):
    print(f"\n{label}")


# ── Market closed guard ───────────────────────────────────────────────────────

scenario("1. Market closed -> early exit")
result = run_scenario(make_state(), [], clock=CLOCK_CLOSED)
check("status is market_closed", result["status"] == "market_closed")
check("no actions returned", result["actions"] == [])


# ── Strategy disabled (12% drawdown) ─────────────────────────────────────────

scenario("2. Portfolio drawdown 13% -> strategy disabled")
result = run_scenario(
    make_state(peak_equity=100_000),
    positions=[],
    account={**ACCOUNT, "equity": "87000"},  # 13% below peak
)
check("status is strategy_disabled", result["status"] == "strategy_disabled")
check("drawdown > 12%", result.get("drawdown", 0) >= 0.12)


# ── Fill reconciliation: expired order clears state ───────────────────────────

scenario("3. Pending order not in positions or open orders -> state cleared")
state = make_state({"AAPL": put_symbol_state(order_status="pending_fill")})
result = run_scenario(state, positions=[], open_orders=[])
# After reconciliation AAPL is removed -no actions for it
actions = result.get("actions", [])
check("no assignment action", not any(a.get("action") == "assigned_to_stage2" for a in actions))
check("status ok", result["status"] == "ok")


# ── Fill reconciliation: pending fill confirmed ────────────────────────────────

scenario("4. Pending order found in positions -> marked filled, proceeds to profit check")
state = make_state({"AAPL": put_symbol_state(order_status="pending_fill")})
# option is in positions (filled) at 50% profit
opt_pos = short_put_position(unrealized_pl=48.0, cost_basis=-96.0, market_value=-48.0)
result = run_scenario(state, positions=[opt_pos])
actions = result.get("actions", [])
check("50pct profit action fired", any(a.get("action") == "50pct_close_put" for a in actions),
      f"got {[a.get('action') for a in actions]}")


# ── 50% profit on short put ───────────────────────────────────────────────────

scenario("5. Stage 1 put at 50% profit -> close and re-sell")
state = make_state({"AAPL": put_symbol_state(order_status="filled")})
opt_pos = short_put_position(unrealized_pl=48.0, cost_basis=-96.0, market_value=-48.0)
result = run_scenario(state, positions=[opt_pos])
actions = result.get("actions", [])
check("action is 50pct_close_put", any(a.get("action") == "50pct_close_put" for a in actions))
action = next((a for a in actions if a.get("action") == "50pct_close_put"), {})
check("close_order is limit (not market)", action.get("close_order", {}).get("type") == "limit")


# ── 200% loss on short put ────────────────────────────────────────────────────

scenario("6. Stage 1 put at 200% loss -> close with market order, NO new_order")
state = make_state({"AAPL": put_symbol_state(order_status="filled")})
opt_pos = short_put_position(unrealized_pl=-200.0, cost_basis=-96.0, market_value=-296.0)
result = run_scenario(state, positions=[opt_pos])
actions = result.get("actions", [])
check("action is loss_limit_put", any(a.get("action") == "loss_limit_put" for a in actions))
action = next((a for a in actions if a.get("action") == "loss_limit_put"), {})
check("close_order is market (urgent)", action.get("close_order", {}).get("type") == "market")
check("no new_order on loss limit", "new_order" not in action)


# ── Assignment: 100 shares, no option ────────────────────────────────────────

scenario("7. Stage 1, option gone, 100 shares -> assignment detected -> Stage 2")
state = make_state({"AAPL": put_symbol_state(order_status="filled")})
result = run_scenario(state, positions=[share_position(qty="100")])
actions = result.get("actions", [])
check("action is assigned_to_stage2", any(a.get("action") == "assigned_to_stage2" for a in actions))


# ── Assignment guard: fractional shares ───────────────────────────────────────

scenario("8. Stage 1, option gone, 3.021 fractional shares -> NOT assigned")
state = make_state({"AAPL": put_symbol_state(order_status="filled")})
result = run_scenario(state, positions=[share_position(qty="3.021")])
actions = result.get("actions", [])
check("no assignment action", not any(a.get("action") == "assigned_to_stage2" for a in actions))


# ── 50% profit on covered call ────────────────────────────────────────────────

scenario("9. Stage 2 call at 50% profit -> close and re-sell call")
state = make_state({"AAPL": call_symbol_state(order_status="filled")})
opt_pos = short_call_position(unrealized_pl=52.5, cost_basis=-105.0, market_value=-52.5)
share_pos = share_position(qty="100", avg_entry=270.0)
result = run_scenario(state, positions=[opt_pos, share_pos])
actions = result.get("actions", [])
check("action is 50pct_close_call", any(a.get("action") == "50pct_close_call" for a in actions))
action = next((a for a in actions if a.get("action") == "50pct_close_call"), {})
check("close_order is limit", action.get("close_order", {}).get("type") == "limit")


# ── Called away -> back to Stage 1 ────────────────────────────────────────────

scenario("10. Stage 2, no option, no shares -> called away, back to Stage 1")
state = make_state({"AAPL": call_symbol_state(order_status="filled")})
result = run_scenario(state, positions=[])
actions = result.get("actions", [])
check("action is called_away_to_stage1", any(a.get("action") == "called_away_to_stage1" for a in actions))


# ── Order ladder: GTC adjustment ─────────────────────────────────────────────

LADDER_CHAINS = {"AAPL": {"puts": {PUT_SYM: {"latestQuote": {"bp": 0.80, "ap": 1.02}}}, "calls": {}}}

scenario("11. Pending GTC order, adj_count=0 -> limit adjusted toward bid")
state = make_state({"AAPL": put_symbol_state(order_status="pending_fill")})
open_order = {"id": "order-ladder-test", "symbol": PUT_SYM, "asset_class": "us_option", "limit_price": "0.96"}
result = run_scenario(state, positions=[], open_orders=[open_order], option_chains=LADDER_CHAINS)
actions = result.get("actions", [])
check("adjust_entry_order action emitted", any(a.get("action") == "adjust_entry_order" for a in actions))
action = next((a for a in actions if a.get("action") == "adjust_entry_order"), {})
check("new limit lower than original", float(action.get("limit_price", "99")) < 0.96)
check("replace_order_by_id dispatched", action.get("_mcp_call") == "replace_order_by_id")


# ── Order ladder: max adjustments → cancel ───────────────────────────────────

scenario("12. Pending GTC order, adj_count=3 -> cancel action emitted")
state = make_state({"AAPL": {**put_symbol_state(order_status="pending_fill"), "adjustment_count": 3}})
open_order = {"id": "order-ladder-cancel", "symbol": PUT_SYM, "asset_class": "us_option", "limit_price": "0.80"}
result = run_scenario(state, positions=[], open_orders=[open_order], option_chains=LADDER_CHAINS)
actions = result.get("actions", [])
check("cancel_unfilled_entry action emitted", any(a.get("action") == "cancel_unfilled_entry" for a in actions))
action = next((a for a in actions if a.get("action") == "cancel_unfilled_entry"), {})
check("cancel_order_by_id dispatched", action.get("_mcp_call") == "cancel_order_by_id")


# ── Vol regime gate: panic blocks new put ────────────────────────────────────

scenario("13. Vol regime panic -> new put entry blocked after profit close")
state = make_state({"AAPL": put_symbol_state(order_status="filled")})
opt_pos = short_put_position(unrealized_pl=48.0, cost_basis=-96.0, market_value=-48.0)
vol_cache = {"symbols": {"AAPL": {"regime": "panic", "iv_rank": 0.90}}}
with (
    patch("monitor.load_state", return_value=state),
    patch("monitor.save_state"),
    patch("monitor.append_log"),
    patch("put_seller.load_state", return_value=state),
    patch("put_seller.save_state"),
    patch("put_seller.append_log"),
    patch("call_seller.load_state", return_value=state),
    patch("call_seller.save_state"),
    patch("call_seller.append_log"),
    patch("monitor._in_trading_window", return_value=True),
):
    result_vol = monitor.run(
        clock=CLOCK_OPEN,
        positions=[opt_pos],
        open_orders=[],
        account=ACCOUNT,
        option_chains=CHAINS,
        trading_days=TRADING_DAYS,
        snapshots=SNAPSHOT,
        dry_run=True,
        vol_cache=vol_cache,
    )
actions = result_vol.get("actions", [])
check("50pct_close_put fired", any(a.get("action") == "50pct_close_put" for a in actions))
check("no new_order after close (blocked by panic)", not any(a.get("action") == "new_order" for a in actions))


# ── Put spread: 50% profit close ─────────────────────────────────────────────

SHORT_STRIKE = 252
LONG_STRIKE  = 242
SHORT_SYM = f"{TICKER}{EXPIRY_OCC}P{int(SHORT_STRIKE * 1000):08d}"
LONG_SYM  = f"{TICKER}{EXPIRY_OCC}P{int(LONG_STRIKE  * 1000):08d}"
NET_CREDIT_SPREAD = 2.60

spread_position = {
    "id": f"{TICKER}-put-spread-smoke",
    "symbol": TICKER,
    "strategy": "put_spread",
    "stage": "open",
    "short_contract": SHORT_SYM,
    "long_contract":  LONG_SYM,
    "short_strike": SHORT_STRIKE,
    "long_strike":  LONG_STRIKE,
    "spread_width": 10,
    "net_credit": NET_CREDIT_SPREAD,
    "max_risk": round((10 * 100) - NET_CREDIT_SPREAD * 100, 2),
    "expiry_date": EXPIRY_ISO,
    "cycle_start": "2026-05-15",
    "order_status": "filled",
    "adjustment_count": 0,
    "entry_iv_rank": 0.65,
    "entry_delta_short": 0.25,
}

scenario("14. Put spread at 50% profit -> 2-leg close emitted")
spread_chains = {
    TICKER: {
        "puts": {
            SHORT_SYM: {"latestQuote": {"bp": 1.00, "ap": 1.00}},
            LONG_SYM:  {"latestQuote": {"bp": 0.10, "ap": 0.15}},
        },
        "calls": {},
    }
}
# debit = short_ask - long_bid = 1.00 - 0.10 = 0.90 <= 0.50 * 2.60 = 1.30 → profit close
with (
    patch("spread_monitor.load_state", return_value={"spread_positions": [spread_position]}),
    patch("spread_monitor.save_state"),
    patch("spread_monitor.append_log"),
):
    spread_actions = spread_monitor.process(
        spread_position, spread_chains, [], make_trading_days(), dry_run=True
    )
check("2-leg close emitted", len(spread_actions) == 1)
check("is mleg order", spread_actions[0].get("order_class") == "mleg")
check("has 2 legs", len(spread_actions[0].get("legs", [])) == 2)


# ── Iron condor: 50% profit close ────────────────────────────────────────────

scenario("15. Iron condor at 50% profit -> 4-leg close emitted")
NET_CREDIT_CONDOR = 3.20
MAX_RISK_CONDOR = round((5 * 100) - NET_CREDIT_CONDOR * 100, 2)

SHORT_PUT_STRIKE  = 470
LONG_PUT_STRIKE   = 465
SHORT_CALL_STRIKE = 530
LONG_CALL_STRIKE  = 535
SHORT_PUT_SYM  = f"{TICKER}{EXPIRY_OCC}P{int(SHORT_PUT_STRIKE  * 1000):08d}"
LONG_PUT_SYM   = f"{TICKER}{EXPIRY_OCC}P{int(LONG_PUT_STRIKE   * 1000):08d}"
SHORT_CALL_SYM = f"{TICKER}{EXPIRY_OCC}C{int(SHORT_CALL_STRIKE * 1000):08d}"
LONG_CALL_SYM  = f"{TICKER}{EXPIRY_OCC}C{int(LONG_CALL_STRIKE  * 1000):08d}"

condor_position = {
    "id": f"{TICKER}-condor-smoke",
    "symbol": TICKER,
    "strategy": "iron_condor",
    "short_put_contract":  SHORT_PUT_SYM,
    "long_put_contract":   LONG_PUT_SYM,
    "short_call_contract": SHORT_CALL_SYM,
    "long_call_contract":  LONG_CALL_SYM,
    "short_put_strike": SHORT_PUT_STRIKE, "long_put_strike": LONG_PUT_STRIKE,
    "short_call_strike": SHORT_CALL_STRIKE, "long_call_strike": LONG_CALL_STRIKE,
    "wing_width": 5,
    "net_credit": NET_CREDIT_CONDOR,
    "max_risk": MAX_RISK_CONDOR,
    "expiry_date": EXPIRY_ISO,
    "cycle_start": "2026-05-15",
    "order_status": "filled",
    "adjustment_count": 0,
}

condor_chains = {
    TICKER: {
        "puts": {
            SHORT_PUT_SYM:  {"latestQuote": {"ap": 0.80, "bp": 0.72}},
            LONG_PUT_SYM:   {"latestQuote": {"ap": 0.22, "bp": 0.20}},
        },
        "calls": {
            SHORT_CALL_SYM: {"latestQuote": {"ap": 0.80, "bp": 0.72}},
            LONG_CALL_SYM:  {"latestQuote": {"ap": 0.22, "bp": 0.20}},
        },
    }
}
# put_debit = 0.80-0.20=0.60, call_debit=0.80-0.20=0.60, total=1.20 <= 0.5*3.20=1.60 → profit
with (
    patch("condor_monitor.load_state", return_value={"condor_positions": [condor_position]}),
    patch("condor_monitor.save_state"),
    patch("condor_monitor.append_log"),
):
    condor_actions = condor_monitor.process(
        condor_position, condor_chains, [], make_trading_days(), dry_run=True
    )
check("4-leg close emitted", len(condor_actions) == 1)
check("is mleg order", condor_actions[0].get("order_class") == "mleg")
check("has 4 legs", len(condor_actions[0].get("legs", [])) == 4)


# ── Iron condor: untested-side roll ──────────────────────────────────────────

scenario("16. Iron condor put side tested -> call roll descriptor emitted")
# put side: short_put_ask=3.50, long_put_bid=0.20 → debit=3.30 > 0.5*5=2.50 → tested
# call side: 0.80-0.20=0.60 < 2.50 → fine
condor_roll_chains = {
    TICKER: {
        "puts": {
            SHORT_PUT_SYM:  {"latestQuote": {"ap": 3.50, "bp": 3.15}},
            LONG_PUT_SYM:   {"latestQuote": {"ap": 0.22, "bp": 0.20}},
        },
        "calls": {
            SHORT_CALL_SYM: {"latestQuote": {"ap": 0.80, "bp": 0.72}},
            LONG_CALL_SYM:  {"latestQuote": {"ap": 0.22, "bp": 0.20}},
        },
    }
}
# put_debit=3.30, call_debit=0.60, total=3.90 → loss=(3.90-3.20)*100=70 < max_risk=180
# profit: 3.90 > 1.60 → no profit close. So roll should emit.
with (
    patch("condor_monitor.load_state", return_value={"condor_positions": [condor_position]}),
    patch("condor_monitor.save_state"),
    patch("condor_monitor.append_log"),
):
    roll_actions = condor_monitor.process(
        condor_position, condor_roll_chains, [], make_trading_days(), dry_run=True
    )
roll_descs = [a for a in roll_actions if a.get("_meta", {}).get("action") == "roll_untested_side"]
check("untested-side roll emitted", len(roll_descs) == 1)
check("roll targets call side", roll_descs[0]["_meta"]["side"] == "call" if roll_descs else False)


# ── Summary ───────────────────────────────────────────────────────────────────

print(f"\n{'='*50}")
if _failures:
    print(f"FAILED: {len(_failures)} scenario(s)")
    for f in _failures:
        print(f"  • {f}")
    sys.exit(1)
else:
    total = 16  # number of scenario blocks
    print(f"All smoke tests passed")
