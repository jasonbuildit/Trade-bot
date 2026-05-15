"""Unit tests for condor_monitor.py."""
from datetime import date, timedelta
from unittest.mock import patch

import pytest

import condor_monitor
from fixtures import (
    TICKER, EXPIRY_OCC, EXPIRY_ISO,
    make_state, make_trading_days,
)

TODAY      = date.today()
WING_WIDTH = 5

SHORT_PUT_STRIKE  = 470
LONG_PUT_STRIKE   = 465
SHORT_CALL_STRIKE = 530
LONG_CALL_STRIKE  = 535

SHORT_PUT_SYM  = f"{TICKER}{EXPIRY_OCC}P{int(SHORT_PUT_STRIKE  * 1000):08d}"
LONG_PUT_SYM   = f"{TICKER}{EXPIRY_OCC}P{int(LONG_PUT_STRIKE   * 1000):08d}"
SHORT_CALL_SYM = f"{TICKER}{EXPIRY_OCC}C{int(SHORT_CALL_STRIKE * 1000):08d}"
LONG_CALL_SYM  = f"{TICKER}{EXPIRY_OCC}C{int(LONG_CALL_STRIKE  * 1000):08d}"

NET_CREDIT = 3.20
MAX_RISK   = round((WING_WIDTH * 100) - NET_CREDIT * 100, 2)  # 180.0
CONDOR_ID  = f"{TICKER}-condor-test"

TRADING_DAYS = make_trading_days()


def _condor(
    order_status="filled",
    net_credit=NET_CREDIT,
    max_risk=MAX_RISK,
    expiry_date=EXPIRY_ISO,
    wing_width=WING_WIDTH,
):
    return {
        "id": CONDOR_ID,
        "symbol": TICKER,
        "strategy": "iron_condor",
        "stage": "open",
        "short_put_contract":  SHORT_PUT_SYM,
        "long_put_contract":   LONG_PUT_SYM,
        "short_call_contract": SHORT_CALL_SYM,
        "long_call_contract":  LONG_CALL_SYM,
        "short_put_strike":  SHORT_PUT_STRIKE,
        "long_put_strike":   LONG_PUT_STRIKE,
        "short_call_strike": SHORT_CALL_STRIKE,
        "long_call_strike":  LONG_CALL_STRIKE,
        "wing_width": wing_width,
        "net_credit": net_credit,
        "max_risk":   max_risk,
        "expiry_date": expiry_date,
        "cycle_start": TODAY.isoformat(),
        "order_status": order_status,
        "adjustment_count": 0,
    }


def _chains(
    short_put_ask=1.60, long_put_bid=0.50,
    short_call_ask=1.60, long_call_bid=0.50,
):
    """Current option chain quotes for all four legs."""
    return {
        TICKER: {
            "puts": {
                SHORT_PUT_SYM:  {"latestQuote": {"ap": short_put_ask,  "bp": short_put_ask  * 0.9}},
                LONG_PUT_SYM:   {"latestQuote": {"ap": long_put_bid    * 1.1, "bp": long_put_bid}},
            },
            "calls": {
                SHORT_CALL_SYM: {"latestQuote": {"ap": short_call_ask, "bp": short_call_ask * 0.9}},
                LONG_CALL_SYM:  {"latestQuote": {"ap": long_call_bid  * 1.1, "bp": long_call_bid}},
            },
        }
    }


def _positions():
    return [
        {"symbol": SHORT_PUT_SYM,  "qty": "-1", "avg_entry_price": "2.50"},
        {"symbol": LONG_PUT_SYM,   "qty": "1",  "avg_entry_price": "0.90"},
        {"symbol": SHORT_CALL_SYM, "qty": "-1", "avg_entry_price": "2.60"},
        {"symbol": LONG_CALL_SYM,  "qty": "1",  "avg_entry_price": "1.00"},
    ]


def _run(condor_pos=None, chains=None, positions=None, dry_run=False, state=None):
    if condor_pos is None:
        condor_pos = _condor()
    if chains is None:
        chains = _chains()
    if positions is None:
        positions = _positions()
    if state is None:
        state = make_state()
        state["condor_positions"] = [condor_pos]

    with (
        patch("condor_monitor.load_state", return_value=state),
        patch("condor_monitor.save_state"),
        patch("condor_monitor.append_log"),
    ):
        return condor_monitor.process(condor_pos, chains, positions, TRADING_DAYS, dry_run=dry_run)


# ── TestProfitClose50 ─────────────────────────────────────────────────────────

class TestProfitClose50:
    def test_close_at_50pct_profit(self):
        # net_credit=3.20, 50% target = debit <= 1.60
        # put_debit = short_put_ask - long_put_bid = 0.80 - 0.20 = 0.60
        # call_debit = 0.80 - 0.20 = 0.60
        # total_debit = 1.20 <= 1.60 → profit close
        chains = _chains(short_put_ask=0.80, long_put_bid=0.20, short_call_ask=0.80, long_call_bid=0.20)
        actions = _run(chains=chains)
        assert len(actions) == 1
        assert actions[0]["_mcp_call"] == "place_option_order"
        assert actions[0]["order_class"] == "mleg"

    def test_no_close_above_50pct_debit(self):
        # total_debit = 1.60+1.60=3.20 - 0.50-0.50 = too much calculation
        # Use simple: put_debit=1.60-0.50=1.10, call_debit=1.60-0.50=1.10, total=2.20 > 1.60
        chains = _chains(short_put_ask=1.60, long_put_bid=0.50, short_call_ask=1.60, long_call_bid=0.50)
        # total_debit = (1.60-0.50) + (1.60-0.50) = 1.10+1.10=2.20 > 1.60 → no close
        actions = _run(chains=chains)
        assert actions == []

    def test_four_legs_in_close_order(self):
        chains = _chains(short_put_ask=0.80, long_put_bid=0.20, short_call_ask=0.80, long_call_bid=0.20)
        actions = _run(chains=chains)
        assert len(actions[0]["legs"]) == 4

    def test_close_legs_are_buy_to_close_and_sell_to_close(self):
        chains = _chains(short_put_ask=0.80, long_put_bid=0.20, short_call_ask=0.80, long_call_bid=0.20)
        actions = _run(chains=chains)
        buy_legs  = [l for l in actions[0]["legs"] if l["side"] == "buy"]
        sell_legs = [l for l in actions[0]["legs"] if l["side"] == "sell"]
        assert all(l["position_intent"] == "buy_to_close"  for l in buy_legs)
        assert all(l["position_intent"] == "sell_to_close" for l in sell_legs)

    def test_profit_close_reason_in_meta(self):
        chains = _chains(short_put_ask=0.80, long_put_bid=0.20, short_call_ask=0.80, long_call_bid=0.20)
        actions = _run(chains=chains)
        assert "profit" in actions[0]["_meta"]["reason"].lower()

    def test_dry_run_no_state_write(self):
        chains = _chains(short_put_ask=0.80, long_put_bid=0.20, short_call_ask=0.80, long_call_bid=0.20)
        state = make_state()
        state["condor_positions"] = [_condor()]
        with (
            patch("condor_monitor.load_state", return_value=state),
            patch("condor_monitor.save_state") as mock_save,
            patch("condor_monitor.append_log"),
        ):
            actions = condor_monitor.process(_condor(), chains, _positions(), TRADING_DAYS, dry_run=True)
        assert len(actions) == 1
        mock_save.assert_not_called()


# ── TestLossClose ─────────────────────────────────────────────────────────────

class TestLossClose:
    def test_close_at_loss_limit(self):
        # max_risk=180, SPREAD_MAX_LOSS_MULT=1.00 → close when loss >= 180
        # current_loss = (total_debit - net_credit) * 100
        # need (total_debit - 3.20) * 100 >= 180 → total_debit >= 5.00
        # put_debit = 4.00 - 0.20 = 3.80, call_debit = 1.60 - 0.40 = 1.20 → total=5.00
        chains = _chains(short_put_ask=4.00, long_put_bid=0.20, short_call_ask=1.60, long_call_bid=0.40)
        # total_debit = (4.00-0.20) + (1.60-0.40) = 3.80+1.20 = 5.00
        # loss = (5.00 - 3.20) * 100 = 180 >= 180 → close
        actions = _run(chains=chains)
        assert len(actions) == 1
        assert "loss" in actions[0]["_meta"]["reason"].lower()

    def test_no_close_below_loss_limit(self):
        # total_debit = (1.60-0.50)+(1.60-0.50) = 2.20, loss=(2.20-3.20)*100=-100 (profit)
        chains = _chains(short_put_ask=1.60, long_put_bid=0.50, short_call_ask=1.60, long_call_bid=0.50)
        actions = _run(chains=chains)
        assert actions == []

    def test_loss_close_order_is_market(self):
        chains = _chains(short_put_ask=4.00, long_put_bid=0.20, short_call_ask=1.60, long_call_bid=0.40)
        actions = _run(chains=chains)
        assert actions[0]["type"] == "market"


# ── TestExpiry ────────────────────────────────────────────────────────────────

class TestExpiry:
    def test_expired_condor_removed(self):
        past = (TODAY - timedelta(days=1)).isoformat()
        condor_pos = _condor(expiry_date=past)
        saved = {}

        def capture(s):
            saved.update(s)

        state = make_state()
        state["condor_positions"] = [condor_pos]
        with (
            patch("condor_monitor.load_state", return_value=state),
            patch("condor_monitor.save_state", side_effect=capture),
            patch("condor_monitor.append_log"),
        ):
            actions = condor_monitor.process(condor_pos, _chains(), _positions(), TRADING_DAYS)

        assert saved.get("condor_positions", [None]) == []
        assert any(a.get("action") == "condor_expired" for a in actions)

    def test_future_expiry_not_expired(self):
        future = (TODAY + timedelta(days=30)).isoformat()
        condor_pos = _condor(expiry_date=future)
        # chains with no profit/loss trigger
        chains = _chains(short_put_ask=1.60, long_put_bid=0.50, short_call_ask=1.60, long_call_bid=0.50)
        actions = _run(condor_pos=condor_pos, chains=chains)
        assert not any(a.get("action") == "condor_expired" for a in actions)


# ── TestUntestedRoll ──────────────────────────────────────────────────────────

class TestUntestedRoll:
    def test_put_tested_emits_call_roll(self):
        # Put side tested: put_debit > 50% of wing_width×100 = 250
        # put_debit = short_put_ask - long_put_bid
        # need put_debit > 2.50 → short_put_ask=3.50, long_put_bid=0.20 → put_debit=3.30
        # call side healthy: call_debit = 1.60-0.50=1.10 < 2.50 → not tested
        chains = _chains(
            short_put_ask=3.50, long_put_bid=0.20,   # put tested
            short_call_ask=1.60, long_call_bid=0.50,  # call fine
        )
        actions = _run(chains=chains)
        roll_actions = [a for a in actions if a.get("_meta", {}).get("action") == "roll_untested_side"]
        assert len(roll_actions) == 1
        assert roll_actions[0]["_meta"]["side"] == "call"

    def test_call_tested_emits_put_roll(self):
        # Call tested, put healthy
        chains = _chains(
            short_put_ask=1.60, long_put_bid=0.50,    # put fine
            short_call_ask=3.50, long_call_bid=0.20,  # call tested
        )
        actions = _run(chains=chains)
        roll_actions = [a for a in actions if a.get("_meta", {}).get("action") == "roll_untested_side"]
        assert len(roll_actions) == 1
        assert roll_actions[0]["_meta"]["side"] == "put"

    def test_both_tested_no_roll_emitted(self):
        # Both sides tested → proceed to loss close (loss is large)
        chains = _chains(
            short_put_ask=3.50, long_put_bid=0.20,
            short_call_ask=3.50, long_call_bid=0.20,
        )
        # total_debit = (3.50-0.20)+(3.50-0.20) = 3.30+3.30=6.60
        # loss = (6.60-3.20)*100=340 >= max_risk=180 → loss limit fires instead
        actions = _run(chains=chains)
        roll_actions = [a for a in actions if a.get("_meta", {}).get("action") == "roll_untested_side"]
        assert len(roll_actions) == 0  # loss close preempts the roll

    def test_no_roll_when_neither_tested(self):
        chains = _chains(short_put_ask=1.60, long_put_bid=0.50, short_call_ask=1.60, long_call_bid=0.50)
        actions = _run(chains=chains)
        roll_actions = [a for a in actions if a.get("_meta", {}).get("action") == "roll_untested_side"]
        assert len(roll_actions) == 0
