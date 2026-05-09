"""
OCC contract symbol utilities shared across wheel modules.

OCC format: {TICKER}{YYMMDD}{C|P}{8-digit-strike*1000}
  strike  = int(contract[-8:]) / 1000
  expiry  = "20" + contract[len(ticker):len(ticker)+6]  → YYYY-MM-DD
"""
from datetime import date


def parse_strike(contract: str) -> float:
    return int(contract[-8:]) / 1000


def parse_expiry(contract: str, symbol: str) -> str:
    exp = "20" + contract[len(symbol):len(symbol) + 6]
    return f"{exp[:4]}-{exp[4:6]}-{exp[6:8]}"


def expiry_tag(expiry_iso: str) -> str:
    """Convert ISO date to OCC YYMMDD tag for substring matching in contract symbols."""
    return expiry_iso.replace("-", "")[2:]


def find_expiry(trading_days: list, min_dte: int = 21, max_dte: int = 45) -> str | None:
    """Return the first Friday (preferred) or any trading day in the DTE window."""
    today = date.today()
    for day in trading_days:
        d = date.fromisoformat(day["date"])
        delta = (d - today).days
        if min_dte <= delta <= max_dte and d.weekday() == 4:
            return day["date"]
    for day in trading_days:
        d = date.fromisoformat(day["date"])
        delta = (d - today).days
        if min_dte <= delta <= max_dte:
            return day["date"]
    return None
