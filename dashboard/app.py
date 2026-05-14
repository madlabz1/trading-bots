"""
Trading Dashboard
==================
Reads live data from:
  - Alpaca API (account value, buying power, positions, recent orders)
  - state.json from each bot (stage, premiums, floors, etc.)
  - bot.log from each bot (recent activity)

Run: python app.py
Open: http://localhost:5000
Auto-refreshes every 30 seconds.
"""

import json
import os
from collections import deque
from datetime import datetime

from flask import Flask, jsonify, render_template

from alpaca.trading.client import TradingClient
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest

# ── Config ─────────────────────────────────────────────────────────────────────
API_KEY    = os.environ.get("ALPACA_API_KEY",    "PK6XN3NEUNMHWC3WBBXB4LYYYS")
API_SECRET = os.environ.get("ALPACA_API_SECRET", "2X44uwNNun2Awh3DqAVSaLSibbVnGorbPQsq7bcky6db")

# Local bot directories (only available when running locally)
BASE  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOTS  = {
    "bot1": os.path.join(BASE, "bot1_trailing_stop"),
    "bot2": os.path.join(BASE, "bot2_copy_trading"),
    "bot3": os.path.join(BASE, "bot3_wheel_strategy"),
}

app = Flask(__name__)

# ── Helpers ────────────────────────────────────────────────────────────────────
def load_state(bot_key: str) -> dict:
    path = os.path.join(BOTS[bot_key], "state.json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}

def load_log(bot_key: str, lines: int = 30) -> list[str]:
    path = os.path.join(BOTS[bot_key], "bot.log")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return list(deque(f, lines))

def get_alpaca_data() -> dict:
    try:
        tc   = TradingClient(API_KEY, API_SECRET, paper=True)
        dc   = StockHistoricalDataClient(API_KEY, API_SECRET)
        acct = tc.get_account()

        # Positions
        positions = []
        for p in tc.get_all_positions():
            positions.append({
                "symbol":       p.symbol,
                "qty":          float(p.qty),
                "entry_price":  float(p.avg_entry_price),
                "current_price": float(p.current_price),
                "market_value": float(p.market_value),
                "unrealized_pl": float(p.unrealized_pl),
                "unrealized_plpc": round(float(p.unrealized_plpc) * 100, 2),
            })

        # Recent orders (last 10)
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        orders_raw = tc.get_orders(GetOrdersRequest(status=QueryOrderStatus.ALL, limit=10))
        orders = []
        for o in orders_raw:
            orders.append({
                "symbol":     o.symbol,
                "side":       o.side.value,
                "qty":        float(o.qty or 0),
                "filled_qty": float(o.filled_qty or 0),
                "status":     o.status.value,
                "filled_price": float(o.filled_avg_price) if o.filled_avg_price else None,
                "submitted_at": o.submitted_at.strftime("%d %b %H:%M:%S") if o.submitted_at else "",
            })

        return {
            "portfolio_value": float(acct.portfolio_value),
            "buying_power":    float(acct.buying_power),
            "equity":          float(acct.equity),
            "last_equity":     float(acct.last_equity),
            "daily_pl":        float(acct.equity) - float(acct.last_equity),
            "daily_pl_pct":    round(((float(acct.equity) - float(acct.last_equity)) / float(acct.last_equity)) * 100, 2),
            "positions":       positions,
            "orders":          orders,
            "status":          acct.status.value,
        }
    except Exception as e:
        return {"error": str(e)}

# ── Routes ─────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/data")
def api_data():
    alpaca  = get_alpaca_data()
    b1      = load_state("bot1")
    b2      = load_state("bot2")
    b3      = load_state("bot3")
    log1    = load_log("bot1", 15)
    log2    = load_log("bot2", 15)
    log3    = load_log("bot3", 15)

    return jsonify({
        "timestamp": datetime.now().strftime("%d %b %Y %H:%M:%S"),
        "alpaca":    alpaca,
        "bot1": {
            "name":    "Bot 1 — Trailing Stop",
            "symbol":  "AAPL",
            "state":   b1,
            "log":     log1,
        },
        "bot2": {
            "name":      "Bot 2 — Copy Trading",
            "politician": b2.get("selected_politician", "Selecting..."),
            "state":     b2,
            "log":       log2,
        },
        "bot3": {
            "name":   "Bot 3 — Wheel Strategy",
            "symbol": "NVDA",
            "state":  b3,
            "log":    log3,
        },
    })

if __name__ == "__main__":
    print("Dashboard running at http://localhost:5000")
    app.run(debug=False, port=5000)
