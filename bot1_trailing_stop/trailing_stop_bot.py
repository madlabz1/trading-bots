"""
Bot 1: Trailing Stop Bot
========================
Strategy:
  - Buys INITIAL_SHARES of AAPL on first run
  - Sets a stop-loss floor at -10% from entry price
  - If price rises 10%+ from entry, trails the floor up to 5% below current price
  - Ladder buys on dips: buys more shares at -20% and -30% drops from entry
  - Sells everything if price hits the floor
  - Runs every 5 minutes during market hours via Windows Task Scheduler

State is persisted in state.json so each 5-min run picks up where the last left off.
"""

import json
import logging
import os
import sys
from datetime import datetime, time as dtime

import pytz

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

import sys
sys.path.insert(0, r"C:\Users\madlabz\Documents\Trading")
from shared import notify
import config

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "state.json")
LOG_FILE   = os.path.join(BASE_DIR, "bot.log")

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ── Market Hours ───────────────────────────────────────────────────────────────
def is_market_open() -> bool:
    """Returns True if current ET time is within regular market hours Mon–Fri."""
    et  = pytz.timezone("America/New_York")
    now = datetime.now(et)
    if now.weekday() >= 5:          # Saturday=5, Sunday=6
        return False
    t = now.time()
    return dtime(9, 34) <= t <= dtime(16, 0)

# ── State Persistence ──────────────────────────────────────────────────────────
def _default_state() -> dict:
    return {
        "status":        "watching",   # watching | holding | exited
        "entry_price":   None,
        "floor":         None,
        "highest_price": None,
        "shares_held":   0,
        "ladder": [
            {"drop_pct": lvl["drop_pct"], "shares": lvl["shares"], "triggered": False}
            for lvl in config.LADDER_LEVELS
        ],
    }

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return _default_state()

def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ── Price Feed ─────────────────────────────────────────────────────────────────
def get_price(data_client: StockHistoricalDataClient) -> float:
    """Gets the latest trade price for the configured symbol."""
    req   = StockLatestTradeRequest(symbol_or_symbols=config.SYMBOL)
    trade = data_client.get_stock_latest_trade(req)
    price = float(trade[config.SYMBOL].price)
    return price

# ── Order Placement ────────────────────────────────────────────────────────────
def place_order(trade_client: TradingClient, side: OrderSide, qty: int) -> None:
    """Places a market order and logs the result."""
    req   = MarketOrderRequest(
        symbol=config.SYMBOL,
        qty=qty,
        side=side,
        time_in_force=TimeInForce.DAY,
    )
    order = trade_client.submit_order(req)
    action = "BUY" if side == OrderSide.BUY else "SELL"
    log.info(f"ORDER → {action} {qty} x {config.SYMBOL} | ID: {order.id}")

# ── Account Check ──────────────────────────────────────────────────────────────
def get_buying_power(trade_client: TradingClient) -> float:
    account = trade_client.get_account()
    return float(account.buying_power)

# ── Core Logic ─────────────────────────────────────────────────────────────────
def run() -> None:
    if not is_market_open():
        log.info("Market closed — nothing to do.")
        return

    trade_client = TradingClient(config.API_KEY, config.API_SECRET, paper=True)
    data_client  = StockHistoricalDataClient(config.API_KEY, config.API_SECRET)

    state = load_state()
    price = get_price(data_client)
    log.info(f"{config.SYMBOL} @ ${price:.2f}")

    # ── WATCHING: no open position, buy initial shares ─────────────────────────
    if state["status"] == "watching":
        bp = get_buying_power(trade_client)
        cost = price * config.INITIAL_SHARES
        if bp < cost:
            log.warning(f"Insufficient buying power (${bp:.2f}) for {config.INITIAL_SHARES} shares (${cost:.2f}). Skipping.")
            return

        log.info(f"Opening position: buying {config.INITIAL_SHARES} shares of {config.SYMBOL} at ~${price:.2f}")
        place_order(trade_client, OrderSide.BUY, config.INITIAL_SHARES)

        floor = round(price * (1 - config.STOP_LOSS_PCT), 2)
        state.update({
            "status":        "holding",
            "entry_price":   round(price, 2),
            "floor":         floor,
            "highest_price": round(price, 2),
            "shares_held":   config.INITIAL_SHARES,
        })
        log.info(f"Entry: ${price:.2f} | Stop-loss floor: ${floor:.2f}")
        notify.b1_opened(config.SYMBOL, config.INITIAL_SHARES, price, floor)
        save_state(state)
        return

    # ── HOLDING: manage open position ──────────────────────────────────────────
    if state["status"] == "holding":
        entry   = state["entry_price"]
        floor   = state["floor"]
        highest = state["highest_price"]
        shares  = state["shares_held"]

        # Track new high
        if price > highest:
            state["highest_price"] = round(price, 2)
            highest = price

        # 1. Floor hit → sell everything
        if price <= floor:
            log.info(
                f"FLOOR HIT — Price ${price:.2f} <= Floor ${floor:.2f}. "
                f"Selling {shares} shares."
            )
            place_order(trade_client, OrderSide.SELL, shares)
            notify.b1_sold(config.SYMBOL, shares, price, entry)
            state.update({"status": "exited", "shares_held": 0})
            save_state(state)
            log.info("Position closed. To restart the bot, delete state.json.")
            return

        # 2. Trail the floor upward
        if price >= entry * (1 + config.TRAIL_TRIGGER_PCT):
            new_floor = round(price * (1 - config.TRAIL_FLOOR_PCT), 2)
            if new_floor > floor:
                log.info(f"TRAILING UP — Floor raised: ${floor:.2f} → ${new_floor:.2f}")
                notify.b1_floor_raised(config.SYMBOL, floor, new_floor, price)
                state["floor"] = new_floor
                floor = new_floor

        # 3. Ladder buys on dips
        for level in state["ladder"]:
            if not level["triggered"]:
                target = round(entry * (1 - level["drop_pct"]), 2)
                if price <= target:
                    qty = level["shares"]
                    bp  = get_buying_power(trade_client)
                    cost = price * qty
                    if bp >= cost:
                        log.info(
                            f"LADDER BUY — Price dropped -{level['drop_pct']*100:.0f}% "
                            f"to ${price:.2f}. Buying {qty} more shares."
                        )
                        place_order(trade_client, OrderSide.BUY, qty)
                        notify.b1_ladder_buy(config.SYMBOL, qty, price, level["drop_pct"] * 100)
                        state["shares_held"] += qty
                        level["triggered"] = True
                    else:
                        log.warning(
                            f"Ladder buy at -{level['drop_pct']*100:.0f}% triggered "
                            f"but insufficient buying power (${bp:.2f} < ${cost:.2f})."
                        )

        save_state(state)
        pnl_pct = ((price - entry) / entry) * 100
        log.info(
            f"STATUS | Price: ${price:.2f} | Entry: ${entry:.2f} | "
            f"Floor: ${floor:.2f} | Shares: {state['shares_held']} | "
            f"P&L: {pnl_pct:+.2f}%"
        )
        return

    # ── EXITED: position was closed, waiting for manual reset ─────────────────
    if state["status"] == "exited":
        log.info("Bot has exited its position. Delete state.json to start a new trade.")


# ── Entry Point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    run()
