"""Unit tests for volatility.py — HV computation, IV rank, regime classification."""
import json
import math
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

import volatility


def _flat_closes(n: int, base: float = 100.0) -> list[float]:
    """All same price → zero returns → HV = 0."""
    return [base] * n


def _trending_closes(n: int, daily_return: float = 0.01) -> list[float]:
    closes = [100.0]
    for _ in range(n - 1):
        closes.append(closes[-1] * (1 + daily_return))
    return closes


def _volatile_closes(n: int, sigma: float = 0.02) -> list[float]:
    """Deterministic zig-zag closes that produce a known non-zero HV."""
    closes = [100.0]
    for i in range(n - 1):
        closes.append(closes[-1] * (1 + sigma if i % 2 == 0 else 1 - sigma))
    return closes


class TestHVComputation:
    def test_flat_prices_give_zero_hv(self):
        closes = _flat_closes(30)
        hv = volatility.compute_hv(closes, window=21)
        assert hv == pytest.approx(0.0, abs=1e-10)

    def test_hv_positive_for_volatile_prices(self):
        closes = _volatile_closes(30)
        hv = volatility.compute_hv(closes, window=21)
        assert hv > 0

    def test_hv_annualized_scale(self):
        # Daily returns of exactly +2%/-2% alternating → known daily std
        closes = _volatile_closes(30, sigma=0.02)
        hv = volatility.compute_hv(closes, window=21)
        # Daily std ≈ 0.02, annualized ≈ 0.02 × sqrt(252) ≈ 0.317
        assert 0.25 < hv < 0.40

    def test_hv_short_window_sufficient_data(self):
        closes = _volatile_closes(25, sigma=0.01)
        hv = volatility.compute_hv(closes, window=21)
        assert hv > 0

    def test_hv_insufficient_data_returns_zero(self):
        closes = _volatile_closes(5, sigma=0.01)
        hv = volatility.compute_hv(closes, window=21)
        assert hv == 0.0

    def test_higher_sigma_gives_higher_hv(self):
        low  = volatility.compute_hv(_volatile_closes(30, sigma=0.01), window=21)
        high = volatility.compute_hv(_volatile_closes(30, sigma=0.03), window=21)
        assert high > low


class TestIVRank:
    def _make_closes(self, n: int = 100) -> list[float]:
        return _volatile_closes(n, sigma=0.015)

    def test_iv_rank_in_range(self):
        closes = self._make_closes()
        result = volatility.compute_iv_rank(0.25, closes)
        assert 0.0 <= result["iv_rank"] <= 1.0

    def test_high_iv_current_gives_high_rank(self):
        closes = self._make_closes()
        # iv_current well above typical HV → high rank
        high = volatility.compute_iv_rank(2.0, closes)
        assert high["iv_rank"] == pytest.approx(1.0)

    def test_low_iv_current_gives_low_rank(self):
        closes = self._make_closes()
        low = volatility.compute_iv_rank(0.001, closes)
        assert low["iv_rank"] == pytest.approx(0.0)

    def test_result_contains_required_keys(self):
        closes = self._make_closes()
        result = volatility.compute_iv_rank(0.25, closes)
        for key in ("hv_21", "hv_252", "iv_current", "iv_rank", "regime"):
            assert key in result

    def test_insufficient_data_returns_normal_regime(self):
        closes = _flat_closes(5)
        result = volatility.compute_iv_rank(0.25, closes)
        # Not enough data → iv_rank defaults to 0.5 → normal or high regime
        assert result["regime"] in ("normal", "high")


class TestRegimeClassification:
    def test_low_regime_below_min_entry(self):
        assert volatility.classify_regime(0.0)  == "low"
        assert volatility.classify_regime(0.29) == "low"

    def test_normal_regime(self):
        assert volatility.classify_regime(0.30) == "normal"
        assert volatility.classify_regime(0.49) == "normal"

    def test_high_regime(self):
        assert volatility.classify_regime(0.50) == "high"
        assert volatility.classify_regime(0.84) == "high"

    def test_panic_regime_at_threshold(self):
        assert volatility.classify_regime(0.85) == "panic"
        assert volatility.classify_regime(1.0)  == "panic"

    def test_boundary_at_min_entry(self):
        # Exactly at IV_RANK_MIN_ENTRY (0.30) → "normal", not "low"
        assert volatility.classify_regime(0.30) == "normal"

    def test_boundary_at_panic(self):
        # Exactly at IV_RANK_PANIC (0.85) → "panic"
        assert volatility.classify_regime(0.85) == "panic"


class TestCacheStale:
    def test_stale_when_date_differs(self):
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        assert volatility.is_cache_stale({"date": yesterday}) is True

    def test_fresh_when_date_matches_today(self):
        assert volatility.is_cache_stale({"date": date.today().isoformat()}) is False

    def test_stale_when_date_missing(self):
        assert volatility.is_cache_stale({}) is True

    def test_load_returns_empty_when_file_missing(self, tmp_path):
        with patch.object(volatility, "CACHE_FILE", tmp_path / "nonexistent.json"):
            result = volatility.load_vol_cache()
        assert result == {}

    def test_load_returns_empty_when_stale(self, tmp_path):
        cache = {"date": "2000-01-01", "symbols": {"AAPL": {}}}
        cache_file = tmp_path / "volatility_cache.json"
        cache_file.write_text(json.dumps(cache))
        with patch.object(volatility, "CACHE_FILE", cache_file):
            result = volatility.load_vol_cache()
        assert result == {}

    def test_load_returns_cache_when_fresh(self, tmp_path):
        cache = {"date": date.today().isoformat(), "symbols": {"AAPL": {"iv_rank": 0.6}}}
        cache_file = tmp_path / "volatility_cache.json"
        cache_file.write_text(json.dumps(cache))
        with patch.object(volatility, "CACHE_FILE", cache_file):
            result = volatility.load_vol_cache()
        assert result["symbols"]["AAPL"]["iv_rank"] == 0.6


class TestBuildVolCache:
    def test_builds_cache_with_today_date(self):
        bars = {"AAPL": [{"c": c} for c in _volatile_closes(100)]}
        cache = volatility.build_vol_cache(bars)
        assert cache["date"] == date.today().isoformat()
        assert "AAPL" in cache["symbols"]

    def test_skips_symbol_with_insufficient_bars(self):
        bars = {"TINY": [{"c": c} for c in _volatile_closes(5)]}
        cache = volatility.build_vol_cache(bars)
        assert "TINY" not in cache["symbols"]

    def test_uses_atm_iv_when_provided(self):
        bars = {"AAPL": [{"c": c} for c in _volatile_closes(100)]}
        cache = volatility.build_vol_cache(bars, atm_ivs={"AAPL": 0.45})
        assert cache["symbols"]["AAPL"]["iv_current"] == pytest.approx(0.45)

    def test_falls_back_to_hv_when_atm_iv_absent(self):
        bars = {"AAPL": [{"c": c} for c in _volatile_closes(100, sigma=0.02)]}
        cache = volatility.build_vol_cache(bars, atm_ivs={})
        # Falls back to hv_21 for iv_current
        result = cache["symbols"]["AAPL"]
        assert result["iv_current"] > 0
