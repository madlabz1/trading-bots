"""
Bot 2: Copy Trading Bot
========================
Data source: Quiver Quantitative (free, no API key needed)
  → Returns the 1,000 most recent congressional stock disclosures

Politician scoring (picks the best one automatically):
  - Only counts stock purchases (not options, not sales)
  - Score = (number of purchases) + (avg ExcessReturn vs S&P * 10)
  - Higher score = more active buyer who outperforms the market

Trade execution:
  - BUY  → when politician purchases a stock we don't yet hold
  - SELL → when politician sells a stock we hold (mirrors their exit)
  - Only trades stocks (skips options / OP ticker type)
  - Allocates $500 per trade, max 10 open positions
  - Tracks every copied trade in state.json to prevent duplicates

Schedule: runs once daily at 9:34 AM ET via Windows Task Scheduler
"""

import json
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, time as dtime

import pytz
import requests

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
    et  = pytz.timezone("America/New_York")
    now = datetime.now(et)
    if now.weekday() >= 5:
        return False
    t = now.time()
    return dtime(9, 34) <= t <= dtime(16, 0)

# ── State ──────────────────────────────────────────────────────────────────────
def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {
        "selected_politician": None,
        "copied_trade_ids":    [],
        "last_run":            None,
    }

def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ── Data Fetching ──────────────────────────────────────────────────────────────
def fetch_congress_trades() -> list[dict]:
    """
    Fetches the 1,000 most recent congressional trade disclosures from Quiver Quant.
    The endpoint is CDN-cached every 5 minutes — running once daily always hits a fresh cache.
    A single retry with a 10-second delay handles the rare case where the cache just expired.
    """
    import time

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
    }

    for attempt in range(1, 3):   # max 2 attempts
        try:
            r = requests.get(config.QUIVER_URL, headers=headers, timeout=15)
            if r.status_code == 401 and attempt == 1:
                log.warning("Quiver API returned 401 — waiting 10s for CDN cache refresh...")
                time.sleep(10)
                continue
            r.raise_for_status()
            data = r.json()
            log.info(f"Fetched {len(data)} congressional trade records.")
            return data
        except Exception as e:
            log.error(f"Attempt {attempt}: Failed to fetch trade data: {e}")
            if attempt < 2:
                time.sleep(10)

    return []

# ── Politician Scoring ─────────────────────────────────────────────────────────
def select_politician(trades: list[dict]) -> str:
    """
    Scores each politician by:
      purchase_count  (activity)
    + avg_excess_return * 10  (outperformance vs S&P, scaled up)

    Only stock purchases are counted. Politicians with negative excess
    returns can still rank if they trade very frequently, giving a
    balanced view of both volume and quality.
    """
    if config.FORCE_POLITICIAN:
        log.info(f"Forced politician override: {config.FORCE_POLITICIAN}")
        return config.FORCE_POLITICIAN

    purchase_counts:  dict[str, int]        = defaultdict(int)
    excess_returns:   dict[str, list[float]] = defaultdict(list)

    for t in trades:
        if t.get("TickerType") != "Stock":
            continue
        if "Purchase" not in (t.get("Transaction") or ""):
            continue
        ticker = (t.get("Ticker") or "").strip().upper()
        if not ticker or not ticker.isalpha() or len(ticker) > 5:
            continue

        name = t.get("Representative", "Unknown").strip()
        purchase_counts[name] += 1

        er = t.get("ExcessReturn")
        if er is not None:
            try:
                excess_returns[name].append(float(er))
            except (TypeError, ValueError):
                pass

    scores: dict[str, float] = {}
    for name, count in purchase_counts.items():
        avg_er = (
            sum(excess_returns[name]) / len(excess_returns[name])
            if excess_returns[name] else 0.0
        )
        scores[name] = count + (avg_er * 10)

    if not scores:
        log.warning("No scoreable politicians found — using Nancy Pelosi as fallback.")
        return "Nancy Pelosi"

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    log.info("Top 5 politicians by score:")
    for i, (name, score) in enumerate(ranked[:5], 1):
        count  = purchase_counts[name]
        avg_er = (
            sum(excess_returns[name]) / len(excess_returns[name])
            if excess_returns.get(name) else 0.0
        )
        log.info(f"  {i}. {name:40s} score={score:7.2f}  buys={count:3d}  avg_excess={avg_er:+.2f}%")

    winner = ranked[0][0]
    log.info(f"Selected: {winner}")
    return winner

# ── Trade Helpers ──────────────────────────────────────────────────────────────
def trade_id(t: dict) -> str:
    """Unique ID per disclosure record — prevents duplicate orders."""
    name   = (t.get("Representative") or "").strip()
    ticker = (t.get("Ticker") or "").strip().upper()
    date   = (t.get("TransactionDate") or t.get("ReportDate") or "")[:10]
    txn    = (t.get("Transaction") or "").strip()
    return f"{name}|{ticker}|{date}|{txn}"

def is_buy(t: dict) -> bool:
    return "Purchase" in (t.get("Transaction") or "")

def is_sell(t: dict) -> bool:
    txn = t.get("Transaction") or ""
    return "Sale" in txn or "sale" in txn

# ── Alpaca Helpers ─────────────────────────────────────────────────────────────
def is_tradable(trade_client: TradingClient, ticker: str) -> bool:
    try:
        asset = trade_client.get_asset(ticker)
        return bool(asset.tradable)
    except Exception:
        return False

def get_price(data_client: StockHistoricalDataClient, ticker: str) -> float | None:
    try:
        req   = StockLatestTradeRequest(symbol_or_symbols=ticker)
        trade = data_client.get_stock_latest_trade(req)
        return float(trade[ticker].price)
    except Exception:
        return None

def get_buying_power(trade_client: TradingClient) -> float:
    return float(trade_client.get_account().buying_power)

def count_open_positions(trade_client: TradingClient) -> int:
    return len(trade_client.get_all_positions())

def shares_held(trade_client: TradingClient, ticker: str) -> int:
    try:
        pos = trade_client.get_open_position(ticker)
        return int(float(pos.qty))
    except Exception:
        return 0

def place_order(trade_client: TradingClient, side: OrderSide, ticker: str, qty: int) -> None:
    req   = MarketOrderRequest(
        symbol=ticker,
        qty=qty,
        side=side,
        time_in_force=TimeInForce.DAY,
    )
    order = trade_client.submit_order(req)
    log.info(f"ORDER → {'BUY' if side == OrderSide.BUY else 'SELL'} {qty} x {ticker} | ID: {order.id}")

# ── Core Logic ─────────────────────────────────────────────────────────────────
def run() -> None:
    if not is_market_open():
        log.info("Market closed — nothing to do.")
        return

    trade_client = TradingClient(config.API_KEY, config.API_SECRET, paper=True)
    data_client  = StockHistoricalDataClient(config.API_KEY, config.API_SECRET)
    state        = load_state()

    # ── 1. Fetch & score ───────────────────────────────────────────────────────
    all_trades = fetch_congress_trades()
    if not all_trades:
        log.error("No trade data — aborting.")
        return

    politician = select_politician(all_trades)
    if state["selected_politician"] != politician:
        log.info(f"Politician updated: {state['selected_politician']} → {politician}")
        notify.b2_politician_selected(politician, 0)
    state["selected_politician"] = politician

    # ── 2. Filter to this politician's stock trades only ──────────────────────
    pol_trades = [
        t for t in all_trades
        if (t.get("Representative") or "").strip() == politician
        and t.get("TickerType") == "Stock"
    ]
    log.info(f"{len(pol_trades)} stock trades found for {politician} in dataset.")

    # ── 3. Execute new trades ──────────────────────────────────────────────────
    bp             = get_buying_power(trade_client)
    open_pos       = count_open_positions(trade_client)
    orders_placed  = 0

    for t in pol_trades:
        tid    = trade_id(t)
        ticker = (t.get("Ticker") or "").strip().upper()

        # Skip already processed
        if tid in state["copied_trade_ids"]:
            continue

        # Validate ticker
        if not ticker or len(ticker) > 5 or not ticker.isalpha():
            state["copied_trade_ids"].append(tid)
            continue

        if not is_tradable(trade_client, ticker):
            log.info(f"  {ticker}: not tradable on Alpaca — skipping.")
            state["copied_trade_ids"].append(tid)
            continue

        # ── BUY ───────────────────────────────────────────────────────────────
        if is_buy(t):
            if open_pos >= config.MAX_POSITIONS:
                log.info(f"  Max positions reached ({config.MAX_POSITIONS}). Skipping {ticker}.")
                continue
            if bp < config.TRADE_SIZE_USD:
                log.warning(f"  Low buying power (${bp:.2f}). Skipping {ticker}.")
                continue

            price = get_price(data_client, ticker)
            if not price:
                log.warning(f"  Could not get price for {ticker} — skipping.")
                continue

            qty  = max(1, int(config.TRADE_SIZE_USD / price))
            cost = qty * price
            if cost > bp:
                log.warning(f"  ${cost:.2f} exceeds buying power ${bp:.2f} — skipping {ticker}.")
                continue

            txn_date = t.get("TransactionDate", "unknown date")
            log.info(
                f"  Copying BUY: {politician} purchased {ticker} on {txn_date}. "
                f"Buying {qty} share(s) @ ~${price:.2f}."
            )
            place_order(trade_client, OrderSide.BUY, ticker, qty)
            notify.b2_trade_copied(politician, "buy", ticker, qty, price)
            state["copied_trade_ids"].append(tid)
            bp       -= cost
            open_pos += 1
            orders_placed += 1

        # ── SELL ──────────────────────────────────────────────────────────────
        elif is_sell(t):
            qty = shares_held(trade_client, ticker)
            if qty <= 0:
                log.info(f"  Sell signal for {ticker} but we hold no shares — skipping.")
                state["copied_trade_ids"].append(tid)
                continue

            txn_date = t.get("TransactionDate", "unknown date")
            log.info(
                f"  Copying SELL: {politician} sold {ticker} on {txn_date}. "
                f"Selling our {qty} share(s)."
            )
            place_order(trade_client, OrderSide.SELL, ticker, qty)
            notify.b2_trade_copied(politician, "sell", ticker, qty, get_price(data_client, ticker) or 0)
            state["copied_trade_ids"].append(tid)
            open_pos -= 1
            orders_placed += 1

    # ── 4. Summary ─────────────────────────────────────────────────────────────
    state["last_run"] = datetime.utcnow().isoformat()
    save_state(state)
    log.info(
        f"Run complete | Copying: {politician} | Orders placed: {orders_placed} | "
        f"Open positions: {open_pos} | Buying power: ${get_buying_power(trade_client):.2f}"
    )

# ── Entry Point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    run()
