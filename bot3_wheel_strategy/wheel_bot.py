"""
Bot 3: Wheel Strategy Bot
==========================
Stock: NVDA  |  Account: Alpaca Paper Trading

State Machine
─────────────
  IDLE
    └─ No position. Finds best put contract (10% OTM, 14-28 DTE) and sells it.
       → moves to PUT_OPEN

  PUT_OPEN
    └─ Short put is live. Monitors each run:
       • Profit >= 50% of premium → buy back (close early), return to IDLE
       • Put position gone + we own shares → ASSIGNED, move to SHARES_HELD
       • Put position gone + no shares    → EXPIRED WORTHLESS, return to IDLE

  SHARES_HELD
    └─ Own 100 shares. Finds best call contract (10% above cost basis) and sells it.
       → moves to CALL_OPEN

  CALL_OPEN
    └─ Own shares + short call. Monitors each run:
       • Profit >= 50% of premium → buy back (close early), return to SHARES_HELD
       • Call position gone + no shares → CALLED AWAY (profit!), return to IDLE
       • Call position gone + still own shares → EXPIRED WORTHLESS, return to SHARES_HELD

Rules
──────
  • Never sell a put without enough buying power to cover full assignment
  • Never sell a call below cost basis (locks in a guaranteed profit)
  • Track cumulative premiums across all cycles
  • Daily summary logged at 3:50 PM ET (before close)
  • Runs every 15 minutes during market hours via Windows Task Scheduler
"""

import json
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta, time as dtime

import pytz

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import ContractType, OrderSide, TimeInForce
from alpaca.trading.requests import GetOptionContractsRequest, LimitOrderRequest

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

# ── Stages ─────────────────────────────────────────────────────────────────────
IDLE         = "IDLE"
PUT_OPEN     = "PUT_OPEN"
SHARES_HELD  = "SHARES_HELD"
CALL_OPEN    = "CALL_OPEN"

# ── Market Time Helpers ────────────────────────────────────────────────────────
def now_et() -> datetime:
    return datetime.now(pytz.timezone("America/New_York"))

def is_market_open() -> bool:
    n = now_et()
    if n.weekday() >= 5:
        return False
    t = n.time()
    return dtime(9, 34) <= t <= dtime(16, 0)

def is_summary_time() -> bool:
    t = now_et().time()
    return dtime(config.SUMMARY_HOUR, config.SUMMARY_MINUTE) <= t <= dtime(15, 59)

# ── State ──────────────────────────────────────────────────────────────────────
def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {
        "stage":             IDLE,
        "contract_symbol":   None,   # active option contract OSI symbol
        "premium_received":  0.0,    # premium from current open contract ($)
        "cost_basis":        None,   # effective cost per share after premiums
        "total_premiums":    0.0,    # cumulative premiums across all cycles
        "cycles_completed":  0,
        "last_summary_date": None,
    }

def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ── Price Feed ─────────────────────────────────────────────────────────────────
def get_stock_price(data_client: StockHistoricalDataClient) -> float:
    req   = StockLatestTradeRequest(symbol_or_symbols=config.SYMBOL)
    trade = data_client.get_stock_latest_trade(req)
    return float(trade[config.SYMBOL].price)

# ── Position Helpers ───────────────────────────────────────────────────────────
def get_position(trade_client: TradingClient, symbol: str):
    """Returns position object or None if not held."""
    try:
        return trade_client.get_open_position(symbol)
    except Exception:
        return None

def get_stock_position(trade_client: TradingClient):
    return get_position(trade_client, config.SYMBOL)

def get_option_position(trade_client: TradingClient, contract_symbol: str | None):
    if not contract_symbol:
        return None
    return get_position(trade_client, contract_symbol)

def get_buying_power(trade_client: TradingClient) -> float:
    return float(trade_client.get_account().buying_power)

# ── Contract Selection ─────────────────────────────────────────────────────────
def find_best_put(trade_client: TradingClient, stock_price: float):
    """
    Finds the put contract closest to 10% OTM with 14-28 DTE.
    Prefers contracts with higher open interest (more liquid).
    Returns None if no suitable contract is found.
    """
    target_strike = stock_price * (1 - config.PUT_OTM_PCT)
    min_exp = date.today() + timedelta(days=config.MIN_DTE)
    max_exp = date.today() + timedelta(days=config.MAX_DTE)

    try:
        req = GetOptionContractsRequest(
            underlying_symbols=[config.SYMBOL],
            expiration_date_gte=str(min_exp),
            expiration_date_lte=str(max_exp),
            type=ContractType.PUT,
            limit=200,
        )
        resp      = trade_client.get_option_contracts(req)
        contracts = resp.option_contracts if hasattr(resp, "option_contracts") else resp
    except Exception as e:
        log.error(f"Failed to fetch put contracts: {e}")
        return None

    # Filter: must be tradable and have a price
    valid = [
        c for c in contracts
        if c.tradable and c.close_price and float(c.close_price) > 0.05
    ]
    if not valid:
        log.warning("No valid put contracts found in the target range.")
        return None

    # Rank: closest to target strike, tie-break by open interest
    valid.sort(key=lambda c: (
        abs(float(c.strike_price) - target_strike),
        -int(c.open_interest or 0),
    ))
    best = valid[0]
    log.info(
        f"Best put: {best.symbol} | Strike: ${float(best.strike_price):.2f} "
        f"(target ${target_strike:.2f}) | Exp: {best.expiration_date} "
        f"| Last: ${float(best.close_price):.2f} | OI: {best.open_interest}"
    )
    return best

def find_best_call(trade_client: TradingClient, cost_basis: float):
    """
    Finds the call contract closest to 10% above cost basis with 14-28 DTE.
    Never goes below cost basis (guaranteed to lock in profit if exercised).
    """
    target_strike = cost_basis * (1 + config.CALL_OTM_PCT)
    min_exp = date.today() + timedelta(days=config.MIN_DTE)
    max_exp = date.today() + timedelta(days=config.MAX_DTE)

    try:
        req = GetOptionContractsRequest(
            underlying_symbols=[config.SYMBOL],
            expiration_date_gte=str(min_exp),
            expiration_date_lte=str(max_exp),
            type=ContractType.CALL,
            limit=200,
        )
        resp      = trade_client.get_option_contracts(req)
        contracts = resp.option_contracts if hasattr(resp, "option_contracts") else resp
    except Exception as e:
        log.error(f"Failed to fetch call contracts: {e}")
        return None

    # Filter: must be tradable, priced, and above cost basis
    valid = [
        c for c in contracts
        if c.tradable
        and c.close_price
        and float(c.close_price) > 0.05
        and float(c.strike_price) >= cost_basis   # NEVER sell below cost basis
    ]
    if not valid:
        log.warning("No valid call contracts found above cost basis.")
        return None

    valid.sort(key=lambda c: (
        abs(float(c.strike_price) - target_strike),
        -int(c.open_interest or 0),
    ))
    best = valid[0]
    log.info(
        f"Best call: {best.symbol} | Strike: ${float(best.strike_price):.2f} "
        f"(target ${target_strike:.2f}) | Exp: {best.expiration_date} "
        f"| Last: ${float(best.close_price):.2f} | OI: {best.open_interest}"
    )
    return best

# ── Order Helpers ──────────────────────────────────────────────────────────────
def sell_option(trade_client: TradingClient, contract_symbol: str, limit_price: float) -> float:
    """
    Opens a short option position (sell to open) using a limit order.
    Returns the premium received per contract ($).
    """
    req = LimitOrderRequest(
        symbol=contract_symbol,
        qty=1,
        side=OrderSide.SELL,
        time_in_force=TimeInForce.DAY,
        limit_price=round(limit_price, 2),
    )
    order = trade_client.submit_order(req)
    log.info(f"SELL TO OPEN: {contract_symbol} @ ${limit_price:.2f} | ID: {order.id}")

    # Wait briefly for paper fill
    time.sleep(3)
    try:
        filled = trade_client.get_order_by_id(str(order.id))
        fill   = float(filled.filled_avg_price or limit_price)
    except Exception:
        fill = limit_price

    premium = fill * 100   # 1 contract = 100 shares
    log.info(f"Filled @ ${fill:.2f} | Premium received: ${premium:.2f}")
    return premium

def close_option(trade_client: TradingClient, contract_symbol: str, limit_price: float) -> None:
    """Closes a short option position (buy to close)."""
    req = LimitOrderRequest(
        symbol=contract_symbol,
        qty=1,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
        limit_price=round(limit_price, 2),
    )
    order = trade_client.submit_order(req)
    log.info(f"BUY TO CLOSE: {contract_symbol} @ ${limit_price:.2f} | ID: {order.id}")

# ── Stage Handlers ─────────────────────────────────────────────────────────────

def handle_idle(trade_client: TradingClient, data_client: StockHistoricalDataClient,
                state: dict, stock_price: float) -> None:
    """Stage IDLE: sell a cash-secured put."""
    log.info(f"[IDLE] {config.SYMBOL} @ ${stock_price:.2f} — looking for put to sell...")

    contract = find_best_put(trade_client, stock_price)
    if not contract:
        log.warning("[IDLE] No suitable put contract available. Will retry next run.")
        return

    strike       = float(contract.strike_price)
    cash_needed  = strike * 100
    buying_power = get_buying_power(trade_client)

    if buying_power < cash_needed:
        log.warning(
            f"[IDLE] Insufficient buying power (${buying_power:,.2f}) "
            f"to cover put assignment (${cash_needed:,.2f}). Skipping."
        )
        return

    premium = sell_option(trade_client, contract.symbol, float(contract.close_price))

    state["stage"]           = PUT_OPEN
    state["contract_symbol"] = contract.symbol
    state["premium_received"] = premium
    notify.b3_put_sold(contract.symbol, strike, str(contract.expiration_date), premium)
    log.info(
        f"[IDLE → PUT_OPEN] Sold put {contract.symbol} | "
        f"Strike: ${strike:.2f} | Premium: ${premium:.2f} | "
        f"Max risk if assigned: ${cash_needed - premium:.2f}"
    )


def handle_put_open(trade_client: TradingClient, state: dict, stock_price: float) -> None:
    """Stage PUT_OPEN: monitor the short put."""
    contract_symbol = state["contract_symbol"]
    opt_pos         = get_option_position(trade_client, contract_symbol)

    # ── Put position is gone ───────────────────────────────────────────────────
    if opt_pos is None:
        stock_pos = get_stock_position(trade_client)
        shares    = int(float(stock_pos.qty)) if stock_pos else 0

        if shares >= 100:
            # Assigned: we bought 100 shares at the strike price
            raw_cost_basis = float(stock_pos.avg_entry_price)
            # Effective cost = purchase price - premium already collected
            effective_cost = raw_cost_basis - (state["premium_received"] / 100)
            state["stage"]           = SHARES_HELD
            state["contract_symbol"] = None
            state["cost_basis"]      = round(effective_cost, 2)
            state["total_premiums"] += state["premium_received"]
            state["premium_received"] = 0.0
            notify.b3_assigned(config.SYMBOL, raw_cost_basis, effective_cost)
            log.info(
                f"[PUT_OPEN] ASSIGNED! Got 100 {config.SYMBOL} shares @ ${raw_cost_basis:.2f} | "
                f"Effective cost basis (after premium): ${effective_cost:.2f}"
            )
        else:
            # Expired worthless — keep the full premium
            state["stage"]           = IDLE
            state["contract_symbol"] = None
            state["total_premiums"] += state["premium_received"]
            state["cycles_completed"] += 1
            notify.b3_put_expired(state["premium_received"], state["total_premiums"], state["cycles_completed"])
            log.info(
                f"[PUT_OPEN] Put expired WORTHLESS. "
                f"Kept ${state['premium_received']:.2f} premium. "
                f"Cycles: {state['cycles_completed']} | "
                f"Total premiums: ${state['total_premiums']:.2f}"
            )
            state["premium_received"] = 0.0
        return

    # ── Put is still open — check 50% profit ──────────────────────────────────
    unrealized_pl  = float(opt_pos.unrealized_pl)
    premium_target = state["premium_received"] * config.EARLY_CLOSE_PCT
    current_price  = abs(float(opt_pos.market_value)) / 100  # per share price of option

    log.info(
        f"[PUT_OPEN] {contract_symbol} | {config.SYMBOL} @ ${stock_price:.2f} | "
        f"Unrealized P&L: ${unrealized_pl:.2f} | Target: ${premium_target:.2f}"
    )

    if unrealized_pl >= premium_target:
        log.info(
            f"[PUT_OPEN] 50% profit reached! Closing early. "
            f"P&L: ${unrealized_pl:.2f}"
        )
        close_option(trade_client, contract_symbol, max(0.01, round(current_price, 2)))
        notify.b3_early_close(contract_symbol, unrealized_pl, state["total_premiums"] + unrealized_pl)
        state["stage"]           = IDLE
        state["contract_symbol"] = None
        state["total_premiums"] += unrealized_pl
        state["cycles_completed"] += 1
        log.info(f"[PUT_OPEN → IDLE] Closed early. Total premiums: ${state['total_premiums']:.2f}")
        state["premium_received"] = 0.0


def handle_shares_held(trade_client: TradingClient, state: dict, stock_price: float) -> None:
    """Stage SHARES_HELD: sell a covered call against our 100 shares."""
    cost_basis = state["cost_basis"]
    log.info(
        f"[SHARES_HELD] Holding 100 {config.SYMBOL} @ cost ${cost_basis:.2f} | "
        f"Current: ${stock_price:.2f} — looking for call to sell..."
    )

    contract = find_best_call(trade_client, cost_basis)
    if not contract:
        log.warning("[SHARES_HELD] No suitable call contract available. Will retry next run.")
        return

    premium = sell_option(trade_client, contract.symbol, float(contract.close_price))

    # Update cost basis (reduce it further by the call premium)
    new_basis = cost_basis - (premium / 100)
    state["stage"]            = CALL_OPEN
    state["contract_symbol"]  = contract.symbol
    state["premium_received"] = premium
    state["cost_basis"]       = round(new_basis, 2)
    notify.b3_call_sold(contract.symbol, float(contract.strike_price), str(contract.expiration_date), premium)
    log.info(
        f"[SHARES_HELD → CALL_OPEN] Sold call {contract.symbol} | "
        f"Strike: ${float(contract.strike_price):.2f} | Premium: ${premium:.2f} | "
        f"New cost basis: ${new_basis:.2f}"
    )


def handle_call_open(trade_client: TradingClient, state: dict, stock_price: float) -> None:
    """Stage CALL_OPEN: monitor the short covered call."""
    contract_symbol = state["contract_symbol"]
    opt_pos         = get_option_position(trade_client, contract_symbol)

    # ── Call position is gone ──────────────────────────────────────────────────
    if opt_pos is None:
        stock_pos = get_stock_position(trade_client)
        shares    = int(float(stock_pos.qty)) if stock_pos else 0

        if shares < 100:
            # Called away: our shares were sold at the strike price
            state["total_premiums"] += state["premium_received"]
            state["cycles_completed"] += 1
            state["stage"]           = IDLE
            state["contract_symbol"] = None
            state["cost_basis"]      = None
            notify.b3_called_away(config.SYMBOL, 0, state["total_premiums"], state["cycles_completed"])
            log.info(
                f"[CALL_OPEN] CALLED AWAY! Shares sold at strike. "
                f"Wheel cycle #{state['cycles_completed']} complete. "
                f"Total premiums collected: ${state['total_premiums']:.2f}"
            )
            state["premium_received"] = 0.0
        else:
            # Call expired worthless — keep premium, still own shares
            state["total_premiums"] += state["premium_received"]
            state["stage"]           = SHARES_HELD
            state["contract_symbol"] = None
            log.info(
                f"[CALL_OPEN] Call expired WORTHLESS. "
                f"Kept ${state['premium_received']:.2f}. Still own shares. "
                f"Total premiums: ${state['total_premiums']:.2f}"
            )
            state["premium_received"] = 0.0
        return

    # ── Call still open — check 50% profit ────────────────────────────────────
    unrealized_pl  = float(opt_pos.unrealized_pl)
    premium_target = state["premium_received"] * config.EARLY_CLOSE_PCT
    current_price  = abs(float(opt_pos.market_value)) / 100

    log.info(
        f"[CALL_OPEN] {contract_symbol} | {config.SYMBOL} @ ${stock_price:.2f} | "
        f"Unrealized P&L: ${unrealized_pl:.2f} | Target: ${premium_target:.2f}"
    )

    if unrealized_pl >= premium_target:
        log.info(
            f"[CALL_OPEN] 50% profit reached! Closing call early. "
            f"P&L: ${unrealized_pl:.2f}"
        )
        close_option(trade_client, contract_symbol, max(0.01, round(current_price, 2)))
        notify.b3_early_close(contract_symbol, unrealized_pl, state["total_premiums"] + unrealized_pl)
        state["total_premiums"] += unrealized_pl
        state["stage"]           = SHARES_HELD
        state["contract_symbol"] = None
        log.info(f"[CALL_OPEN → SHARES_HELD] Will sell another call next run.")
        state["premium_received"] = 0.0


# ── Daily Summary ──────────────────────────────────────────────────────────────
def print_daily_summary(trade_client: TradingClient, state: dict, stock_price: float) -> None:
    today = str(date.today())
    if state.get("last_summary_date") == today:
        return   # already logged today

    positions = trade_client.get_all_positions()
    acct      = trade_client.get_account()

    log.info("=" * 60)
    log.info(f"DAILY SUMMARY — {today}")
    log.info(f"  Stage:             {state['stage']}")
    log.info(f"  {config.SYMBOL} price:       ${stock_price:.2f}")
    log.info(f"  Active contract:   {state['contract_symbol'] or 'None'}")
    log.info(f"  Cost basis:        ${state['cost_basis']:.2f}" if state["cost_basis"] else "  Cost basis:        —")
    log.info(f"  Premium (current): ${state['premium_received']:.2f}")
    log.info(f"  Total premiums:    ${state['total_premiums']:.2f}")
    log.info(f"  Cycles completed:  {state['cycles_completed']}")
    log.info(f"  Buying power:      ${float(acct.buying_power):,.2f}")
    log.info(f"  Portfolio value:   ${float(acct.portfolio_value):,.2f}")
    log.info("=" * 60)

    state["last_summary_date"] = today


# ── Main ───────────────────────────────────────────────────────────────────────
def run() -> None:
    if not is_market_open():
        log.info("Market closed — nothing to do.")
        return

    trade_client = TradingClient(config.API_KEY, config.API_SECRET, paper=True)
    data_client  = StockHistoricalDataClient(config.API_KEY, config.API_SECRET)
    state        = load_state()

    stock_price = get_stock_price(data_client)
    log.info(f"{config.SYMBOL} @ ${stock_price:.2f} | Stage: {state['stage']}")

    if state["stage"] == IDLE:
        handle_idle(trade_client, data_client, state, stock_price)

    elif state["stage"] == PUT_OPEN:
        handle_put_open(trade_client, state, stock_price)

    elif state["stage"] == SHARES_HELD:
        handle_shares_held(trade_client, state, stock_price)

    elif state["stage"] == CALL_OPEN:
        handle_call_open(trade_client, state, stock_price)

    if is_summary_time():
        print_daily_summary(trade_client, state, stock_price)

    save_state(state)


if __name__ == "__main__":
    run()
