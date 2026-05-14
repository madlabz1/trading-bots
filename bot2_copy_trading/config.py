# ── Bot 2: Copy Trading Bot — Configuration ───────────────────────────────────

# Alpaca Paper Trading credentials (same account as Bot 1 is fine)
API_KEY    = "PK6XN3NEUNMHWC3WBBXB4LYYYS"
API_SECRET = "2X44uwNNun2Awh3DqAVSaLSibbVnGorbPQsq7bcky6db"

# ── Data Sources ───────────────────────────────────────────────────────────────
# Quiver Quantitative — free, no API key needed, 1000 most recent records
QUIVER_URL = "https://api.quiverquant.com/beta/live/congresstrading"

# ── Trade Settings ─────────────────────────────────────────────────────────────
LOOKBACK_DAYS    = 60    # How far back to scan for trades when selecting politician
TRADE_SIZE_USD   = 500   # Dollar amount to allocate per copied trade
MAX_POSITIONS    = 10    # Max number of simultaneous open positions
SCORE_WINDOW     = 30    # Days to score recent activity (weighted more heavily)

# ── Politician Selection ───────────────────────────────────────────────────────
# Leave blank to let the bot auto-select the most active trader.
# To lock in a specific politician, enter their name exactly as it appears
# in disclosure data e.g. "Michael McCaul" or "Nancy Pelosi"
FORCE_POLITICIAN = ""
