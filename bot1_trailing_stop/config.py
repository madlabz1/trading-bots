# ── Bot 1: Trailing Stop Bot — Configuration ──────────────────────────────────
# Fill in your Alpaca Paper Trading credentials below.
# Get them from: alpaca.markets → Paper Trading account → API Keys

API_KEY    = "PK6XN3NEUNMHWC3WBBXB4LYYYS"
API_SECRET = "2X44uwNNun2Awh3DqAVSaLSibbVnGorbPQsq7bcky6db"

# Alpaca paper trading base URL (do not change)
BASE_URL = "https://paper-api.alpaca.markets"

# ── Stock Settings ─────────────────────────────────────────────────────────────
SYMBOL         = "AAPL"   # Stock to trade (chosen: stable, liquid, ~$200/share)
INITIAL_SHARES = 10       # Shares to buy on first run (~$2,000 of your $10k)

# ── Risk / Trailing Settings ───────────────────────────────────────────────────
STOP_LOSS_PCT     = 0.10  # Sell everything if price drops 10% from entry
TRAIL_TRIGGER_PCT = 0.10  # Start trailing once stock is up 10% from entry
TRAIL_FLOOR_PCT   = 0.05  # Floor sits 5% below the highest price seen

# ── Ladder Buy Settings ────────────────────────────────────────────────────────
# If the stock dips, buy more shares at better prices.
# Each level triggers once and is never re-triggered.
LADDER_LEVELS = [
    {"drop_pct": 0.20, "shares": 10},   # Buy 10 more at -20% (~$1,600)
    {"drop_pct": 0.30, "shares": 20},   # Buy 20 more at -30% (~$2,800)
]
# Max possible exposure: 10 + 10 + 20 = 40 shares (~$6,400) — within $10k budget
