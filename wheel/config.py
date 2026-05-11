"""
Wheel / Flywheel Options Strategy — Central Configuration

All strategy constants live here. Import from this module instead of
repeating magic numbers across files.
"""

# ── Entry filters ────────────────────────────────────────────────────────────
DELTA_MIN = 0.16      # conservative delta floor (below = too far OTM)
DELTA_MAX = 0.35      # delta ceiling (above = too close to ATM)
SPREAD_MAX = 0.10     # max bid-ask spread as % of mid
IV_FLOOR   = 0.20     # minimum ATM implied volatility (annualized)
DTE_MIN    = 21       # minimum days to expiry
DTE_MAX    = 45       # maximum days to expiry

# ── Portfolio-level capital limits ───────────────────────────────────────────
MAX_BP_COMMITTED    = 0.50   # max 50% of portfolio value in total open puts
MIN_CASH_RESERVE_PCT = 0.25  # always keep 25% of portfolio value in cash

# ── Per-position size cap ────────────────────────────────────────────────────
DEFAULT_MAX_POSITION_PCT = 0.10  # 10% of portfolio per individual position

# ── Profit / loss thresholds ─────────────────────────────────────────────────
PROFIT_CLOSE_PCT      = 0.50  # close short option at 50% of max profit
FAST_PROFIT_CLOSE_PCT = 0.75  # close early if 75%+ profit reached (don't wait for pennies)
LOSS_LIMIT_PCT        = 2.00  # close at 200% of premium received as a loss

# ── Portfolio drawdown controls ──────────────────────────────────────────────
DRAWDOWN_PAUSE   = 0.05   # 5%  drawdown: pause new entries, review
DRAWDOWN_REDUCE  = 0.08   # 8%  drawdown: log alert (reduce sizing)
DRAWDOWN_DISABLE = 0.12   # 12% drawdown: disable all new premium selling

# ── Market timing guards ─────────────────────────────────────────────────────
MARKET_OPEN_BUFFER_MIN  = 15  # skip first 15 min after open  (9:30–9:45 ET)
MARKET_CLOSE_BUFFER_MIN = 15  # skip last 15 min before close (3:45–4:00 ET)

# ── Roll thresholds ──────────────────────────────────────────────────────────
ROLL_PUT_THRESH  = 0.03  # roll put when stock within 3% of strike (approaching ITM)
ROLL_CALL_THRESH = 0.05  # roll call up when stock 5%+ above call strike (deeply ITM)

# ── Underlying price health checks ───────────────────────────────────────────
UNDERLYING_WARN_PCT  = 0.08  # warn if stock falls 8% from put entry price
UNDERLYING_BLOCK_PCT = 0.15  # block new puts if stock/assigned shares down 15%

# ── Pre-entry event gate ─────────────────────────────────────────────────────
EARNINGS_PROXIMITY_DAYS = 14  # block redeployment if earnings within 14 days

# ── Sector exposure cap ──────────────────────────────────────────────────────
MAX_SECTOR_EXPOSURE = 0.25  # max 25% of portfolio in puts on correlated names (same sector)

# ── Order execution ladder ───────────────────────────────────────────────────
ORDER_ADJUSTMENT_MAX = 3  # max price adjustments per entry order (one per monitor cycle)
