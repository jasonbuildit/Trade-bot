"""
State integrity checker — run before market open or after any manual state edit.

Usage:
    cd C:\\workspace\\Trade-bot\\wheel
    python check_state.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
STATE_FILE = ROOT / "wheel" / "state.json"
WATCHLIST_FILE = ROOT / "wheel" / "watchlist.json"


def main():
    errors = []
    warnings = []

    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
    except Exception as e:
        print(f"FATAL: cannot load state.json: {e}")
        sys.exit(1)

    try:
        with open(WATCHLIST_FILE) as f:
            wl_data = json.load(f)
        enabled_symbols = {
            s["symbol"] for s in wl_data.get("symbols", [])
            if isinstance(s, dict) and s.get("enabled", True)
        }
    except Exception as e:
        warnings.append(f"Cannot load watchlist.json: {e}")
        enabled_symbols = set()

    symbols = state.get("symbols", {})
    summary = state.get("account_summary", {})

    # 1. Valid stage values
    for sym, s in symbols.items():
        stage = s.get("stage")
        if stage not in (1, 2):
            errors.append(f"{sym}: invalid stage {stage!r} (must be 1 or 2)")

    # 2. Stage 1 integrity
    for sym, s in symbols.items():
        if s.get("stage") != 1:
            continue
        if not s.get("option_symbol"):
            warnings.append(f"{sym}: Stage 1 but no option_symbol set")
        if s.get("order_status") == "pending_fill":
            warnings.append(
                f"{sym}: order {s.get('option_symbol')} is still pending_fill — "
                f"confirm fill status before trading"
            )
        if s.get("shares_qty", 0) >= 100:
            errors.append(
                f"{sym}: Stage 1 but shares_qty={s.get('shares_qty')} — "
                f"should be Stage 2 if assigned"
            )

    # 3. Stage 2 integrity
    for sym, s in symbols.items():
        if s.get("stage") != 2:
            continue
        if not s.get("cost_basis"):
            errors.append(f"{sym}: Stage 2 but no cost_basis")
        shares = s.get("shares_qty", 0)
        if shares < 100:
            errors.append(f"{sym}: Stage 2 but shares_qty={shares} (expected ≥100)")

    # 4. State symbols vs enabled watchlist
    if enabled_symbols:
        for sym in symbols:
            if sym not in enabled_symbols:
                warnings.append(
                    f"{sym}: tracked in state but not in enabled watchlist — "
                    f"was it disabled? Consider removing manually."
                )

    # 5. Premium total consistency
    computed = round(sum(s.get("total_premium_all_cycles", 0) for s in symbols.values()), 4)
    stored = round(summary.get("total_premium_collected", 0), 4)
    if abs(computed - stored) > 0.01:
        warnings.append(
            f"total_premium_collected mismatch: "
            f"stored {stored:.4f} vs computed from symbols {computed:.4f}"
        )

    # ── Report ────────────────────────────────────────────────────────────────
    print(f"\n{'='*55}")
    print(f"  STATE INTEGRITY CHECK")
    print(f"  {STATE_FILE}")
    print(f"{'='*55}")
    print(f"  Symbols tracked : {len(symbols)}")
    print(f"  Total premium   : ${stored:.4f}")
    print(f"  Peak equity     : {summary.get('peak_equity') or 'not set'}")
    print(f"  Last summary    : {summary.get('last_daily_summary') or 'never'}")

    if symbols:
        print()
        for sym, s in symbols.items():
            stage_label = "Stage 1 (short put)" if s.get("stage") == 1 else "Stage 2 (covered call)"
            status = s.get("order_status", "—")
            opt = s.get("option_symbol", "—")
            expiry = s.get("expiry_date", "—")
            print(f"  {sym:6s}  {stage_label} | {opt} | exp {expiry} | {status}")
    else:
        print("\n  No active positions — clean state, ready for screener")

    if errors:
        print(f"\n  ERRORS ({len(errors)}):")
        for e in errors:
            print(f"    ✗ {e}")

    if warnings:
        print(f"\n  WARNINGS ({len(warnings)}):")
        for w in warnings:
            print(f"    ! {w}")

    if not errors and not warnings:
        print("\n  ✓ State is clean")

    print(f"{'='*55}\n")

    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
