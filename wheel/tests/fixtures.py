"""Shared test fixtures — realistic but minimal Alpaca-shaped dicts."""
from datetime import date, timedelta

TICKER = "AAPL"
TODAY = date.today()

# Use the first Friday in the 21-45 DTE window — matches what _find_expiry() returns,
# so expiry filters in roller/put_seller/call_seller resolve correctly in tests.
def _first_friday_in_window(min_dte=21, max_dte=45):
    d = TODAY + timedelta(days=min_dte)
    while (d - TODAY).days <= max_dte:
        if d.weekday() == 4:
            return d
        d += timedelta(days=1)
    return TODAY + timedelta(days=30)

EXP_DT = _first_friday_in_window()
EXPIRY_OCC = EXP_DT.strftime("%y%m%d")   # e.g. "260605"
EXPIRY_ISO = EXP_DT.isoformat()           # e.g. "2026-06-05"

# OCC symbols
PUT_SYM  = f"{TICKER}{EXPIRY_OCC}P00260000"   # strike 260
CALL_SYM = f"{TICKER}{EXPIRY_OCC}C00290000"   # strike 290

# Earnings well outside expiry window
EARNINGS_AFTER  = (EXP_DT + timedelta(days=60)).isoformat()
EARNINGS_BEFORE = (EXP_DT - timedelta(days=5)).isoformat()


def make_state(symbols=None, peak_equity=None, total_premium=0.0, last_summary=None):
    return {
        "symbols": symbols or {},
        "account_summary": {
            "total_premium_collected": total_premium,
            "last_daily_summary": last_summary,
            "peak_equity": peak_equity,
        },
    }


def put_symbol_state(order_status="filled", option_sym=None):
    return {
        "stage": 1,
        "option_symbol": option_sym or PUT_SYM,
        "premium_collected": 0.96,
        "fill_price": 0.96 if order_status == "filled" else None,
        "entry_price": 270.0,
        "cost_basis": None,
        "shares_qty": 0,
        "cycle_start": TODAY.isoformat(),
        "expiry_date": EXPIRY_ISO,
        "breakeven": 259.04,
        "max_risk": 25904.0,
        "total_premium_all_cycles": 0.96,
        "roll_count": 0,
        "cycle_number": 1,
        "order_status": order_status,
        "adjustment_count": 0,
    }


def call_symbol_state(order_status="filled", option_sym=None):
    return {
        "stage": 2,
        "option_symbol": option_sym or CALL_SYM,
        "premium_collected": 1.05,
        "fill_price": 1.05 if order_status == "filled" else None,
        "entry_price": None,
        "cost_basis": 270.0,
        "shares_qty": 100,
        "cycle_start": TODAY.isoformat(),
        "expiry_date": EXPIRY_ISO,
        "breakeven": None,
        "max_risk": None,
        "total_premium_all_cycles": 2.01,
        "roll_count": 0,
        "cycle_number": 1,
        "order_status": order_status,
        "adjustment_count": 0,
    }


def short_put_position(unrealized_pl=48.0, cost_basis=-96.0, market_value=-48.0):
    """Short put: cost_basis negative (proceeds received), unrealized_pl positive when profitable."""
    return {
        "symbol": PUT_SYM,
        "asset_class": "us_option",
        "qty": "-1",
        "avg_entry_price": "0.96",
        "cost_basis": str(cost_basis),
        "unrealized_pl": str(unrealized_pl),
        "market_value": str(market_value),
    }


def short_call_position(unrealized_pl=52.5, cost_basis=-105.0, market_value=-52.5):
    return {
        "symbol": CALL_SYM,
        "asset_class": "us_option",
        "qty": "-1",
        "avg_entry_price": "1.05",
        "cost_basis": str(cost_basis),
        "unrealized_pl": str(unrealized_pl),
        "market_value": str(market_value),
    }


def share_position(qty="100", avg_entry=270.0):
    return {
        "symbol": TICKER,
        "asset_class": "us_equity",
        "qty": qty,
        "avg_entry_price": str(avg_entry),
        "cost_basis": str(float(avg_entry) * float(qty)),
        "unrealized_pl": "0",
        "market_value": str(float(avg_entry) * float(qty)),
    }


ACCOUNT = {
    "non_marginable_buying_power": "50000",
    "buying_power": "50000",
    "portfolio_value": "100000",
    "equity": "100000",
    "cash": "50000",
}

CLOCK_OPEN = {"is_open": True}
CLOCK_CLOSED = {"is_open": False}


def make_trading_days(n=60):
    """n future weekdays as trading-day dicts."""
    days = []
    d = TODAY + timedelta(days=1)
    while len(days) < n:
        if d.weekday() < 5:
            days.append({"date": d.isoformat()})
        d += timedelta(days=1)
    return days


def put_chain(strike=260, bid=0.90, ask=1.02, delta=0.25, iv=0.35):
    sym = f"{TICKER}{EXPIRY_OCC}P{int(strike * 1000):08d}"
    return {
        sym: {
            "latestQuote": {"bp": bid, "ap": ask},
            "greeks": {"delta": -delta},
            "impliedVolatility": iv,
            "dailyBar": {"t": TODAY.isoformat() + "T00:00:00Z", "v": 100, "c": (bid + ask) / 2},
        }
    }


def call_chain(strike=290, bid=1.00, ask=1.10, delta=0.25, iv=0.35):
    sym = f"{TICKER}{EXPIRY_OCC}C{int(strike * 1000):08d}"
    return {
        sym: {
            "latestQuote": {"bp": bid, "ap": ask},
            "greeks": {"delta": delta},
            "impliedVolatility": iv,
            "dailyBar": {"t": TODAY.isoformat() + "T00:00:00Z", "v": 100, "c": (bid + ask) / 2},
        }
    }


SNAPSHOT = {
    TICKER: {
        "latestTrade": {"p": 280.0},
        "latestQuote": {"ap": 280.1, "bp": 279.9},
    }
}
