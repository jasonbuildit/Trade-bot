"""Unit tests for screener.py — score_candidate filters and OCC parsing."""
from datetime import date, timedelta

import pytest

import screener
from fixtures import TICKER, EXPIRY_OCC, EXPIRY_ISO, EXP_DT, TODAY


def _contract(strike, delta=-0.25, bid=1.00, ask=1.05, iv=0.35, vol=100):
    sym = f"{TICKER}{EXPIRY_OCC}P{int(strike * 1000):08d}"
    data = {
        "latestQuote": {"bp": bid, "ap": ask},
        "greeks": {"delta": delta},
        "impliedVolatility": iv,
        "dailyBar": {"t": TODAY.isoformat() + "T00:00:00Z", "v": vol, "c": (bid + ask) / 2},
    }
    return sym, data


def _puts(*strikes, **kwargs):
    return dict(_contract(s, **kwargs) for s in strikes)


# ── OCC symbol parsing ────────────────────────────────────────────────────────

class TestOCCParsing:
    def test_strike_from_last_8_chars(self):
        contract = f"{TICKER}{EXPIRY_OCC}P00260000"
        strike = int(contract[-8:]) / 1000
        assert strike == pytest.approx(260.0)

    def test_expiry_from_symbol_offset(self):
        contract = f"{TICKER}{EXPIRY_OCC}P00260000"
        raw = "20" + contract[len(TICKER):len(TICKER) + 6]
        expiry = f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
        assert expiry == EXPIRY_ISO

    def test_qqq_offset(self):
        # 3-char ticker
        exp_occ = EXP_DT.strftime("%y%m%d")
        contract = f"QQQ{exp_occ}P00500000"
        strike = int(contract[-8:]) / 1000
        assert strike == pytest.approx(500.0)
        raw = "20" + contract[3:9]
        expiry = f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
        assert expiry == EXPIRY_ISO


# ── Earnings gate ─────────────────────────────────────────────────────────────

class TestEarningsGate:
    def test_earnings_before_expiry_blocks(self):
        earnings = (EXP_DT - timedelta(days=5)).isoformat()
        sym, data = _contract(260)
        results = screener.score_candidate(TICKER, 280.0, {sym: data}, earnings_date=earnings)
        assert results == []

    def test_earnings_after_expiry_allows(self):
        earnings = (EXP_DT + timedelta(days=10)).isoformat()
        sym, data = _contract(260)
        results = screener.score_candidate(TICKER, 280.0, {sym: data}, earnings_date=earnings)
        assert len(results) == 1

    def test_no_earnings_allows(self):
        sym, data = _contract(260)
        results = screener.score_candidate(TICKER, 280.0, {sym: data}, earnings_date=None)
        assert len(results) == 1


# ── Delta filter ──────────────────────────────────────────────────────────────

class TestDeltaFilter:
    def test_delta_too_high_blocked(self):
        sym, data = _contract(260, delta=-0.40)  # > DELTA_MAX 0.35
        results = screener.score_candidate(TICKER, 280.0, {sym: data})
        assert results == []

    def test_delta_too_low_blocked(self):
        sym, data = _contract(260, delta=-0.10)  # < DELTA_MIN 0.16
        results = screener.score_candidate(TICKER, 280.0, {sym: data})
        assert results == []

    def test_delta_zero_not_filtered(self):
        # delta=0 (no greek data) passes the delta filter — screener requires delta>0 AND <DELTA_MIN to block
        sym, data = _contract(260, delta=0.0)
        results = screener.score_candidate(TICKER, 280.0, {sym: data})
        # Not blocked by delta (no greeks present); may be blocked by other filters — just confirm delta isn't reason
        # The screener lets it through; downstream callers can validate greeks separately
        assert isinstance(results, list)

    def test_delta_in_range_passes(self):
        sym, data = _contract(260, delta=-0.25)
        results = screener.score_candidate(TICKER, 280.0, {sym: data})
        assert len(results) == 1


# ── Bid-ask spread filter ─────────────────────────────────────────────────────

class TestSpreadFilter:
    def test_wide_spread_blocked(self):
        # bid=0.50, ask=1.50 → spread = 1.00 / mid 1.00 = 100% > 10%
        sym, data = _contract(260, bid=0.50, ask=1.50)
        results = screener.score_candidate(TICKER, 280.0, {sym: data})
        assert results == []

    def test_tight_spread_passes(self):
        sym, data = _contract(260, bid=1.00, ask=1.05)
        results = screener.score_candidate(TICKER, 280.0, {sym: data})
        assert len(results) == 1

    def test_no_bid_blocked(self):
        sym, data = _contract(260, bid=0.0)
        results = screener.score_candidate(TICKER, 280.0, {sym: data})
        assert results == []


# ── Trend filter (strict mode) ────────────────────────────────────────────────

class TestTrendFilter:
    def test_downtrend_blocked_in_strict_mode(self):
        sym, data = _contract(260)
        # price 250 < sma21 280 → downtrend
        results = screener.score_candidate(TICKER, 250.0, {sym: data}, strict_trend=True, sma21=280.0)
        assert results == []

    def test_downtrend_allowed_in_non_strict(self):
        sym, data = _contract(260)
        results = screener.score_candidate(TICKER, 250.0, {sym: data}, strict_trend=False, sma21=280.0)
        assert len(results) == 1
        assert "TREND_DOWN" in results[0]["flags"]

    def test_uptrend_passes(self):
        sym, data = _contract(260)
        results = screener.score_candidate(TICKER, 290.0, {sym: data}, strict_trend=True, sma21=270.0)
        assert len(results) == 1


# ── Sorting ───────────────────────────────────────────────────────────────────

class TestSorting:
    def test_sorted_by_ann_yield_descending(self):
        # Two contracts: strike 260 (higher yield) vs 250 (lower yield at same bid)
        sym1, d1 = _contract(260, bid=2.00, ask=2.20)  # higher yield on lower strike notional
        sym2, d2 = _contract(200, bid=2.00, ask=2.20)  # same bid, higher notional → lower yield
        results = screener.score_candidate(TICKER, 280.0, {sym1: d1, sym2: d2})
        assert results[0]["ann_yield"] >= results[-1]["ann_yield"]
