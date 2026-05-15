"""Unit tests for spread_monitor.py."""
from datetime import date, timedelta
from unittest.mock import patch

import pytest

import spread_monitor
from fixtures import (
    TICKER, EXPIRY_OCC, EXPIRY_ISO,
    make_state, make_trading_days,
)

TODAY = date.today()

SHORT_STRIKE = 252
LONG_STRIKE  = 242
SHORT_SYM = f"{TICKER}{EXPIRY_OCC}P{int(SHORT_STRIKE * 1000):08d}"
LONG_SYM  = f"{TICKER}{EXPIRY_OCC}P{int(LONG_STRIKE  * 1000):08d}"

NET_CREDIT = 2.60
MAX_RISK   = round((10 * 100) - NET_CREDIT * 100, 2)  # 740.0
SPREAD_ID  = f"{TICKER}-put-spread-test-{SHORT_STRIKE}-{LONG_STRIKE}"

TRADING_DAYS = make_trading_days()


def _spread(
    order_status="filled",
    net_credit=NET_CREDIT,
    max_risk=MAX_RISK,
    expiry_date=EXPIRY_ISO,
):
    return {
        "id": SPREAD_ID,
        "symbol": TICKER,
        "strategy": "put_spread",
        "stage": "open",
        "short_contract": SHORT_SYM,
        "long_contract": LONG_SYM,
        "short_strike": SHORT_STRIKE,
        "long_strike": LONG_STRIKE,
        "spread_width": 10,
        "net_credit": net_credit,
        "max_risk": max_risk,
        "expiry_date": expiry_date,
        "cycle_start": TODAY.isoformat(),
        "order_status": order_status,
        "adjustment_count": 0,
        "entry_iv_rank": 0.65,
        "entry_delta_short": 0.25,
    }


def _chains(short_ask=1.30, long_bid=0.20):
    """option_chains dict with both spread contracts quoted."""
    return {
        TICKER: {
            "puts": {
                SHORT_SYM: {"latestQuote": {"bp": short_ask * 0.9, "ap": short_ask}},
                LONG_SYM:  {"latestQuote": {"bp": long_bid, "ap": long_bid * 1.1}},
            },
            "calls": {},
        }
    }


def _positions():
    """Live positions containing both legs (spread is filled)."""
    return [
        {"symbol": SHORT_SYM, "asset_class": "us_option",
         "qty": "-1", "avg_entry_price": str(NET_CREDIT)},
        {"symbol": LONG_SYM, "asset_class": "us_option",
         "qty": "1", "avg_entry_price": "0.40"},
    ]


def _run(spread, chains=None, positions=None, dry_run=False, state=None):
    if chains is None:
        chains = _chains()
    if positions is None:
        positions = _positions()
    if state is None:
        state = make_state()
        state["spread_positions"] = [spread]

    with (
        patch("spread_monitor.load_state", return_value=state),
        patch("spread_monitor.save_state"),
        patch("spread_monitor.append_log"),
    ):
        return spread_monitor.process(spread, chains, positions, TRADING_DAYS, dry_run=dry_run)


# ── TestFillReconciliation ────────────────────────────────────────────────────

class TestFillReconciliation:
    def test_pending_fill_confirmed_when_contract_in_positions(self):
        spread = _spread(order_status="pending_fill")
        saved = {}

        def capture(s):
            saved.update(s)

        state = make_state()
        state["spread_positions"] = [spread]
        with (
            patch("spread_monitor.load_state", return_value=state),
            patch("spread_monitor.save_state", side_effect=capture),
            patch("spread_monitor.append_log"),
        ):
            result = spread_monitor.process(spread, _chains(), _positions(), TRADING_DAYS)

        # After fill, state should be updated
        assert saved.get("spread_positions", [{}])[0].get("order_status") == "filled"

    def test_pending_fill_returns_no_actions(self):
        spread = _spread(order_status="pending_fill")
        actions = _run(spread)
        assert actions == []

    def test_pending_fill_not_in_positions_skips(self):
        spread = _spread(order_status="pending_fill")
        actions = _run(spread, positions=[])  # contract not in live positions
        assert actions == []


# ── TestProfitClose50 ─────────────────────────────────────────────────────────

class TestProfitClose50:
    def test_close_when_debit_at_50pct_of_credit(self):
        # net_credit=2.60, 50% target = 1.30 debit-to-close
        # short_ask=1.30, long_bid=0.20 → debit = 1.30 - 0.20 = 1.10 < 1.30 ✓
        chains = _chains(short_ask=1.10, long_bid=0.20)
        # debit = 1.10 - 0.20 = 0.90 ≤ 0.50 * 2.60 = 1.30 → profit close
        actions = _run(_spread(), chains=chains)
        assert len(actions) == 1
        assert actions[0]["_mcp_call"] == "place_option_order"
        assert actions[0]["order_class"] == "mleg"

    def test_no_close_when_debit_above_50pct(self):
        # debit = 2.20 - 0.20 = 2.00 > 1.30 → no profit close
        chains = _chains(short_ask=2.20, long_bid=0.20)
        actions = _run(_spread(), chains=chains)
        assert actions == []

    def test_close_legs_are_buy_to_close_and_sell_to_close(self):
        chains = _chains(short_ask=1.10, long_bid=0.20)
        actions = _run(_spread(), chains=chains)
        legs = actions[0]["legs"]
        buy_leg  = next(l for l in legs if l["side"] == "buy")
        sell_leg = next(l for l in legs if l["side"] == "sell")
        assert buy_leg["position_intent"]  == "buy_to_close"
        assert sell_leg["position_intent"] == "sell_to_close"
        assert buy_leg["symbol"]  == SHORT_SYM
        assert sell_leg["symbol"] == LONG_SYM

    def test_meta_reason_contains_profit(self):
        chains = _chains(short_ask=1.10, long_bid=0.20)
        actions = _run(_spread(), chains=chains)
        assert "profit" in actions[0]["_meta"]["reason"].lower()

    def test_dry_run_returns_action_without_state_write(self):
        chains = _chains(short_ask=1.10, long_bid=0.20)
        state = make_state()
        state["spread_positions"] = [_spread()]
        with (
            patch("spread_monitor.load_state", return_value=state),
            patch("spread_monitor.save_state") as mock_save,
            patch("spread_monitor.append_log"),
        ):
            actions = spread_monitor.process(_spread(), chains, _positions(), TRADING_DAYS, dry_run=True)
        assert len(actions) == 1
        mock_save.assert_not_called()


# ── TestLossClose ─────────────────────────────────────────────────────────────

class TestLossClose:
    def test_close_at_100pct_loss(self):
        # SPREAD_MAX_LOSS_MULT=1.00 → close when loss >= max_risk (740)
        # debit-to-close = short_ask - long_bid
        # current_loss = (debit - net_credit) * 100
        # need current_loss >= 740
        # (debit - 2.60) * 100 >= 740 → debit >= 10.00
        # short_ask=9.90, long_bid=0.10 → debit=9.80 → loss=(9.80-2.60)*100=720 < 740
        # short_ask=10.00, long_bid=0.10 → debit=9.90 → loss=(9.90-2.60)*100=730 < 740
        # short_ask=10.00, long_bid=0.00 → debit=10.00 → loss=(10.00-2.60)*100=740 ≥ 740 ✓
        chains = _chains(short_ask=10.00, long_bid=0.00)
        actions = _run(_spread(), chains=chains)
        assert len(actions) == 1
        assert "loss" in actions[0]["_meta"]["reason"].lower()

    def test_no_close_below_loss_limit(self):
        # debit=2.60-0.20=2.40, loss=(2.40-2.60)*100=-20 (profit) → no close
        chains = _chains(short_ask=2.40, long_bid=0.20)
        actions = _run(_spread(), chains=chains)
        assert actions == []

    def test_close_order_is_market_type(self):
        chains = _chains(short_ask=10.00, long_bid=0.00)
        actions = _run(_spread(), chains=chains)
        assert actions[0]["type"] == "market"


# ── TestExpiry ────────────────────────────────────────────────────────────────

class TestExpiry:
    def test_expired_spread_removed_from_state(self):
        past_date = (TODAY - timedelta(days=1)).isoformat()
        saved = {}

        def capture(s):
            saved.update(s)

        spread = _spread(expiry_date=past_date)
        state = make_state()
        state["spread_positions"] = [spread]

        with (
            patch("spread_monitor.load_state", return_value=state),
            patch("spread_monitor.save_state", side_effect=capture),
            patch("spread_monitor.append_log"),
        ):
            actions = spread_monitor.process(spread, _chains(), _positions(), TRADING_DAYS)

        assert saved.get("spread_positions", [None]) == []
        assert any(a.get("action") == "spread_expired" for a in actions)

    def test_expiry_today_is_expired(self):
        today_str = TODAY.isoformat()
        spread = _spread(expiry_date=today_str)
        state = make_state()
        state["spread_positions"] = [spread]

        with (
            patch("spread_monitor.load_state", return_value=state),
            patch("spread_monitor.save_state"),
            patch("spread_monitor.append_log"),
        ):
            actions = spread_monitor.process(spread, _chains(), _positions(), TRADING_DAYS)

        assert any(a.get("action") == "spread_expired" for a in actions)

    def test_future_expiry_not_expired(self):
        future = (TODAY + timedelta(days=30)).isoformat()
        spread = _spread(expiry_date=future)
        # Use chains that produce no profit/loss trigger
        chains = _chains(short_ask=2.00, long_bid=0.20)
        # debit=1.80, profit_target=2.60*0.50=1.30, 1.80>1.30 → no profit close
        # loss=(1.80-2.60)*100=-80 → no loss close
        actions = _run(spread, chains=chains)
        assert not any(a.get("action") == "spread_expired" for a in actions)


# ── TestGammaDTEClose ─────────────────────────────────────────────────────────

class TestGammaDTEClose:
    def test_closes_within_gamma_dte_when_risk_present(self):
        # DTE < GAMMA_DTE_THRESHOLD (7) → close if any risk (debit > 0)
        near_expiry = (TODAY + timedelta(days=3)).isoformat()
        spread = _spread(expiry_date=near_expiry)
        # debit = 1.30 - 0.10 = 1.20 > 0 → has risk → close
        chains = _chains(short_ask=1.30, long_bid=0.10)
        actions = _run(spread, chains=chains)
        assert len(actions) == 1
        assert "gamma" in actions[0]["_meta"]["reason"].lower()

    def test_no_gamma_close_when_dte_high(self):
        # DTE well above threshold — normal profit close rules apply
        far_expiry = (TODAY + timedelta(days=30)).isoformat()
        spread = _spread(expiry_date=far_expiry)
        # debit=2.00 → no profit close (>1.30), no loss close
        chains = _chains(short_ask=2.20, long_bid=0.20)
        actions = _run(spread, chains=chains)
        assert not any("gamma" in a.get("_meta", {}).get("reason", "") for a in actions)
