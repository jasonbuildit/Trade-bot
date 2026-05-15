"""Unit tests for iron_condor.py."""
from unittest.mock import patch

import pytest

import iron_condor
from fixtures import (
    TICKER, EXPIRY_OCC, EXPIRY_ISO,
    make_state, make_trading_days,
)

CURRENT_PRICE   = 500.0
PORTFOLIO_VALUE = 100_000.0
TRADING_DAYS    = make_trading_days()

# Condor legs at delta ~0.16 around $500 spot
# short put at 470 (≈9.4% OTM), long put at 465 (5-wide)
# short call at 530 (≈6% OTM), long call at 535 (5-wide)
SHORT_PUT_STRIKE  = 470
LONG_PUT_STRIKE   = 465
SHORT_CALL_STRIKE = 530
LONG_CALL_STRIKE  = 535

SHORT_PUT_SYM  = f"{TICKER}{EXPIRY_OCC}P{int(SHORT_PUT_STRIKE  * 1000):08d}"
LONG_PUT_SYM   = f"{TICKER}{EXPIRY_OCC}P{int(LONG_PUT_STRIKE   * 1000):08d}"
SHORT_CALL_SYM = f"{TICKER}{EXPIRY_OCC}C{int(SHORT_CALL_STRIKE * 1000):08d}"
LONG_CALL_SYM  = f"{TICKER}{EXPIRY_OCC}C{int(LONG_CALL_STRIKE  * 1000):08d}"


def _puts_chain(
    short_put_bid=2.50, short_put_ask=2.70, short_put_delta=0.16,
    long_put_bid=0.80,  long_put_ask=0.90,  long_put_delta=0.10,
):
    return {
        SHORT_PUT_SYM: {
            "latestQuote": {"bp": short_put_bid, "ap": short_put_ask},
            "greeks": {"delta": -short_put_delta},
        },
        LONG_PUT_SYM: {
            "latestQuote": {"bp": long_put_bid, "ap": long_put_ask},
            "greeks": {"delta": -long_put_delta},
        },
    }


def _calls_chain(
    short_call_bid=2.60, short_call_ask=2.80, short_call_delta=0.16,
    long_call_bid=0.90,  long_call_ask=1.00,  long_call_delta=0.10,
):
    return {
        SHORT_CALL_SYM: {
            "latestQuote": {"bp": short_call_bid, "ap": short_call_ask},
            "greeks": {"delta": short_call_delta},
        },
        LONG_CALL_SYM: {
            "latestQuote": {"bp": long_call_bid, "ap": long_call_ask},
            "greeks": {"delta": long_call_delta},
        },
    }


def _watchlist_eligible():
    return [{"symbol": TICKER, "iron_condor_eligible": True}]


def _watchlist_ineligible():
    return [{"symbol": TICKER, "iron_condor_eligible": False}]


def _run(
    symbol=TICKER,
    current_price=CURRENT_PRICE,
    puts=None,
    calls=None,
    state=None,
    portfolio_value=PORTFOLIO_VALUE,
    iv_rank=0.65,
    dry_run=False,
    eligible=True,
):
    if puts is None:
        puts = _puts_chain()
    if calls is None:
        calls = _calls_chain()
    if state is None:
        state = make_state()

    watchlist = _watchlist_eligible() if eligible else _watchlist_ineligible()

    with (
        patch("iron_condor.load_state", return_value=state),
        patch("iron_condor.save_state"),
        patch("iron_condor.append_log"),
        patch("iron_condor._is_condor_eligible", return_value=eligible),
    ):
        return iron_condor.run(
            symbol, current_price, puts, calls, TRADING_DAYS,
            portfolio_value=portfolio_value, iv_rank=iv_rank, dry_run=dry_run,
        )


# ── TestCondorEntry ───────────────────────────────────────────────────────────

class TestCondorEntry:
    def test_returns_mleg_order(self):
        result = _run(dry_run=False)
        assert result is not None
        assert result["_mcp_call"] == "place_option_order"
        assert result["order_class"] == "mleg"

    def test_has_four_legs(self):
        result = _run(dry_run=False)
        assert len(result["legs"]) == 4

    def test_two_sell_and_two_buy_legs(self):
        result = _run(dry_run=False)
        sells = [l for l in result["legs"] if l["side"] == "sell"]
        buys  = [l for l in result["legs"] if l["side"] == "buy"]
        assert len(sells) == 2
        assert len(buys)  == 2

    def test_sell_legs_are_sell_to_open(self):
        result = _run(dry_run=False)
        for leg in result["legs"]:
            if leg["side"] == "sell":
                assert leg["position_intent"] == "sell_to_open"

    def test_buy_legs_are_buy_to_open(self):
        result = _run(dry_run=False)
        for leg in result["legs"]:
            if leg["side"] == "buy":
                assert leg["position_intent"] == "buy_to_open"

    def test_limit_price_is_negative_credit(self):
        # short_put_bid=2.50, long_put_ask=0.90 → put_credit=1.60
        # short_call_bid=2.60, long_call_ask=1.00 → call_credit=1.60
        # net_credit=3.20 → limit_price=-3.20
        result = _run(dry_run=False)
        limit = float(result["limit_price"])
        assert limit < 0  # must be negative (credit)

    def test_meta_action_is_open_iron_condor(self):
        result = _run(dry_run=False)
        assert result["_meta"]["action"] == "open_iron_condor"

    def test_meta_contains_all_four_contracts(self):
        result = _run(dry_run=False)
        meta = result["_meta"]
        assert "short_put_contract"  in meta
        assert "long_put_contract"   in meta
        assert "short_call_contract" in meta
        assert "long_call_contract"  in meta

    def test_dry_run_returns_intent(self):
        result = _run(dry_run=True)
        assert result is not None
        assert result.get("dry_run") is True
        assert "net_credit" in result
        assert "max_risk" in result


class TestCondorEntryGates:
    def test_ineligible_symbol_returns_none(self):
        result = _run(eligible=False)
        assert result is None

    def test_low_iv_rank_returns_none(self):
        # IV_RANK_PREFERRED_MIN = 0.50; iv_rank=0.30 → blocked
        result = _run(iv_rank=0.30)
        assert result is None

    def test_iv_rank_at_preferred_min_passes(self):
        result = _run(iv_rank=0.50, dry_run=True)
        assert result is not None

    def test_insufficient_credit_returns_none(self):
        # min_credit = 0.25 × 5 = 1.25/share; net = 0.05+0.05=0.10 → blocked
        puts  = _puts_chain(short_put_bid=0.10, long_put_ask=0.05)
        calls = _calls_chain(short_call_bid=0.10, long_call_ask=0.05)
        result = _run(puts=puts, calls=calls)
        assert result is None

    def test_duplicate_pending_blocks_new_condor(self):
        state = make_state()
        state["condor_positions"] = [{
            "id": "dupe", "symbol": TICKER, "order_status": "pending_fill"
        }]
        result = _run(state=state, dry_run=True)
        assert result is None

    def test_bp_committed_cap_blocks_when_full(self):
        state = make_state()
        state["condor_positions"] = [{"id": "x", "symbol": "SPY", "max_risk": 51_000.0}]
        result = _run(state=state)
        assert result is None


class TestCondorStateSchema:
    def test_condor_written_to_condor_positions(self):
        saved = {}

        def capture(s):
            saved.update(s)

        state = make_state()
        with (
            patch("iron_condor.load_state", return_value=state),
            patch("iron_condor.save_state", side_effect=capture),
            patch("iron_condor.append_log"),
            patch("iron_condor._is_condor_eligible", return_value=True),
        ):
            iron_condor.run(
                TICKER, CURRENT_PRICE, _puts_chain(), _calls_chain(), TRADING_DAYS,
                portfolio_value=PORTFOLIO_VALUE, iv_rank=0.65, dry_run=False,
            )

        assert "condor_positions" in saved
        assert len(saved["condor_positions"]) == 1
        pos = saved["condor_positions"][0]
        assert pos["symbol"] == TICKER
        assert pos["strategy"] == "iron_condor"
        assert pos["order_status"] == "pending_fill"
        assert "net_credit" in pos
        assert "max_risk" in pos
        assert "entry_iv_rank" in pos

    def test_max_risk_formula(self):
        saved = {}

        def capture(s):
            saved.update(s)

        state = make_state()
        with (
            patch("iron_condor.load_state", return_value=state),
            patch("iron_condor.save_state", side_effect=capture),
            patch("iron_condor.append_log"),
            patch("iron_condor._is_condor_eligible", return_value=True),
        ):
            iron_condor.run(
                TICKER, CURRENT_PRICE, _puts_chain(), _calls_chain(), TRADING_DAYS,
                portfolio_value=PORTFOLIO_VALUE, iv_rank=0.65, dry_run=False,
            )

        pos = saved["condor_positions"][0]
        net_credit = pos["net_credit"]
        # max_risk = (CONDOR_WING_WIDTH × 100) - (net_credit × 100)
        from config import CONDOR_WING_WIDTH
        expected = round((CONDOR_WING_WIDTH * 100) - (net_credit * 100), 2)
        assert pos["max_risk"] == pytest.approx(expected, abs=0.01)
