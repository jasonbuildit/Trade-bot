"""Unit tests for roller.py — put and call roll logic."""
from datetime import date, timedelta

import pytest

import roller
from fixtures import TICKER, EXP_DT, TODAY, EXPIRY_OCC as NEW_EXP


def _make_trading_days(n=60):
    days = []
    d = TODAY + timedelta(days=1)
    while len(days) < n:
        if d.weekday() < 5:
            days.append({"date": d.isoformat()})
        d += timedelta(days=1)
    return days


TRADING_DAYS = _make_trading_days()

# Current contract expiring in 5 days (inside any 21-45 window → no later expiry conflict)
NEAR_EXP = (TODAY + timedelta(days=5)).strftime("%y%m%d")
NEAR_EXP_ISO = (TODAY + timedelta(days=5)).isoformat()

CURRENT_PUT = f"{TICKER}{NEAR_EXP}P00260000"   # current short put, strike 260
CURRENT_CALL = f"{TICKER}{NEAR_EXP}C00290000"  # current short call, strike 290

# NEW_EXP is imported from fixtures as EXPIRY_OCC — the first Friday in 21-45 DTE window,
# which matches what roller._find_expiry() returns for the same trading_days list.


def _put_chain(*strikes, bid=2.00):
    return {
        f"{TICKER}{NEW_EXP}P{int(s * 1000):08d}": {
            "latestQuote": {"bp": bid, "ap": bid + 0.10},
        }
        for s in strikes
    }


def _call_chain(*strikes, bid=2.00):
    return {
        f"{TICKER}{NEW_EXP}C{int(s * 1000):08d}": {
            "latestQuote": {"bp": bid, "ap": bid + 0.10},
        }
        for s in strikes
    }


# ── roll_put_down_and_out ─────────────────────────────────────────────────────

class TestRollPutDownAndOut:
    def test_credit_roll_succeeds(self):
        # current_bid (cost to close) = 1.50; new put bid = 2.00 → net credit = 0.50
        puts = _put_chain(260, bid=2.00)
        result = roller.roll_put_down_and_out(
            TICKER, CURRENT_PUT, current_bid=1.50, puts=puts,
            trading_days=TRADING_DAYS, min_credit=0.05,
        )
        assert result is not None
        assert result["_meta"]["net_credit"] == pytest.approx(0.50)

    def test_no_credit_returns_none(self):
        # current_bid = 2.50; new put bid = 2.00 → net credit = -0.50 < 0.05
        puts = _put_chain(260, bid=2.00)
        result = roller.roll_put_down_and_out(
            TICKER, CURRENT_PUT, current_bid=2.50, puts=puts,
            trading_days=TRADING_DAYS,
        )
        assert result is None

    def test_refuses_higher_strike(self):
        # Only a higher-strike put available — should be excluded
        puts = _put_chain(280, bid=3.00)  # 280 > 260 current strike
        result = roller.roll_put_down_and_out(
            TICKER, CURRENT_PUT, current_bid=1.00, puts=puts,
            trading_days=TRADING_DAYS,
        )
        assert result is None

    def test_mleg_order_structure(self):
        puts = _put_chain(260, bid=2.00)
        result = roller.roll_put_down_and_out(
            TICKER, CURRENT_PUT, current_bid=1.50, puts=puts,
            trading_days=TRADING_DAYS,
        )
        assert result["_mcp_call"] == "place_option_order"
        assert result["order_class"] == "mleg"
        legs = result["legs"]
        sides = {leg["side"] for leg in legs}
        assert sides == {"buy", "sell"}
        intents = {leg["position_intent"] for leg in legs}
        assert intents == {"buy_to_close", "sell_to_open"}

    def test_limit_price_is_negative_credit(self):
        puts = _put_chain(260, bid=2.00)
        result = roller.roll_put_down_and_out(
            TICKER, CURRENT_PUT, current_bid=1.50, puts=puts,
            trading_days=TRADING_DAYS,
        )
        assert float(result["limit_price"]) == pytest.approx(-0.50)

    def test_no_later_expiry_returns_none(self):
        # Current contract already has a later expiry than any in trading_days window
        far_exp = (TODAY + timedelta(days=50)).strftime("%y%m%d")
        far_put = f"{TICKER}{far_exp}P00260000"
        puts = _put_chain(260, bid=2.00)
        result = roller.roll_put_down_and_out(
            TICKER, far_put, current_bid=1.50, puts=puts,
            trading_days=TRADING_DAYS,
        )
        assert result is None


# ── roll_call_up_and_out ──────────────────────────────────────────────────────

class TestRollCallUpAndOut:
    def test_credit_roll_up_succeeds(self):
        calls = _call_chain(300, bid=2.00)  # strike 300 > 290 current
        result = roller.roll_call_up_and_out(
            TICKER, CURRENT_CALL, current_bid=1.50, calls=calls,
            trading_days=TRADING_DAYS, effective_basis=265.0,
        )
        assert result is not None
        assert result["_meta"]["net_credit"] == pytest.approx(0.50)

    def test_no_credit_returns_none(self):
        calls = _call_chain(300, bid=1.00)  # 1.00 - 1.50 = -0.50, no credit
        result = roller.roll_call_up_and_out(
            TICKER, CURRENT_CALL, current_bid=1.50, calls=calls,
            trading_days=TRADING_DAYS, effective_basis=265.0,
        )
        assert result is None

    def test_refuses_same_or_lower_strike(self):
        # Only lower-strike calls → excluded
        calls = _call_chain(280, bid=3.00)  # 280 < 290 current
        result = roller.roll_call_up_and_out(
            TICKER, CURRENT_CALL, current_bid=1.00, calls=calls,
            trading_days=TRADING_DAYS, effective_basis=265.0,
        )
        assert result is None

    def test_refuses_strike_below_effective_basis(self):
        # Strike 295 but effective_basis is 300 → excluded
        calls = _call_chain(295, bid=3.00)
        result = roller.roll_call_up_and_out(
            TICKER, CURRENT_CALL, current_bid=1.00, calls=calls,
            trading_days=TRADING_DAYS, effective_basis=300.0,
        )
        assert result is None

    def test_accepts_strike_above_effective_basis(self):
        calls = _call_chain(300, bid=2.00)
        result = roller.roll_call_up_and_out(
            TICKER, CURRENT_CALL, current_bid=1.00, calls=calls,
            trading_days=TRADING_DAYS, effective_basis=265.0,
        )
        assert result is not None
