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
from tests.fixtures import (
    TICKER, PUT_SYM, CALL_SYM,
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


# ── Summary ───────────────────────────────────────────────────────────────────

print(f"\n{'='*50}")
if _failures:
    print(f"FAILED: {len(_failures)} scenario(s)")
    for f in _failures:
        print(f"  • {f}")
    sys.exit(1)
else:
    total = 12  # number of scenario blocks
    print(f"All smoke tests passed")
