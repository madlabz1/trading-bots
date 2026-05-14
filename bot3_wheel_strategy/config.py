# ── Bot 3: Wheel Strategy Bot — Configuration ─────────────────────────────────

# Alpaca Paper Trading credentials
API_KEY    = "PK6XN3NEUNMHWC3WBBXB4LYYYS"
API_SECRET = "2X44uwNNun2Awh3DqAVSaLSibbVnGorbPQsq7bcky6db"

# ── Stock Settings ─────────────────────────────────────────────────────────────
# NVDA chosen: high options liquidity, ~$100-115/share fits $10k budget perfectly
# (100 shares at $97 strike = $9,700 cash secured — right in budget)
SYMBOL = "NVDA"

# ── Stage 1: Sell Cash-Secured Put ────────────────────────────────────────────
PUT_OTM_PCT      = 0.10   # Sell put 10% below current price
MIN_DTE          = 14     # Minimum days to expiry (don't sell < 2 weeks)
MAX_DTE          = 28     # Maximum days to expiry (don't sell > 4 weeks)

# ── Stage 2: Sell Covered Call ────────────────────────────────────────────────
CALL_OTM_PCT     = 0.10   # Sell call 10% above cost basis

# ── Exit Rule ─────────────────────────────────────────────────────────────────
# Close any option early if it hits 50% profit (lock in gains, free up capital)
EARLY_CLOSE_PCT  = 0.50   # Close when unrealized profit >= 50% of premium received

# ── Scheduling ─────────────────────────────────────────────────────────────────
SUMMARY_HOUR     = 15     # Daily summary logged at 3:50 PM ET
SUMMARY_MINUTE   = 50
