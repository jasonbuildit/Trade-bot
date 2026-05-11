"""Unit tests for put_seller.py — guards and happy path."""
from unittest.mock import patch, MagicMock

import pytest

import put_seller
from fixtures import (
    TICKER, PUT_SYM, EXPIRY_ISO,
    make_state, put_symbol_state, put_chain, make_trading_days,
)

PRICE = 280.0
BUYING_POWER = 50_000.0
PORTFOLIO = 100_000.0
TRADING_DAYS = make_trading_days()


def _run(symbol=TICKER, price=PRICE, bp=BUYING_POWER, puts=None,
         state=None, portfolio=PORTFOLIO, dry_run=False):
    if state is None:
        state = make_state()
    if puts is None:
        puts = put_chain()
    with (
        patch("put_seller.load_state", return_value=state),
        patch("put_seller.save_state"),
        patch("put_seller.append_log"),
    ):
        return put_seller.run(symbol, price, bp, puts, TRADING_DAYS,
                              portfolio_value=portfolio, dry_run=dry_run)


# ── find_put_contract ─────────────────────────────────────────────────────────

class TestFindPutContract:
    def test_finds_nearest_to_target(self):
        puts = {**put_chain(strike=260), **put_chain(strike=240)}
        # target_strike = round(280 * 0.90) = 252 → closer to 260 than 240? 260-252=8, 252-240=12 → 260
        result = put_seller.find_put_contract(puts, target_strike=252)
        assert result is not None
        assert result["strike"] == pytest.approx(260.0)

    def test_skips_zero_bid(self):
        puts = put_chain(strike=260, bid=0.0)
        result = put_seller.find_put_contract(puts, target_strike=260)
        assert result is None

    def test_returns_ask(self):
        puts = put_chain(strike=260, bid=0.90, ask=1.02)
        result = put_seller.find_put_contract(puts, target_strike=260)
        assert result["ask"] == pytest.approx(1.02)


# ── Duplicate pending-fill guard ──────────────────────────────────────────────

class TestDuplicateGuard:
    def test_blocks_when_pending_fill(self):
        state = make_state({"AAPL": put_symbol_state(order_status="pending_fill")})
        result = _run(state=state)
        assert result is None

    def test_allows_when_filled(self):
        state = make_state({"AAPL": put_symbol_state(order_status="filled")})
        # Bypass sector cap (not what this test is about)
        with patch("put_seller._sector_of", return_value=None):
            result = _run(state=state, puts=put_chain(strike=90), price=100.0, dry_run=True)
        assert result is not None


# ── Cash guard ────────────────────────────────────────────────────────────────

class TestCashGuard:
    def test_blocks_when_insufficient_bp(self):
        # strike=260, cash_required=26000 — buying_power only 1000
        result = _run(bp=1_000.0)
        assert result is None

    def test_passes_with_sufficient_bp(self):
        result = _run(bp=50_000.0, puts=put_chain(strike=90), price=100.0, dry_run=True)
        assert result is not None


# ── Position size cap ─────────────────────────────────────────────────────────

class TestPositionSizeCap:
    def test_blocks_when_no_valid_smaller_strike(self):
        # portfolio=100k, 10% cap = 10k. strike=260 → 26k > cap, and no smaller strike available
        result = _run(portfolio=100_000.0, puts=put_chain(strike=260))
        assert result is None

    def test_passes_with_small_strike(self):
        # 10% of 100k = 10k. strike=90 → 9000 < 10k ✓
        result = _run(portfolio=100_000.0, puts=put_chain(strike=90), price=100.0, dry_run=True)
        assert result is not None


# ── Min cash reserve guard ────────────────────────────────────────────────────

class TestMinCashReserve:
    def test_blocks_when_reserve_would_be_violated(self):
        # portfolio=100k, reserve=25k. bp=27k, strike=90 → cash_required=9k
        # bp after trade = 27k - 9k = 18k < 25k reserve → blocked
        result = _run(portfolio=100_000.0, bp=27_000.0, puts=put_chain(strike=90), price=100.0)
        assert result is None

    def test_passes_with_enough_reserve(self):
        # bp=40k, cash_required=9k → 31k remaining > 25k reserve ✓
        result = _run(portfolio=100_000.0, bp=40_000.0, puts=put_chain(strike=90), price=100.0, dry_run=True)
        assert result is not None


# ── Sector exposure cap ──────────────────────────────────────────────────────

class TestSectorCap:
    def _run_with_sector(self, existing_risk=0, symbol=TICKER):
        existing_sym = {**put_symbol_state(order_status="filled"), "max_risk": existing_risk}
        state = make_state({symbol: existing_sym})
        with patch("put_seller._sector_of", return_value="tech"):
            return _run(puts=put_chain(strike=90), price=100.0, state=state,
                        bp=50_000.0, portfolio=100_000.0, dry_run=True)

    def test_blocks_when_sector_cap_exceeded(self):
        # existing tech exposure $22k + new $9k = $31k > 25% of $100k ($25k)
        result = self._run_with_sector(existing_risk=22_000)
        assert result is None

    def test_passes_when_sector_has_room(self):
        # existing $10k + new $9k = $19k < $25k cap
        result = self._run_with_sector(existing_risk=10_000)
        assert result is not None

    def test_passes_when_no_sector_tag(self):
        # symbol has no sector — cap is skipped
        state = make_state()
        with patch("put_seller._sector_of", return_value=None):
            result = _run(puts=put_chain(strike=90), price=100.0, state=state,
                          bp=50_000.0, portfolio=100_000.0, dry_run=True)
        assert result is not None

    def test_only_counts_stage1_same_sector(self):
        # stage=2 position in same sector should not count toward cap
        stage2_sym = {**put_symbol_state(order_status="filled"), "stage": 2, "max_risk": 20_000}
        state = make_state({"AAPL": stage2_sym})
        with patch("put_seller._sector_of", return_value="tech"):
            result = _run(puts=put_chain(strike=90), price=100.0, state=state,
                          bp=50_000.0, portfolio=100_000.0, dry_run=True)
        assert result is not None


# ── Happy path ────────────────────────────────────────────────────────────────

class TestHappyPath:
    def test_returns_mcp_call_dict(self):
        result = _run(puts=put_chain(strike=90), price=100.0, dry_run=True)
        assert result is not None
        assert result.get("dry_run") is True
        assert "limit_price" in result

    def test_limit_price_is_mid(self):
        # bid=0.90, ask=1.02 → mid = 0.96
        result = _run(puts=put_chain(strike=90, bid=0.90, ask=1.02), price=100.0, dry_run=True)
        assert result["limit_price"] == pytest.approx(0.96)

    def test_limit_price_falls_back_to_bid_when_ask_missing(self):
        # ask=0 → limit_price = bid
        result = _run(puts=put_chain(strike=90, bid=0.90, ask=0.0), price=100.0, dry_run=True)
        assert result["limit_price"] == pytest.approx(0.90)

    def test_state_written_on_live_run(self):
        saved = {}

        def capture_save(state):
            saved.update(state)

        state = make_state()
        with (
            patch("put_seller.load_state", return_value=state),
            patch("put_seller.save_state", side_effect=capture_save),
            patch("put_seller.append_log"),
        ):
            put_seller.run(TICKER, 100.0, 40_000.0,
                           put_chain(strike=90), make_trading_days(),
                           portfolio_value=100_000.0, dry_run=False)

        assert TICKER in saved.get("symbols", {})
        assert saved["symbols"][TICKER]["stage"] == 1
        assert saved["symbols"][TICKER]["order_status"] == "pending_fill"
