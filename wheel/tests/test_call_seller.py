"""Unit tests for call_seller.py — guards and happy path."""
from unittest.mock import patch

import pytest

import call_seller
from fixtures import (
    TICKER, CALL_SYM,
    make_state, call_symbol_state, put_symbol_state, call_chain, make_trading_days,
)

COST_BASIS = 270.0
PREMIUMS = 0.96
TRADING_DAYS = make_trading_days()


def _run(symbol=TICKER, cost_basis=COST_BASIS, premiums=PREMIUMS,
         calls=None, state=None, dry_run=False):
    if state is None:
        state = make_state()
    if calls is None:
        calls = call_chain()
    with (
        patch("call_seller.load_state", return_value=state),
        patch("call_seller.save_state"),
        patch("call_seller.append_log"),
    ):
        return call_seller.run(symbol, cost_basis, premiums, calls, TRADING_DAYS, dry_run=dry_run)


# ── find_call_contract ────────────────────────────────────────────────────────

class TestFindCallContract:
    def test_rejects_strike_below_min(self):
        calls = call_chain(strike=260)  # min_strike = 270 - 0.96 = 269.04
        result = call_seller.find_call_contract(calls, target_strike=300, min_strike=270.0)
        assert result is None

    def test_accepts_strike_above_min(self):
        calls = call_chain(strike=290)
        result = call_seller.find_call_contract(calls, target_strike=290, min_strike=270.0)
        assert result is not None
        assert result["strike"] == pytest.approx(290.0)

    def test_skips_zero_bid(self):
        calls = call_chain(strike=290, bid=0.0)
        result = call_seller.find_call_contract(calls, target_strike=290, min_strike=270.0)
        assert result is None

    def test_returns_ask(self):
        calls = call_chain(strike=290, bid=1.00, ask=1.10)
        result = call_seller.find_call_contract(calls, target_strike=290, min_strike=270.0)
        assert result["ask"] == pytest.approx(1.10)


# ── Duplicate pending-fill guard (Phase 1) ────────────────────────────────────

class TestDuplicateGuard:
    def test_blocks_when_stage2_pending(self):
        state = make_state({"AAPL": call_symbol_state(order_status="pending_fill")})
        result = _run(state=state)
        assert result is None

    def test_allows_when_not_pending(self):
        state = make_state({"AAPL": call_symbol_state(order_status="filled")})
        result = _run(state=state, dry_run=True)
        assert result is not None

    def test_allows_when_symbol_not_in_state(self):
        state = make_state()
        result = _run(state=state, dry_run=True)
        assert result is not None


# ── Strike below effective basis ──────────────────────────────────────────────

class TestStrikeBasisGuard:
    def test_no_call_contract_above_basis(self):
        # effective_basis = 270 - 0.96 = 269.04; all calls at 260 → rejected
        calls = call_chain(strike=260)
        result = _run(calls=calls)
        assert result is None

    def test_call_above_basis_accepted(self):
        calls = call_chain(strike=290)
        result = _run(calls=calls, dry_run=True)
        assert result is not None


# ── Happy path ────────────────────────────────────────────────────────────────

class TestHappyPath:
    def test_returns_dry_run_dict(self):
        result = _run(dry_run=True)
        assert result is not None
        assert result.get("dry_run") is True
        assert "limit_price" in result

    def test_limit_price_is_mid(self):
        calls = call_chain(strike=290, bid=1.00, ask=1.10)
        result = _run(calls=calls, dry_run=True)
        assert result["limit_price"] == pytest.approx(1.05)

    def test_limit_price_falls_back_to_bid(self):
        calls = call_chain(strike=290, bid=1.00, ask=0.0)
        result = _run(calls=calls, dry_run=True)
        assert result["limit_price"] == pytest.approx(1.00)

    def test_state_written_on_live_run(self):
        saved = {}

        def capture_save(state):
            saved.update(state)

        state = make_state()
        with (
            patch("call_seller.load_state", return_value=state),
            patch("call_seller.save_state", side_effect=capture_save),
            patch("call_seller.append_log"),
        ):
            call_seller.run(TICKER, COST_BASIS, PREMIUMS,
                            call_chain(strike=290), TRADING_DAYS, dry_run=False)

        assert TICKER in saved.get("symbols", {})
        assert saved["symbols"][TICKER]["stage"] == 2
        assert saved["symbols"][TICKER]["order_status"] == "pending_fill"
        assert saved["symbols"][TICKER]["shares_qty"] == 100


# ── Dividend risk block ───────────────────────────────────────────────────────

class TestDividendBlock:
    def _make_watchlist(self, ex_div: str | None, allow_div_risk: bool = False) -> list[dict]:
        return [{"symbol": TICKER, "ex_dividend_date": ex_div, "allow_div_risk": allow_div_risk}]

    def test_blocks_when_ex_div_before_expiry(self):
        # Expiry will be ~30 DTE; set ex-div far in the future but before expiry
        expiry = TRADING_DAYS[15]["date"]  # ~30 DTE entry
        wl = self._make_watchlist(ex_div=expiry)  # ex-div == expiry → blocks
        with (
            patch("call_seller.load_state", return_value=make_state()),
            patch("call_seller.save_state"),
            patch("call_seller.append_log"),
            patch("call_seller._allow_div_risk", return_value=False),
            patch("call_seller._check_dividend_risk", return_value={"blocked": True, "reason": "test"}),
        ):
            result = call_seller.run(TICKER, COST_BASIS, PREMIUMS,
                                     call_chain(strike=290), TRADING_DAYS, dry_run=True)
        assert result is None

    def test_allows_when_allow_div_risk_true(self):
        with (
            patch("call_seller.load_state", return_value=make_state()),
            patch("call_seller.save_state"),
            patch("call_seller.append_log"),
            patch("call_seller._allow_div_risk", return_value=True),
            patch("call_seller._check_dividend_risk", return_value={"blocked": True, "reason": "test"}),
        ):
            result = call_seller.run(TICKER, COST_BASIS, PREMIUMS,
                                     call_chain(strike=290), TRADING_DAYS, dry_run=True)
        assert result is not None

    def test_passes_when_no_dividend(self):
        with (
            patch("call_seller.load_state", return_value=make_state()),
            patch("call_seller.save_state"),
            patch("call_seller.append_log"),
            patch("call_seller._check_dividend_risk", return_value={"blocked": False}),
        ):
            result = call_seller.run(TICKER, COST_BASIS, PREMIUMS,
                                     call_chain(strike=290), TRADING_DAYS, dry_run=True)
        assert result is not None
