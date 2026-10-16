"""
Bot 2: Momentum + Trailing Stop
=================================
Monitors football, horse racing and tennis markets for sharp probability
moves. Enters in the direction of momentum and trails a stop to lock in profit.

Strategy:
  - Every 2 min: read prices for all active markets, store in price history
  - Momentum trigger: if probability moves >10% in 30 minutes → enter
      BACK  if probability is RISING  (odds shortening — selection more likely to win)
      LAY   if probability is FALLING (odds drifting  — selection becoming less likely)
  - Trailing stop: track best probability since entry
      BACK position: close if prob falls back 8% from peak  (lock in gain)
      LAY  position: close if prob rises back 8% from trough (lock in gain)
  - Hard stop: close if prob reverses 15% from entry (limit loss)

Probability = 1 / decimal_odds
Example: odds 2.0 → prob 50%  |  odds 1.67 → prob 60%

Schedule: Every 2 minutes via Windows Task Scheduler
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone, timedelta

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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ── State ──────────────────────────────────────────────────────────────────────
def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"positions": [], "price_history": {}, "total_pl": 0.0, "last_run": None}

def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
    try:
        from shared.supabase_push import push_state as _sb
        _sb(config.SUPABASE_URL, config.SUPABASE_ANON_KEY, "bot2", "Momentum", state)
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

# ── Market Scanner ─────────────────────────────────────────────────────────────
def scan_markets(client: betfairlightweight.APIClient) -> list[dict]:
    """Returns all active pre-event + in-play markets across football, racing, tennis."""
    markets = []
    targets = [
        ("1", "MATCH_ODDS", ["GB", "DE", "ES", "IT", "FR"]),  # Football
        ("7", "WIN",        ["GB", "IE"]),                     # Horse Racing
        ("2", "MATCH_ODDS", None),                             # Tennis
    ]
    for event_id, market_type, countries in targets:
        try:
            filt = market_filter(
                event_type_ids=[event_id],
                market_type_codes=[market_type],
            )
            if countries:
                filt = market_filter(
                    event_type_ids=[event_id],
                    market_countries=countries,
                    market_type_codes=[market_type],
                )
            cats = client.betting.list_market_catalogue(
                filter=filt,
                market_projection=["RUNNER_DESCRIPTION", "MARKET_START_TIME"],
                sort="FIRST_TO_START",
                max_results=30,
            )
            for c in cats:
                markets.append({"market_id": c.market_id, "market_name": c.market_name})
        except Exception as e:
            log.warning(f"Market scan error (event={event_id}): {e}")
    return markets

# ── Price Snapshot ─────────────────────────────────────────────────────────────
def snapshot_prices(client: betfairlightweight.APIClient, market_ids: list[str]) -> dict:
    """
    Returns {market_id_runnerid: prob} for the favourite in each market.
    Fetches in batches of 10 (Betfair API limit).
    """
    snapshots = {}
    for i in range(0, len(market_ids), 10):
        batch = market_ids[i:i+10]
        try:
            books = client.betting.list_market_book(
                market_ids=batch,
                price_projection=price_projection(
                    price_data=price_data(ex_best_offers=True),
                    ex_best_offers_overrides=ex_best_offers_overrides(best_prices_depth=1),
                ),
            )
            for book in books:
                if not book.runners:
                    continue
                # Pick favourite (lowest lay price = most likely winner)
                best = None
                best_lay = float("inf")
                for r in book.runners:
                    if r.status != "ACTIVE" or not r.ex or not r.ex.available_to_lay:
                        continue
                    lp = r.ex.available_to_lay[0].price
                    if lp < best_lay:
                        best_lay = lp
                        best     = r
                if best:
                    prob = round(1.0 / best_lay, 4)
                    key  = f"{book.market_id}_{best.selection_id}"
                    snapshots[key] = {
                        "prob":       prob,
                        "price":      best_lay,
                        "market_id":  book.market_id,
                        "runner_id":  best.selection_id,
                        "inplay":     book.inplay or False,
                        "ts":         datetime.now(timezone.utc).isoformat(),
                    }
        except Exception as e:
            log.warning(f"Snapshot error for batch: {e}")
    return snapshots

# ── Momentum Detection ─────────────────────────────────────────────────────────
def detect_momentum(key: str, current_prob: float, history: dict) -> str | None:
    """
    Returns 'BACK', 'LAY', or None.
    Compares current probability to the reading MOM_LOOKBACK_MIN minutes ago.
    """
    records = history.get(key, [])
    if not records:
        return None

    now    = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=config.MOM_LOOKBACK_MIN)

    # Find the oldest record within the lookback window
    old_prob = None
    for rec in records:
        try:
            ts = datetime.fromisoformat(rec["ts"])
            if ts <= cutoff:
                old_prob = rec["prob"]
        except Exception:
            continue

    if old_prob is None:
        return None

    change = current_prob - old_prob

    if change >= config.MOM_TRIGGER_PCT:
        return "BACK"   # probability rising → selection shortening
    if change <= -config.MOM_TRIGGER_PCT:
        return "LAY"    # probability falling → selection drifting
    return None

# ── Position Management ────────────────────────────────────────────────────────
def get_best_price(client: betfairlightweight.APIClient, market_id: str, runner_id: int, side: str) -> float | None:
    """Gets best available price for the given side."""
    try:
        books = client.betting.list_market_book(
            market_ids=[market_id],
            price_projection=price_projection(
                price_data=price_data(ex_best_offers=True),
                ex_best_offers_overrides=ex_best_offers_overrides(best_prices_depth=1),
            ),
        )
        for book in books:
            for runner in book.runners:
                if runner.selection_id != runner_id:
                    continue
                ex = runner.ex
                if side == "BACK" and ex.available_to_back:
                    return ex.available_to_back[0].price
                if side == "LAY" and ex.available_to_lay:
                    return ex.available_to_lay[0].price
    except Exception as e:
        log.warning(f"Price fetch error: {e}")
    return None

def open_position(
    client: betfairlightweight.APIClient,
    market_id: str,
    market_name: str,
    runner_id: int,
    side: str,
    current_prob: float,
    state: dict,
) -> bool:
    """Places a momentum entry order. Returns True if successful."""
    price = get_best_price(client, market_id, runner_id, side)
    if not price:
        return False

    stake = min(config.MAX_STAKE_GBP, config.MOM_STAKE_GBP if hasattr(config, "MOM_STAKE_GBP") else config.MAX_STAKE_GBP)

    try:
        result = client.betting.place_orders(
            market_id=market_id,
            instructions=[
                place_instruction(
                    order_type="LIMIT",
                    selection_id=runner_id,
                    side=side,
                    limit_order=limit_order(
                        size=round(stake, 2),
                        price=price,
                        persistence_type="LAPSE",
                    ),
                )
            ],
            customer_strategy_ref="MadMomentum",
        )
        report = result.place_instruction_reports[0]
        if report.status != "SUCCESS":
            log.warning(f"Order failed: {report.error_code}")
            return False

        bet_id = report.bet_id
        state["positions"].append({
            "market_id":    market_id,
            "market_name":  market_name,
            "runner_id":    runner_id,
            "side":         side,
            "entry_price":  price,
            "entry_prob":   current_prob,
            "peak_prob":    current_prob,   # BACK: track highest prob
            "trough_prob":  current_prob,   # LAY:  track lowest prob
            "stake":        round(stake, 2),
            "bet_id":       bet_id,
            "entered_at":   datetime.now(timezone.utc).isoformat(),
        })
        log.info(
            f"ENTRY | {side} {market_name} @ {price} | "
            f"Prob: {current_prob:.1%} | Stake: £{stake:.2f} | BetID: {bet_id}"
        )
        notify.b_mom_entry(market_name, side, price, stake)
        return True

    except Exception as e:
        log.error(f"Open position error: {e}")
        return False

def close_position(
    client: betfairlightweight.APIClient,
    pos: dict,
    reason: str,
    state: dict,
) -> None:
    """Closes a position by placing a hedge order at best available price."""
    close_side  = "LAY" if pos["side"] == "BACK" else "BACK"
    close_price = get_best_price(client, pos["market_id"], pos["runner_id"], close_side)

    if not close_price:
        log.warning(f"  Could not get closing price for {pos['market_name']} — leaving open.")
        return

    try:
        client.betting.place_orders(
            market_id=pos["market_id"],
            instructions=[
                place_instruction(
                    order_type="LIMIT",
                    selection_id=pos["runner_id"],
                    side=close_side,
                    limit_order=limit_order(
                        size=pos["stake"],
                        price=close_price,
                        persistence_type="LAPSE",
                    ),
                )
            ],
            customer_strategy_ref="MadMomentum",
        )

        # Approximate P&L
        ep = pos["entry_price"]
        cp = close_price
        s  = pos["stake"]
        if pos["side"] == "BACK":
            # Backed at ep, closing by laying at cp
            # Win if cp < ep (odds shortened)
            gross = s * (1.0 - cp / ep) if cp < ep else -s * (cp / ep - 1.0)
        else:
            # Laid at ep, closing by backing at cp
            # Win if cp > ep (odds lengthened)
            gross = s * (cp / ep - 1.0) if cp > ep else -s * (1.0 - cp / ep)

        net = round(gross * (1 - config.COMMISSION_RATE), 2)
        state["total_pl"] = round(state["total_pl"] + net, 2)

        log.info(
            f"EXIT  | {pos['side']} {pos['market_name']} | "
            f"Entry {ep} → Close {cp} | "
            f"P&L: £{net:+.2f} | Reason: {reason} | Total: £{state['total_pl']:.2f}"
        )
        notify.b_mom_exit(pos["market_name"], net, reason)

    except Exception as e:
        log.error(f"Close position error: {e}")

# ── Core Logic ─────────────────────────────────────────────────────────────────
def run() -> None:
    client = get_client()
    state  = load_state()
    now    = datetime.now(timezone.utc).isoformat()

    # ── 1. Scan markets and take price snapshot ────────────────────────────────
    markets    = scan_markets(client)
    market_ids = [m["market_id"] for m in markets]
    market_map = {m["market_id"]: m["market_name"] for m in markets}
    log.info(f"Scanning {len(market_ids)} markets for momentum...")

    snapshots = snapshot_prices(client, market_ids)

    # ── 2. Update price history ────────────────────────────────────────────────
    cutoff    = datetime.now(timezone.utc) - timedelta(hours=2)
    for key, snap in snapshots.items():
        history = state["price_history"].setdefault(key, [])
        history.append({"prob": snap["prob"], "ts": snap["ts"]})
        # Prune records older than 2 hours to keep state.json small
        state["price_history"][key] = [
            r for r in history
            if datetime.fromisoformat(r["ts"]) > cutoff
        ]

    # ── 3. Manage existing positions (trailing stop check) ────────────────────
    open_market_ids = {p["market_id"] for p in state["positions"]}
    surviving       = []

    for pos in state["positions"]:
        key   = f"{pos['market_id']}_{pos['runner_id']}"
        snap  = snapshots.get(key)

        if not snap:
            surviving.append(pos)
            continue

        cur_prob = snap["prob"]

        # Update peak/trough
        if pos["side"] == "BACK":
            pos["peak_prob"]   = max(pos["peak_prob"],   cur_prob)
            trail_breach       = cur_prob < pos["peak_prob"]   - config.MOM_TRAIL_PCT
            hard_stop          = cur_prob < pos["entry_prob"]   - 0.15
        else:
            pos["trough_prob"] = min(pos["trough_prob"], cur_prob)
            trail_breach       = cur_prob > pos["trough_prob"]  + config.MOM_TRAIL_PCT
            hard_stop          = cur_prob > pos["entry_prob"]   + 0.15

        if trail_breach:
            close_position(client, pos, "trailing stop", state)
            open_market_ids.discard(pos["market_id"])
        elif hard_stop:
            close_position(client, pos, "hard stop", state)
            open_market_ids.discard(pos["market_id"])
        else:
            surviving.append(pos)
            log.info(
                f"  HOLDING | {pos['side']} {pos['market_name']} | "
                f"Prob: {cur_prob:.1%} | "
                f"Peak: {pos.get('peak_prob', cur_prob):.1%} | "
                f"Entry: {pos['entry_prob']:.1%}"
            )

    state["positions"] = surviving

    # ── 4. Look for new momentum entries ──────────────────────────────────────
    total_open = len(state["positions"])

    for key, snap in snapshots.items():
        if total_open >= config.MAX_POSITIONS:
            break

        market_id = snap["market_id"]
        runner_id = snap["runner_id"]
        cur_prob  = snap["prob"]

        # Skip if already in a position on this market
        if market_id in open_market_ids:
            continue

        signal = detect_momentum(key, cur_prob, state["price_history"])
        if not signal:
            continue

        market_name = market_map.get(market_id, market_id)
        log.info(
            f"SIGNAL | {signal} | {market_name} | "
            f"Prob: {cur_prob:.1%} | Key: {key}"
        )

        success = open_position(client, market_id, market_name, runner_id, signal, cur_prob, state)
        if success:
            open_market_ids.add(market_id)
            total_open += 1

    # ── 5. Save ────────────────────────────────────────────────────────────────
    state["last_run"] = now
    save_state(state)
    log.info(
        f"Run complete | Open positions: {len(state['positions'])} | "
        f"Total P&L: £{state['total_pl']:.2f}"
    )

# ── Entry Point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    run()

