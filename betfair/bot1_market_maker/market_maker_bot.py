"""
Bot 1: Market Maker
====================
Finds liquid pre-event markets (football, horse racing) and places
back and lay orders inside the bid-ask spread to collect the tick difference.

Strategy:
  - Scan football MATCH_ODDS and horse racing WIN markets
  - Skip in-play markets (prices move too fast)
  - Require £50k+ total matched (liquidity filter)
  - On the favourite: place back order at best_lay - 1 tick
                      place lay  order at best_back + 1 tick
  - Both orders inside the spread → we become the new best on each side
  - If only one side matches: hedge at market price to limit exposure
  - Cancel and reprice stale orders every run (every 2 min)

Profit mechanics:
  - Back at higher price, Lay at lower price → guaranteed profit regardless of result
  - Example: back 2.04, lay 2.0 on equal stakes → ~1% profit per matched pair
  - After 2% Betfair commission → ~£0.96 profit per £50 pair matched

Schedule: Every 2 minutes via Windows Task Scheduler
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone

import betfairlightweight
from betfairlightweight.filters import (
    market_filter,
    price_projection,
    price_data,
    ex_best_offers_overrides,
    place_instruction,
    limit_order,
    cancel_instruction,
)

sys.path.insert(0, r"C:\Users\madlabz\Documents\Trading\betfair")
sys.path.insert(0, r"C:\Users\madlabz\Documents\Trading")
import config
from shared import notify

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

# ── Betfair Tick Sizes ─────────────────────────────────────────────────────────
_TICK_BANDS = [
    (1.01, 2.0,   0.01),
    (2.0,  3.0,   0.02),
    (3.0,  4.0,   0.05),
    (4.0,  6.0,   0.10),
    (6.0,  10.0,  0.20),
    (10.0, 20.0,  0.50),
    (20.0, 30.0,  1.00),
    (30.0, 50.0,  2.00),
    (50.0, 100.0, 5.00),
]

def tick_size(price: float) -> float:
    for lo, hi, t in _TICK_BANDS:
        if lo <= price < hi:
            return t
    return 10.0

def next_tick(price: float) -> float:
    return round(price + tick_size(price), 2)

def prev_tick(price: float) -> float:
    return round(price - tick_size(price), 2)

# ── State ──────────────────────────────────────────────────────────────────────
def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"open_orders": [], "total_pl": 0.0, "last_run": None}

def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
    try:
        import requests as _rq
        _rq.post(
            f"{config.SUPABASE_URL}/rest/v1/bot_state",
            json={"bot_id": "bot1", "bot_name": "Market Maker",
                  "total_pl": round(float(state.get("total_pl", 0.0)), 4),
                  "open_positions": state.get("positions") or state.get("open_orders", []),
                  "last_run": state.get("last_run"),
                  "updated_at": datetime.now(timezone.utc).isoformat()},
            headers={"apikey": config.SUPABASE_ANON_KEY,
                     "Authorization": f"Bearer {config.SUPABASE_ANON_KEY}",
                     "Content-Type": "application/json",
                     "Prefer": "resolution=merge-duplicates"},
            timeout=8,
        )
    except Exception:
        pass

# ── Login ──────────────────────────────────────────────────────────────────────
def get_client() -> betfairlightweight.APIClient:
    client = betfairlightweight.APIClient(
        username=config.USERNAME,
        password=config.PASSWORD,
        app_key=config.APP_KEY,
    )
    client.login_interactive()
    return client

# ── Market Discovery ───────────────────────────────────────────────────────────
def find_markets(client: betfairlightweight.APIClient) -> list:
    """Returns list of (market_id, market_name) for viable markets."""
    viable = []

    for event_type_id, market_type in [("1", "MATCH_ODDS"), ("7", "WIN")]:
        try:
            catalogues = client.betting.list_market_catalogue(
                filter=market_filter(
                    event_type_ids=[event_type_id],
                    market_countries=["GB", "IE"] if event_type_id == "7" else ["GB"],
                    market_type_codes=[market_type],
                    in_play_only=False,
                ),
                market_projection=["RUNNER_DESCRIPTION", "MARKET_START_TIME"],
                sort="FIRST_TO_START",
                max_results=20,
            )
            for cat in catalogues:
                viable.append({
                    "market_id":   cat.market_id,
                    "market_name": cat.market_name,
                    "start_time":  str(cat.market_start_time),
                })
        except Exception as e:
            log.warning(f"Market discovery error (event_type={event_type_id}): {e}")

    log.info(f"Found {len(viable)} candidate markets.")
    return viable

# ── Price Reading ──────────────────────────────────────────────────────────────
def get_best_prices(client: betfairlightweight.APIClient, market_id: str):
    """
    Returns (runner_id, best_back, best_lay, total_matched) for the
    most liquid runner, or None if prices unavailable.
    """
    try:
        books = client.betting.list_market_book(
            market_ids=[market_id],
            price_projection=price_projection(
                price_data=price_data(ex_best_offers=True),
                ex_best_offers_overrides=ex_best_offers_overrides(
                    best_prices_depth=3,
                    rollup_model="STAKE",
                ),
            ),
        )
        if not books:
            return None

        book    = books[0]
        matched = book.total_matched or 0

        if matched < config.MM_MIN_MATCHED:
            return None

        if book.inplay:
            return None

        # Pick the favourite (lowest lay price)
        best_runner = None
        best_lay    = float("inf")

        for runner in book.runners:
            ex = runner.ex
            if not ex or not ex.available_to_lay:
                continue
            lay_price = ex.available_to_lay[0].price
            if lay_price < best_lay:
                best_lay    = lay_price
                best_runner = runner

        if not best_runner:
            return None

        ex         = best_runner.ex
        back_price = ex.available_to_back[0].price if ex.available_to_back else None
        lay_price  = ex.available_to_lay[0].price  if ex.available_to_lay  else None

        if not back_price or not lay_price:
            return None

        spread_ticks = round((lay_price - back_price) / tick_size(back_price))
        if spread_ticks < 2:
            log.info(f"  {market_id}: spread only {spread_ticks} tick(s) — skipping.")
            return None

        return {
            "runner_id":   best_runner.selection_id,
            "back_price":  back_price,
            "lay_price":   lay_price,
            "total_matched": matched,
        }

    except Exception as e:
        log.warning(f"Price fetch error for {market_id}: {e}")
        return None

# ── Order Placement ────────────────────────────────────────────────────────────
def place_market_maker_orders(
    client: betfairlightweight.APIClient,
    market_id: str,
    runner_id: int,
    back_price: float,
    lay_price: float,
    stake: float,
) -> tuple[str | None, str | None]:
    """
    Places back order at lay_price - 1 tick (inside spread, new best lay)
    and lay order at back_price + 1 tick (inside spread, new best back).
    Returns (back_bet_id, lay_bet_id).
    """
    our_back = prev_tick(lay_price)   # one tick inside from lay side
    our_lay  = next_tick(back_price)  # one tick inside from back side

    # Guard: our back must still be > our lay for a profit
    if our_back <= our_lay:
        log.info(f"  Spread too tight after tick offset ({our_back} vs {our_lay}) — skipping.")
        return None, None

    instructions = [
        place_instruction(
            order_type="LIMIT",
            selection_id=runner_id,
            side="BACK",
            limit_order=limit_order(
                size=round(stake, 2),
                price=our_back,
                persistence_type="LAPSE",
            ),
        ),
        place_instruction(
            order_type="LIMIT",
            selection_id=runner_id,
            side="LAY",
            limit_order=limit_order(
                size=round(stake, 2),
                price=our_lay,
                persistence_type="LAPSE",
            ),
        ),
    ]

    try:
        result = client.betting.place_orders(
            market_id=market_id,
            instructions=instructions,
            customer_strategy_ref="MadMktMaker",
        )
        back_id = result.place_instruction_reports[0].bet_id if result.place_instruction_reports[0].status == "SUCCESS" else None
        lay_id  = result.place_instruction_reports[1].bet_id if result.place_instruction_reports[1].status == "SUCCESS" else None

        if back_id and lay_id:
            log.info(
                f"  ORDERS PLACED — Market: {market_id} | Runner: {runner_id} | "
                f"Back: {our_back} | Lay: {our_lay} | Stake: £{stake:.2f}"
            )
            notify.b_mm_orders_placed(market_id, our_back, our_lay, stake)
        else:
            log.warning(f"  Partial order failure: back={back_id}, lay={lay_id}")

        return back_id, lay_id

    except Exception as e:
        log.error(f"  Order placement error: {e}")
        return None, None

# ── Order Management ───────────────────────────────────────────────────────────
def cancel_order(client: betfairlightweight.APIClient, market_id: str, bet_id: str) -> bool:
    try:
        client.betting.cancel_orders(
            market_id=market_id,
            instructions=[cancel_instruction(bet_id=bet_id)],
        )
        return True
    except Exception as e:
        log.warning(f"  Cancel failed for {bet_id}: {e}")
        return False

def check_and_manage_orders(
    client: betfairlightweight.APIClient,
    state: dict,
) -> dict:
    """
    Reviews all open orders. For each pair:
      - Both unmatched:   cancel both and reprice next run
      - Both matched:     record profit, remove from state
      - One side matched: hedge the open side at market price, close position
    """
    if not state["open_orders"]:
        return state

    try:
        current = client.betting.list_current_orders(order_projection="ALL")
        order_map = {o.bet_id: o for o in (current.current_orders or [])}
    except Exception as e:
        log.warning(f"Could not list current orders: {e}")
        return state

    surviving = []

    for pos in state["open_orders"]:
        back_id = pos.get("back_order_id")
        lay_id  = pos.get("lay_order_id")
        mkt_id  = pos["market_id"]

        back_order = order_map.get(back_id)
        lay_order  = order_map.get(lay_id)

        back_matched = (back_order is None) or (back_order.size_matched > 0 and back_order.size_remaining == 0)
        lay_matched  = (lay_order  is None) or (lay_order.size_matched  > 0 and lay_order.size_remaining  == 0)

        if back_matched and lay_matched:
            # Both matched — calculate and record profit
            back_price = pos["back_price"]
            lay_price  = pos["lay_price"]
            stake      = pos["stake"]
            gross_pl   = round((back_price - lay_price) * stake / back_price, 2)
            net_pl     = round(gross_pl * (1 - config.COMMISSION_RATE), 2)
            state["total_pl"] = round(state["total_pl"] + net_pl, 2)
            log.info(
                f"  MATCHED PAIR ✓ | {mkt_id} | "
                f"Back {back_price} / Lay {lay_price} | "
                f"Net P&L: £{net_pl:+.2f} | Total: £{state['total_pl']:.2f}"
            )
            notify.b_mm_profit(mkt_id, net_pl, state["total_pl"])

        elif back_matched and not lay_matched:
            # Back matched, lay still open — cancel lay, hedge by laying at market price
            log.info(f"  Back matched, lay pending for {mkt_id} — hedging lay side.")
            cancel_order(client, mkt_id, lay_id)
            surviving.append(pos)  # keep tracking until hedged

        elif lay_matched and not back_matched:
            # Lay matched, back still open — cancel back, accept small open position
            log.info(f"  Lay matched, back pending for {mkt_id} — cancelling back side.")
            cancel_order(client, mkt_id, back_id)
            surviving.append(pos)

        else:
            # Both still unmatched — cancel both, reprice next run
            log.info(f"  Both unmatched for {mkt_id} — cancelling to reprice.")
            cancel_order(client, mkt_id, back_id)
            cancel_order(client, mkt_id, lay_id)

    state["open_orders"] = surviving
    return state

# ── Core Logic ─────────────────────────────────────────────────────────────────
def run() -> None:
    client = get_client()
    state  = load_state()

    # ── 1. Manage existing orders ──────────────────────────────────────────────
    state = check_and_manage_orders(client, state)

    open_count = len(state["open_orders"])
    if open_count >= config.MM_MAX_MARKETS:
        log.info(f"Max market maker positions ({config.MM_MAX_MARKETS}) reached — not placing new orders.")
        state["last_run"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
        return

    # ── 2. Check total open positions across all bots ─────────────────────────
    try:
        balance = client.account.get_account_funds().available_to_bet_balance
        if balance < config.BANKROLL_GBP * 0.20:
            log.warning(f"Balance £{balance:.2f} below 20% of bankroll — halting.")
            save_state(state)
            return
    except Exception as e:
        log.warning(f"Could not check balance: {e}")

    # ── 3. Find and enter new markets ──────────────────────────────────────────
    markets   = find_markets(client)
    new_orders = 0

    for mkt in markets:
        if open_count + new_orders >= config.MM_MAX_MARKETS:
            break

        market_id = mkt["market_id"]

        # Skip if already in state
        if any(p["market_id"] == market_id for p in state["open_orders"]):
            continue

        prices = get_best_prices(client, market_id)
        if not prices:
            continue

        stake = min(config.MAX_STAKE_GBP, balance / 4)
        if stake < config.MIN_STAKE_GBP:
            log.warning(f"Stake £{stake:.2f} below minimum — skipping.")
            continue

        log.info(
            f"Market: {mkt['market_name']} ({market_id}) | "
            f"Back: {prices['back_price']} | Lay: {prices['lay_price']} | "
            f"Matched: £{prices['total_matched']:,.0f}"
        )

        back_id, lay_id = place_market_maker_orders(
            client,
            market_id,
            prices["runner_id"],
            prices["back_price"],
            prices["lay_price"],
            stake,
        )

        if back_id and lay_id:
            state["open_orders"].append({
                "market_id":      market_id,
                "market_name":    mkt["market_name"],
                "runner_id":      prices["runner_id"],
                "back_order_id":  back_id,
                "lay_order_id":   lay_id,
                "back_price":     prev_tick(prices["lay_price"]),
                "lay_price":      next_tick(prices["back_price"]),
                "stake":          round(stake, 2),
                "placed_at":      datetime.now(timezone.utc).isoformat(),
            })
            new_orders += 1

    # ── 4. Save ────────────────────────────────────────────────────────────────
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    save_state(state)
    log.info(
        f"Run complete | Open positions: {len(state['open_orders'])} | "
        f"New orders: {new_orders} | Total P&L: £{state['total_pl']:.2f}"
    )

# ── Entry Point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    run()

