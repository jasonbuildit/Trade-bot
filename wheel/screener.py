"""
Wheel Strategy Screener
Ranks watchlist candidates by annualized put premium yield and picks the best one to sell.

Filters applied (in order):
  1. Earnings gate     — skip expiries that fall inside earnings window
  2. Delta cap         — skip delta > 0.35 (too ATM) or < 0.16 (too OTM)
  3. Bid-ask spread    — skip spread > 10% of mid (illiquid fill)
  4. Stale OI proxy    — skip if zero volume and stale daily bar
  5. IV floor          — flag if ATM IV < 20% (low premium environment)
  6. Trend filter      — flag if price < SMA-21 (downtrend; auto-skip in strict mode)

Run: python wheel/screener.py
"""
import json
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).parent.parent

from config import DELTA_MIN, DELTA_MAX, SPREAD_MAX, IV_FLOOR
from occ import parse_strike, parse_expiry


def load_watchlist() -> list[dict]:
    with open(ROOT / "wheel" / "watchlist.json") as f:
        data = json.load(f)
    symbols = data.get("symbols", data)
    if symbols and isinstance(symbols[0], str):
        return [{"symbol": s, "enabled": True} for s in symbols]
    return [s for s in symbols if s.get("enabled", True)]


def dte(expiry_str: str) -> int:
    return (date.fromisoformat(expiry_str) - date.today()).days


def _earnings_blocks_expiry(expiry_str: str, earnings_date_str: str | None) -> bool:
    """True if earnings fall within the option's life (sell date → expiry)."""
    if not earnings_date_str:
        return False
    earnings = date.fromisoformat(earnings_date_str)
    expiry = date.fromisoformat(expiry_str)
    return earnings <= expiry


def _is_stale_bar(daily_bar: dict | None) -> bool:
    if not daily_bar:
        return True
    bar_date = date.fromisoformat(daily_bar["t"][:10])
    return (date.today() - bar_date).days > 3


def score_candidate(
    symbol: str,
    current_price: float,
    puts: dict,
    earnings_date: str | None = None,
    strict_trend: bool = True,
    sma21: float | None = None,
) -> list[dict]:
    """
    Score each put contract. Returns list of dicts sorted by annualized yield descending.
    puts: {contract_symbol: snapshot_data}
    """
    target_strike = round(current_price * 0.90)
    results = []
    skipped = {"earnings": 0, "delta": 0, "spread": 0, "stale": 0, "no_bid": 0}

    trend_down = sma21 is not None and current_price < sma21

    for contract, data in puts.items():
        quote = data.get("latestQuote", {})
        bid = quote.get("bp", 0) or 0
        ask = quote.get("ap", 0) or 0
        mid = (bid + ask) / 2 if bid and ask else bid

        if mid <= 0:
            skipped["no_bid"] += 1
            continue

        try:
            strike = parse_strike(contract)
        except Exception:
            continue

        try:
            expiry_date_str = parse_expiry(contract, symbol)
            days = dte(expiry_date_str)
        except Exception:
            days = None

        # ── Filter 1: Earnings gate ──────────────────────────────────────────
        if _earnings_blocks_expiry(expiry_date_str, earnings_date):
            skipped["earnings"] += 1
            continue

        # ── Filter 2: Delta cap ──────────────────────────────────────────────
        delta = abs(data.get("greeks", {}).get("delta", 0) or 0)
        if delta > DELTA_MAX or (delta > 0 and delta < DELTA_MIN):
            skipped["delta"] += 1
            continue

        # ── Filter 3: Bid-ask spread ─────────────────────────────────────────
        spread_pct = (ask - bid) / mid if mid > 0 else 1.0
        if spread_pct > SPREAD_MAX:
            skipped["spread"] += 1
            continue

        # ── Filter 4: Stale OI proxy ─────────────────────────────────────────
        daily_bar = data.get("dailyBar")
        if daily_bar and daily_bar.get("v", 0) == 0 and _is_stale_bar(daily_bar):
            skipped["stale"] += 1
            continue

        iv = data.get("impliedVolatility", 0) or 0
        iv_low = iv > 0 and iv < IV_FLOOR

        # Annualized yield (primary sort key)
        annualized_yield = (mid / strike) * (365 / days) * 100 if days and days > 0 else 0
        raw_yield_pct = (mid / strike) * 100

        flags = []
        if iv_low:
            flags.append("IV_LOW")
        if trend_down:
            flags.append("TREND_DOWN")

        # In strict mode, skip trend-down and IV-low
        if strict_trend and ("TREND_DOWN" in flags or "IV_LOW" in flags):
            continue

        results.append({
            "symbol": symbol,
            "contract": contract,
            "strike": strike,
            "mid": round(mid, 2),
            "bid": bid,
            "ask": ask,
            "dte": days,
            "expiry": expiry_date_str,
            "yield_pct": round(raw_yield_pct, 3),
            "ann_yield": round(annualized_yield, 2),
            "iv": round(iv * 100, 1),
            "delta": round(delta, 3),
            "spread_pct": round(spread_pct * 100, 1),
            "distance_from_target": abs(strike - target_strike),
            "flags": flags,
        })

    if skipped["earnings"]:
        print(f"  [{symbol}] earnings gate skipped {skipped['earnings']} contracts")
    if skipped["delta"]:
        print(f"  [{symbol}] delta cap skipped {skipped['delta']} contracts")
    if skipped["spread"]:
        print(f"  [{symbol}] spread filter skipped {skipped['spread']} contracts")

    results.sort(key=lambda x: (-x["ann_yield"], x["distance_from_target"]))
    return results


def print_table(rows: list[dict]):
    if not rows:
        print("  (no candidates passed filters)")
        return
    header = (
        f"  {'Symbol':<6} {'Contract':<25} {'Strike':>7} {'Mid':>5} "
        f"{'Ann%':>6} {'Yld%':>6} {'IV%':>6} {'Delta':>6} {'Sprd%':>6} {'DTE':>4} {'Flags'}"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))
    for r in rows[:5]:
        flags = ",".join(r["flags"]) if r["flags"] else ""
        print(
            f"  {r['symbol']:<6} {r['contract']:<25} {r['strike']:>7.0f} {r['mid']:>5.2f} "
            f"{r['ann_yield']:>6.2f} {r['yield_pct']:>6.3f} {r['iv']:>6.1f} "
            f"{r['delta']:>6.3f} {r['spread_pct']:>6.1f} {r['dte']:>4}  {flags}"
        )


def run(
    snapshots: dict,
    option_chains: dict,
    bars: dict | None = None,
    strict_trend: bool = True,
) -> dict | None:
    """
    snapshots:     {symbol: snapshot_data}  — from mcp__alpaca__get_stock_snapshot
    option_chains: {symbol: puts_dict}      — from mcp__alpaca__get_option_chain per symbol
    bars:          {symbol: [bar, ...]}     — from mcp__alpaca__get_stock_bars (21 daily bars)
    strict_trend:  if True, auto-skip trend-down and IV-low symbols

    Returns: best candidate dict or None
    """
    watchlist = load_watchlist()
    sym_meta = {s["symbol"]: s for s in watchlist}
    all_candidates = []

    for item in watchlist:
        symbol = item["symbol"]

        snap = snapshots.get(symbol)
        if not snap:
            print(f"\n{symbol}: no snapshot — skipping")
            continue

        current_price = (
            snap.get("latestTrade", {}).get("p")
            or snap.get("latestQuote", {}).get("ap")
        )
        if not current_price:
            print(f"\n{symbol}: no price data — skipping")
            continue

        puts = option_chains.get(symbol, {})
        if not puts:
            print(f"\n{symbol} @ ${current_price:.2f}: no option chain returned — skipping")
            continue

        # SMA-21 from daily bars if provided
        sma21 = None
        if bars and symbol in bars:
            bar_list = bars[symbol]
            if len(bar_list) >= 5:
                closes = [b["c"] for b in bar_list]
                sma21 = sum(closes) / len(closes)

        earnings_date = item.get("earnings_date")
        candidates = score_candidate(
            symbol, current_price, puts,
            earnings_date=earnings_date,
            strict_trend=strict_trend,
            sma21=sma21,
        )

        target = round(current_price * 0.90)
        sma_str = f" | SMA21 ${sma21:.2f}" if sma21 else ""
        trend_str = " ⚠ DOWNTREND" if (sma21 and current_price < sma21) else ""
        print(f"\n{symbol} @ ${current_price:.2f} — target strike ${target}{sma_str}{trend_str}")
        print_table(candidates)
        all_candidates.extend(candidates)

    if not all_candidates:
        print("\nNo candidates passed all filters.")
        return None

    all_candidates.sort(key=lambda x: -x["ann_yield"])
    best = all_candidates[0]
    print(
        f"\n★ Best pick: {best['symbol']} {best['contract']} | "
        f"Mid ${best['mid']} | Ann yield {best['ann_yield']}% | "
        f"IV {best['iv']}% | Delta {best['delta']} | DTE {best['dte']}"
    )
    return best


if __name__ == "__main__":
    print("screener.py: invoke via Claude Code — MCP data must be injected by Claude.")
    wl = load_watchlist()
    print("Active watchlist:", [s["symbol"] for s in wl])
