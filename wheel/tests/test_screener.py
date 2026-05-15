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

    def test_delta_zero_blocked(self):
        # delta=0 means no greeks data (e.g. deep ITM); must be rejected
        sym, data = _contract(260, delta=0.0)
        results = screener.score_candidate(TICKER, 280.0, {sym: data})
        assert results == []

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


# ── Theta scoring ─────────────────────────────────────────────────────────────

def _contract_with_theta(strike, theta=-0.05, **kwargs):
    sym, data = _contract(strike, **kwargs)
    data.setdefault("greeks", {})["theta"] = theta
    return sym, data


class TestTheta:
    def test_theta_yield_present_in_result(self):
        sym, data = _contract_with_theta(260)
        results = screener.score_candidate(TICKER, 280.0, {sym: data})
        assert len(results) == 1
        assert "theta_yield" in results[0]
        assert "theta" in results[0]

    def test_theta_yield_nonzero_when_theta_provided(self):
        sym, data = _contract_with_theta(260, theta=-0.05)
        results = screener.score_candidate(TICKER, 280.0, {sym: data})
        assert results[0]["theta_yield"] > 0

    def test_theta_yield_zero_when_no_theta_in_greeks(self):
        sym, data = _contract(260)
        # Keep delta (needed to pass filter) but remove theta
        data["greeks"].pop("theta", None)
        results = screener.score_candidate(TICKER, 280.0, {sym: data})
        assert len(results) == 1
        assert results[0]["theta_yield"] == 0.0

    def test_higher_theta_gives_higher_theta_yield(self):
        sym1, d1 = _contract_with_theta(260, theta=-0.10)
        sym2, d2 = _contract_with_theta(260, theta=-0.05)
        r1 = screener.score_candidate(TICKER, 280.0, {sym1: d1})[0]
        r2 = screener.score_candidate(TICKER, 280.0, {sym2: d2})[0]
        assert r1["theta_yield"] > r2["theta_yield"]

    def test_iv_rank_and_regime_present_when_vol_cache_provided(self):
        sym, data = _contract_with_theta(260)
        vol_cache = {
            "date": date.today().isoformat(),
            "symbols": {TICKER: {"iv_rank": 0.65, "regime": "high"}},
        }
        results = screener.score_candidate(TICKER, 280.0, {sym: data}, vol_cache=vol_cache)
        assert results[0]["iv_rank"] == pytest.approx(0.65)
        assert results[0]["regime"] == "high"

    def test_iv_rank_none_when_no_vol_cache(self):
        sym, data = _contract(260)
        results = screener.score_candidate(TICKER, 280.0, {sym: data})
        assert results[0]["iv_rank"] is None
        assert results[0]["regime"] is None


# ── IV regime gate ────────────────────────────────────────────────────────────

def _make_vol_cache(symbol: str, regime: str, iv_rank: float = 0.5) -> dict:
    return {
        "date": date.today().isoformat(),
        "symbols": {symbol: {"iv_rank": iv_rank, "regime": regime}},
    }


class TestVolRegimeGate:
    def test_low_regime_blocked_in_strict_mode(self):
        sym, data = _contract(260)
        vol_cache = _make_vol_cache(TICKER, "low", iv_rank=0.20)
        results = screener.score_candidate(
            TICKER, 280.0, {sym: data}, strict_trend=True, vol_cache=vol_cache
        )
        assert results == []

    def test_panic_regime_blocked_in_strict_mode(self):
        sym, data = _contract(260)
        vol_cache = _make_vol_cache(TICKER, "panic", iv_rank=0.90)
        results = screener.score_candidate(
            TICKER, 280.0, {sym: data}, strict_trend=True, vol_cache=vol_cache
        )
        assert results == []

    def test_iv_regime_block_flag_set_for_low_regime(self):
        sym, data = _contract(260)
        vol_cache = _make_vol_cache(TICKER, "low", iv_rank=0.20)
        results = screener.score_candidate(
            TICKER, 280.0, {sym: data}, strict_trend=False, vol_cache=vol_cache
        )
        assert len(results) == 1
        assert "IV_REGIME_BLOCK" in results[0]["flags"]

    def test_iv_regime_block_flag_set_for_panic_regime(self):
        sym, data = _contract(260)
        vol_cache = _make_vol_cache(TICKER, "panic", iv_rank=0.90)
        results = screener.score_candidate(
            TICKER, 280.0, {sym: data}, strict_trend=False, vol_cache=vol_cache
        )
        assert len(results) == 1
        assert "IV_REGIME_BLOCK" in results[0]["flags"]

    def test_normal_regime_passes_in_strict_mode(self):
        sym, data = _contract(260)
        vol_cache = _make_vol_cache(TICKER, "normal", iv_rank=0.45)
        results = screener.score_candidate(
            TICKER, 280.0, {sym: data}, strict_trend=True, vol_cache=vol_cache
        )
        assert len(results) == 1

    def test_high_regime_passes_in_strict_mode(self):
        sym, data = _contract(260)
        vol_cache = _make_vol_cache(TICKER, "high", iv_rank=0.70)
        results = screener.score_candidate(
            TICKER, 280.0, {sym: data}, strict_trend=True, vol_cache=vol_cache
        )
        assert len(results) == 1

    def test_no_vol_cache_passes_all(self):
        sym, data = _contract(260)
        results = screener.score_candidate(
            TICKER, 280.0, {sym: data}, strict_trend=True, vol_cache=None
        )
        assert len(results) == 1
