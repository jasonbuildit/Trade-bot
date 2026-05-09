"""Unit tests for monitor.py — pure and near-pure functions."""
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

import monitor
from fixtures import (
    PUT_SYM, CALL_SYM, TICKER,
    make_state, put_symbol_state, call_symbol_state,
    short_put_position, short_call_position, share_position,
)

ET = ZoneInfo("America/New_York")


# ── Profit / loss trigger functions ──────────────────────────────────────────

class TestProfitTriggers:
    def _pos(self, cost_basis, unrealized_pl, market_value=None):
        return {
            "cost_basis": str(cost_basis),
            "unrealized_pl": str(unrealized_pl),
            "market_value": str(market_value if market_value is not None else cost_basis + unrealized_pl),
        }

    def test_50pct_profit_exact(self):
        assert monitor.is_50pct_profit(self._pos(-100, 50)) is True

    def test_50pct_profit_above(self):
        assert monitor.is_50pct_profit(self._pos(-100, 75)) is True

    def test_50pct_profit_below(self):
        assert monitor.is_50pct_profit(self._pos(-100, 49)) is False

    def test_50pct_profit_long_position_skipped(self):
        # cost_basis >= 0 means long — should not trigger
        assert monitor.is_50pct_profit(self._pos(100, 50)) is False

    def test_fast_profit_exact(self):
        assert monitor.is_fast_profit(self._pos(-100, 75)) is True

    def test_fast_profit_above(self):
        assert monitor.is_fast_profit(self._pos(-100, 90)) is True

    def test_fast_profit_below(self):
        assert monitor.is_fast_profit(self._pos(-100, 74)) is False

    def test_loss_limit_exact_2x(self):
        # cost_basis = -100, limit triggers when unrealized_pl <= -200
        assert monitor.is_loss_limit(self._pos(-100, -200)) is True

    def test_loss_limit_beyond_2x(self):
        assert monitor.is_loss_limit(self._pos(-100, -250)) is True

    def test_loss_limit_not_triggered(self):
        assert monitor.is_loss_limit(self._pos(-100, -199)) is False

    def test_loss_limit_profit_position_skipped(self):
        assert monitor.is_loss_limit(self._pos(-100, 50)) is False

    def test_mark_price(self):
        pos = {"market_value": "-48.0"}
        assert monitor._mark_price(pos) == pytest.approx(0.48)

    def test_mark_price_zero(self):
        pos = {"market_value": "0"}
        assert monitor._mark_price(pos) == 0.0


# ── Drawdown and strategy mode ────────────────────────────────────────────────

class TestDrawdown:
    def _account(self, equity):
        return {"equity": str(equity)}

    def test_no_drawdown_at_peak(self):
        state = make_state(peak_equity=100_000)
        dd = monitor.check_drawdown(self._account(100_000), state)
        assert dd == pytest.approx(0.0)

    def test_5pct_drawdown(self):
        state = make_state(peak_equity=100_000)
        dd = monitor.check_drawdown(self._account(95_000), state)
        assert dd == pytest.approx(0.05)

    def test_peak_updates_when_higher(self):
        state = make_state(peak_equity=90_000)
        monitor.check_drawdown(self._account(100_000), state)
        assert state["account_summary"]["peak_equity"] == 100_000

    def test_peak_does_not_fall(self):
        state = make_state(peak_equity=100_000)
        monitor.check_drawdown(self._account(80_000), state)
        assert state["account_summary"]["peak_equity"] == 100_000

    def test_strategy_mode_active(self):
        assert monitor._strategy_mode(0.04) == "active"

    def test_strategy_mode_pause(self):
        assert monitor._strategy_mode(0.05) == "pause"

    def test_strategy_mode_reduce(self):
        assert monitor._strategy_mode(0.08) == "reduce"

    def test_strategy_mode_disabled(self):
        assert monitor._strategy_mode(0.12) == "disabled"

    def test_strategy_mode_disabled_above(self):
        assert monitor._strategy_mode(0.20) == "disabled"


# ── Trading window ────────────────────────────────────────────────────────────

class TestTradingWindow:
    def _et(self, hour, minute):
        return datetime.now(ET).replace(hour=hour, minute=minute, second=0, microsecond=0)

    def test_open_at_945(self):
        # 9:45 ET is 15 min after 9:30 — just inside buffer
        assert monitor._in_trading_window(self._et(9, 45)) is True

    def test_blocked_at_930(self):
        assert monitor._in_trading_window(self._et(9, 30)) is False

    def test_blocked_at_931(self):
        # buffer is 15 min, so 9:31 < 9:45
        assert monitor._in_trading_window(self._et(9, 31)) is False

    def test_open_midday(self):
        assert monitor._in_trading_window(self._et(12, 0)) is True

    def test_blocked_at_345(self):
        # close buffer is 15 min before 16:00 = 15:45
        assert monitor._in_trading_window(self._et(15, 46)) is False

    def test_open_at_344(self):
        assert monitor._in_trading_window(self._et(15, 44)) is True


# ── Reconcile pending fills ───────────────────────────────────────────────────

class TestReconcilePendingFills:
    def _run(self, state, pos_map, open_orders):
        with patch("monitor.append_log"):
            monitor._reconcile_pending_fills(state, pos_map, open_orders)

    def test_fill_detected(self):
        state = make_state({"AAPL": put_symbol_state(order_status="pending_fill")})
        pos_map = {PUT_SYM: {"avg_entry_price": "0.94", "symbol": PUT_SYM}}
        self._run(state, pos_map, [])
        assert state["symbols"]["AAPL"]["order_status"] == "filled"
        assert state["symbols"]["AAPL"]["fill_price"] == pytest.approx(0.94)

    def test_expired_clears_symbol(self):
        state = make_state({"AAPL": put_symbol_state(order_status="pending_fill")})
        self._run(state, {}, [])  # not in positions, not in open orders
        assert "AAPL" not in state["symbols"]

    def test_still_pending_unchanged(self):
        sym_state = put_symbol_state(order_status="pending_fill")
        state = make_state({"AAPL": sym_state})
        open_order = {"symbol": PUT_SYM, "asset_class": "us_option"}
        self._run(state, {}, [open_order])
        assert state["symbols"]["AAPL"]["order_status"] == "pending_fill"

    def test_already_filled_skipped(self):
        state = make_state({"AAPL": put_symbol_state(order_status="filled")})
        # Even though it's not in pos_map, should not be re-processed
        self._run(state, {}, [])
        assert "AAPL" in state["symbols"]

    def test_no_option_symbol_skipped(self):
        sym = {**put_symbol_state(order_status="pending_fill"), "option_symbol": None}
        state = make_state({"AAPL": sym})
        self._run(state, {}, [])
        assert "AAPL" in state["symbols"]  # no option_symbol → skip


# ── Assignment guard (partial shares) ────────────────────────────────────────

class TestAssignmentGuard:
    """
    Stage 1 with option gone: fractional shares must NOT trigger assignment.
    100 shares MUST trigger assignment. Tested via run() with put_seller mocked.
    """

    def _run_monitor(self, state, positions, open_orders=None):
        with (
            patch("monitor.load_state", return_value=state),
            patch("monitor.save_state"),
            patch("monitor.append_log"),
            patch("put_seller.run", return_value=None),
            patch("call_seller.run", return_value=None),
        ):
            from fixtures import ACCOUNT, CLOCK_OPEN, SNAPSHOT, make_trading_days
            return monitor.run(
                clock=CLOCK_OPEN,
                positions=positions,
                open_orders=open_orders or [],
                account=ACCOUNT,
                option_chains={"AAPL": {"puts": {}, "calls": {}}},
                trading_days=make_trading_days(),
                snapshots=SNAPSHOT,
                dry_run=True,
            )

    def test_fractional_shares_not_assigned(self):
        state = make_state({"AAPL": put_symbol_state(order_status="filled")})
        # Only 3.021 shares — not a real assignment
        positions = [share_position(qty="3.021")]
        result = self._run_monitor(state, positions)
        actions = result.get("actions", [])
        assert not any(a["action"] == "assigned_to_stage2" for a in actions)

    def test_hundred_shares_triggers_assignment(self):
        state = make_state({"AAPL": put_symbol_state(order_status="filled")})
        positions = [share_position(qty="100")]
        result = self._run_monitor(state, positions)
        actions = result.get("actions", [])
        assert any(a["action"] == "assigned_to_stage2" for a in actions)
