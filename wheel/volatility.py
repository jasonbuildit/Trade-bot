"""
Volatility regime computation for entry gating.

Computes historical volatility (HV) and IV Rank from daily bar data.
No MCP calls — pure computation on injected data.
Results cached daily in wheel/volatility_cache.json.
"""
import json
import math
from datetime import date
from pathlib import Path

from config import (
    HV_LOOKBACK_DAYS,
    HV_SHORT_WINDOW,
    IV_RANK_MIN_ENTRY,
    IV_RANK_PANIC,
)

ROOT = Path(__file__).parent.parent
CACHE_FILE = ROOT / "wheel" / "volatility_cache.json"


def compute_hv(closes: list[float], window: int = 21) -> float:
    """Annualized historical volatility from daily close prices using log returns."""
    if len(closes) < window + 1:
        return 0.0
    recent = closes[-(window + 1):]
    log_returns = [math.log(recent[i] / recent[i - 1]) for i in range(1, len(recent))]
    n = len(log_returns)
    mean = sum(log_returns) / n
    variance = sum((r - mean) ** 2 for r in log_returns) / (n - 1)
    return math.sqrt(variance * 252)


def _rolling_hv(closes: list[float], window: int) -> list[float]:
    """Compute rolling HV for each sub-window across the full close series."""
    results = []
    for i in range(window, len(closes)):
        sub = closes[i - window: i + 1]
        results.append(compute_hv(sub, window))
    return results


def compute_iv_rank(iv_current: float, bar_closes: list[float]) -> dict:
    """
    Returns:
      hv_21, hv_252, iv_rank (0–1), regime ("low"|"normal"|"high"|"panic")

    Uses HV as a proxy for IV history when historical IV is unavailable.
    iv_current: ATM implied volatility from the option chain (annualized).
    bar_closes: list of daily close prices, ideally HV_LOOKBACK_DAYS long.
    """
    hv_21  = compute_hv(bar_closes, HV_SHORT_WINDOW)
    hv_252 = compute_hv(bar_closes, min(HV_LOOKBACK_DAYS, len(bar_closes) - 1))

    # Use rolling 21-day HV as the historical IV proxy series for ranking
    rolling = _rolling_hv(bar_closes, HV_SHORT_WINDOW)
    if not rolling or max(rolling) == min(rolling):
        iv_rank = 0.5  # not enough data — treat as normal
    else:
        lo = min(rolling)
        hi = max(rolling)
        iv_rank = (iv_current - lo) / (hi - lo)
        iv_rank = max(0.0, min(1.0, iv_rank))  # clamp to [0, 1]

    return {
        "hv_21": round(hv_21, 4),
        "hv_252": round(hv_252, 4),
        "iv_current": round(iv_current, 4),
        "iv_rank": round(iv_rank, 4),
        "regime": classify_regime(iv_rank),
    }


def classify_regime(iv_rank: float) -> str:
    """
    low:    iv_rank < IV_RANK_MIN_ENTRY      (premiums thin — skip new entries)
    panic:  iv_rank >= IV_RANK_PANIC         (gap risk — block entries, allow closes)
    high:   between preferred and panic      (optimal premium selling zone)
    normal: between min_entry and preferred
    """
    if iv_rank < IV_RANK_MIN_ENTRY:
        return "low"
    if iv_rank >= IV_RANK_PANIC:
        return "panic"
    if iv_rank >= 0.50:
        return "high"
    return "normal"


def load_vol_cache() -> dict:
    """Read volatility_cache.json. Returns {} if missing or stale (different date)."""
    try:
        with open(CACHE_FILE) as f:
            cache = json.load(f)
        if is_cache_stale(cache):
            return {}
        return cache
    except Exception:
        return {}


def is_cache_stale(cache: dict) -> bool:
    """True if cache is missing a date or its date differs from today."""
    cached_date = cache.get("date")
    return cached_date != date.today().isoformat()


def build_vol_cache(symbol_bars: dict, atm_ivs: dict | None = None) -> dict:
    """
    Build the daily volatility cache from bar history.

    symbol_bars: {symbol: [{"c": close, ...}, ...]} with up to 252 daily bars
    atm_ivs:     {symbol: iv_float} — ATM IV from option chain at cache-build time.
                 If absent for a symbol, uses hv_21 as a proxy for iv_current.

    Returns cache dict ready to be saved to CACHE_FILE.
    """
    if atm_ivs is None:
        atm_ivs = {}

    symbols_data = {}
    for symbol, bars in symbol_bars.items():
        closes = [b["c"] for b in bars if "c" in b]
        if len(closes) < HV_SHORT_WINDOW + 2:
            continue
        iv_current = atm_ivs.get(symbol) or compute_hv(closes, HV_SHORT_WINDOW)
        result = compute_iv_rank(iv_current, closes)
        symbols_data[symbol] = result

    return {
        "date": date.today().isoformat(),
        "symbols": symbols_data,
    }


def save_vol_cache(cache: dict) -> None:
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)
