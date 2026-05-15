"""Unit tests for put_spread_seller.py."""
from unittest.mock import patch

import pytest

import put_spread_seller
from fixtures import (
    TICKER, EXPIRY_OCC, EXPIRY_ISO,
    make_state, make_trading_days,
)

CURRENT_PRICE = 280.0
PORTFOLIO_VALUE = 100_000.0

SHORT_STRIKE = 252   # round(280 * 0.90) = 252
LONG_STRIKE  = 242   # 252 - 10 (SPREAD_WIDTH_DEFAULT)

SHORT_SYM = f"{TICKER}{EXPIRY_OCC}P{int(SHORT_STRIKE * 1000):08d}"
LONG_SYM  = f"{TICKER}{EXPIRY_OCC}P{int(LONG_STRIKE  * 1000):08d}"

TRADING_DAYS = make_trading_days()


def _puts_chain(
    short_bid=3.00, short_ask=3.30, short_delta=0.25,
    long_bid=0.30, long_ask=0.40,
    short_strike=SHORT_STRIKE, long_strike=LONG_STRIKE,
):
    """Minimal put chain with both legs in the right expiry."""
    return {
        SHORT_SYM: {
            "latestQuote": {"bp": short_bid, "ap": short_ask},
            "greeks": {"delta": -short_delta},
            "impliedVolatility": 0.35,
        },
        LONG_SYM: {
            "latestQuote": {"bp": long_bid, "ap": long_ask},
            "greeks": {"delta": -0.10},
            "impliedVolatility": 0.32,
        },
    }


def _spread_state(order_status="pending_fill"):
    net_credit = 2.60
    return {
        "id": f"{TICKER}-put-spread-test-{SHORT_STRIKE}-{LONG_STRIKE}",
        "symbol": TICKER,
        "strategy": "put_spread",
        "stage": "open",
        "short_contract": SHORT_SYM,
        "long_contract": LONG_SYM,
        "short_strike": SHORT_STRIKE,
        "long_strike": LONG_STRIKE,
        "spread_width": 10,
        "net_credit": net_credit,
        "max_risk": round((10 * 100) - (net_credit * 100), 2),
        "expiry_date": EXPIRY_ISO,
        "cycle_start": "2026-05-14",
        "order_status": order_status,
        "adjustment_count": 0,
        "entry_iv_rank": 0.65,
        "entry_delta_short": 0.25,
    }


def _run(
    symbol=TICKER,
    current_price=CURRENT_PRICE,
    puts=None,
    state=None,
    portfolio_value=PORTFOLIO_VALUE,
    iv_rank=0.60,
    dry_run=False,
):
    if puts is None:
        puts = _puts_chain()
    if state is None:
        state = make_state()

    with (
        patch("put_spread_seller.load_state", return_value=state),
        patch("put_spread_seller.save_state"),
        patch("put_spread_seller.append_log"),
    ):
        return put_spread_seller.run(
            symbol, current_price, puts, TRADING_DAYS,
            portfolio_value=portfolio_value, iv_rank=iv_rank, dry_run=dry_run,
        )


# ── TestSpreadConstruction ────────────────────────────────────────────────────

class TestSpreadConstruction:
    def test_returns_mleg_order(self):
        result = _run(dry_run=False)
        assert result is not None
        assert result["_mcp_call"] == "place_option_order"
        assert result["order_class"] == "mleg"

    def test_has_two_legs(self):
        result = _run(dry_run=False)
        assert len(result["legs"]) == 2

    def test_short_leg_is_sell_to_open(self):
        result = _run(dry_run=False)
        short_leg = next(l for l in result["legs"] if l["side"] == "sell")
        assert short_leg["position_intent"] == "sell_to_open"
        assert short_leg["symbol"] == SHORT_SYM

    def test_long_leg_is_buy_to_open(self):
        result = _run(dry_run=False)
        long_leg = next(l for l in result["legs"] if l["side"] == "buy")
        assert long_leg["position_intent"] == "buy_to_open"
        assert long_leg["symbol"] == LONG_SYM

    def test_limit_price_is_negative_credit(self):
        # short_bid=3.00, long_ask=0.40 → net_credit=2.60 → limit_price=-2.60
        result = _run(dry_run=False)
        limit = float(result["limit_price"])
        assert limit == pytest.approx(-2.60, abs=0.01)

    def test_meta_contains_max_risk(self):
        result = _run(dry_run=False)
        assert "max_risk" in result["_meta"]
        assert result["_meta"]["max_risk"] > 0

    def test_meta_action_is_open_put_spread(self):
        result = _run(dry_run=False)
        assert result["_meta"]["action"] == "open_put_spread"


# ── TestNetCreditMinimum ──────────────────────────────────────────────────────

class TestNetCreditMinimum:
    def test_insufficient_credit_returns_none(self):
        # min_credit = 0.25 × 10 × 100 / 100 = 2.50 per share
        # net = 0.10 - 0.08 = 0.02 << 2.50 → blocked
        puts = _puts_chain(short_bid=0.10, short_ask=0.20, long_bid=0.08, long_ask=0.08)
        result = _run(puts=puts)
        assert result is None

    def test_sufficient_credit_passes(self):
        # Use very high bid/ask to ensure net credit exceeds any minimum
        puts = _puts_chain(short_bid=5.00, short_ask=5.50, long_bid=0.20, long_ask=0.30)
        result = _run(puts=puts, dry_run=True)
        assert result is not None


# ── TestCapitalGuards ─────────────────────────────────────────────────────────

class TestCapitalGuards:
    def test_position_cap_blocks_oversized_risk(self):
        # max_risk ≈ (10*100) - net_credit*100. With tiny portfolio, this exceeds 10% cap.
        puts = _puts_chain(short_bid=5.00, short_ask=5.50, long_bid=0.20, long_ask=0.30)
        result = _run(puts=puts, portfolio_value=500)  # $500 portfolio, 10% = $50 — max_risk ~$550
        assert result is None

    def test_bp_committed_cap_blocks_when_full(self):
        # Already have 50% committed — new spread would push over MAX_BP_COMMITTED
        puts = _puts_chain(short_bid=5.00, short_ask=5.50, long_bid=0.20, long_ask=0.30)
        existing_spread = _spread_state("filled")
        existing_spread["max_risk"] = 50_001.0   # > 50% of $100k
        state = make_state()
        state["spread_positions"] = [existing_spread]
        result = _run(puts=puts, state=state)
        assert result is None

    def test_uses_max_risk_not_strike_for_cap(self):
        # max_risk for a 10-wide spread is at most $1000 (10*100).
        # Position cap on $100k at 30% = $30k. max_risk << $30k → should pass.
        puts = _puts_chain(short_bid=5.00, short_ask=5.50, long_bid=0.20, long_ask=0.30)
        result = _run(puts=puts, portfolio_value=PORTFOLIO_VALUE, dry_run=True)
        assert result is not None


# ── TestStateSchema ───────────────────────────────────────────────────────────

class TestStateSchema:
    def test_spread_written_to_spread_positions(self):
        saved = {}

        def capture(state):
            saved.update(state)

        state = make_state()
        puts = _puts_chain(short_bid=5.00, short_ask=5.50, long_bid=0.20, long_ask=0.30)
        with (
            patch("put_spread_seller.load_state", return_value=state),
            patch("put_spread_seller.save_state", side_effect=capture),
            patch("put_spread_seller.append_log"),
        ):
            put_spread_seller.run(
                TICKER, CURRENT_PRICE, puts, TRADING_DAYS,
                portfolio_value=PORTFOLIO_VALUE, dry_run=False,
            )

        assert "spread_positions" in saved
        assert len(saved["spread_positions"]) == 1
        pos = saved["spread_positions"][0]
        assert pos["symbol"] == TICKER
        assert pos["strategy"] == "put_spread"
        assert pos["order_status"] == "pending_fill"
        assert pos["short_strike"] > pos["long_strike"]
        assert pos["spread_width"] == 10
        assert "net_credit" in pos
        assert "max_risk" in pos
        assert "entry_iv_rank" in pos
        assert "entry_delta_short" in pos

    def test_max_risk_formula(self):
        saved = {}

        def capture(state):
            saved.update(state)

        state = make_state()
        puts = _puts_chain(short_bid=5.00, short_ask=5.50, long_bid=0.20, long_ask=0.30)
        with (
            patch("put_spread_seller.load_state", return_value=state),
            patch("put_spread_seller.save_state", side_effect=capture),
            patch("put_spread_seller.append_log"),
        ):
            put_spread_seller.run(
                TICKER, CURRENT_PRICE, puts, TRADING_DAYS,
                portfolio_value=PORTFOLIO_VALUE, dry_run=False,
            )

        pos = saved["spread_positions"][0]
        net_credit = pos["net_credit"]
        expected_max_risk = round((10 * 100) - (net_credit * 100), 2)
        assert pos["max_risk"] == pytest.approx(expected_max_risk, abs=0.01)


# ── TestDryRun ────────────────────────────────────────────────────────────────

class TestDryRun:
    def test_dry_run_returns_intent_dict(self):
        puts = _puts_chain(short_bid=5.00, short_ask=5.50, long_bid=0.20, long_ask=0.30)
        result = _run(puts=puts, dry_run=True)
        assert result is not None
        assert result.get("dry_run") is True

    def test_dry_run_contains_key_fields(self):
        puts = _puts_chain(short_bid=5.00, short_ask=5.50, long_bid=0.20, long_ask=0.30)
        result = _run(puts=puts, dry_run=True)
        assert "short_contract" in result
        assert "long_contract" in result
        assert "net_credit" in result
        assert "max_risk" in result

    def test_dry_run_does_not_write_state(self):
        puts = _puts_chain(short_bid=5.00, short_ask=5.50, long_bid=0.20, long_ask=0.30)
        state = make_state()
        with (
            patch("put_spread_seller.load_state", return_value=state),
            patch("put_spread_seller.save_state") as mock_save,
            patch("put_spread_seller.append_log"),
        ):
            put_spread_seller.run(
                TICKER, CURRENT_PRICE, puts, TRADING_DAYS,
                portfolio_value=PORTFOLIO_VALUE, dry_run=True,
            )
        mock_save.assert_not_called()


# ── TestDuplicateGuard ────────────────────────────────────────────────────────

class TestDuplicateGuard:
    def test_blocks_when_spread_pending_fill(self):
        puts = _puts_chain(short_bid=5.00, short_ask=5.50, long_bid=0.20, long_ask=0.30)
        state = make_state()
        state["spread_positions"] = [_spread_state("pending_fill")]
        result = _run(puts=puts, state=state)
        assert result is None

    def test_allows_when_spread_filled(self):
        puts = _puts_chain(short_bid=5.00, short_ask=5.50, long_bid=0.20, long_ask=0.30)
        state = make_state()
        state["spread_positions"] = [_spread_state("filled")]
        result = _run(puts=puts, state=state, dry_run=True)
        assert result is not None
