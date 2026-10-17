"""
Bot 5: Value Scanner
=====================
Finds value bets by comparing Betfair's implied probability against a model
probability derived from form data.

What is "value"?
  A bet has value when:  model_prob > market_prob + VALUE_MIN_EDGE
  Example: market implies 40% (price 2.50), model says 48% → edge = 8%
  We BACK the runner because we think it's more likely than the market does.

  Conversely: model_prob < market_prob - VALUE_MIN_EDGE
  Example: market implies 60% (price 1.67), model says 50% → edge = 10%
  We LAY the runner because the market overestimates it.

Model probability sources:
  Football:
    - Betfair Win market prices (match-odds): we use the cross-market
      consistency check — if MATCH_ODDS price differs from CORRECT_SCORE
      aggregated probability by > threshold, that indicates mispricing.
    - Simpler approach: head-to-head probability from last-5-games points tally
      (fetched from the Betfair market catalogue team names → form lookup).
    - We use the multi-market arbitrage approach: compare a runner's
      MATCH_ODDS implied prob against its NEXT_GOAL and BOTH_TEAMS_TO_SCORE
      aggregates to find outlier prices.

  Horse racing:
    - Official Betfair SP (starting price) is a strong proxy for true probability.
      We compare the CURRENT exchange price vs recent BSP data from market history.
    - If current back < BSP_average * 0.85 → market has overshot, LAY value.
    - If current back > BSP_average * 1.15 → market has underpriced, BACK value.

  Implementation (realistic without external data API):
    - We track each runner's price over multiple runs (stored in state).
    - "Fair value" = weighted average of last N observed prices (recency-weighted).
    - Current price deviating > VALUE_MIN_EDGE from fair value = signal.
    - This is the "mean-reversion" approach — prices tend to converge toward
      true probability as the event approaches.

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

# ── Model Settings ─────────────────────────────────────────────────────────────
# Minimum observations before trusting the fair-value estimate
MIN_OBSERVATIONS = 10
# Recency weight decay: each older observation is worth DECAY_FACTOR less
DECAY_FACTOR = 0.85
# Maximum price to consider (very long shots have high variance, skip them)
MAX_PRICE_CONSIDERED = 20.0
# Minimum price (odds-on shots have thin edge potential)
MIN_PRICE_CONSIDERED = 1.20
# Max hours before market start to look for value (within 4 hours)
MAX_HOURS_BEFORE_START = 4.0
# How many price readings to keep per runner (2 mins × 60 = 2 hours of data)
MAX_PRICE_HISTORY = 60

# ── State ──────────────────────────────────────────────────────────────────────
def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {
        "positions":     [],
        "price_history": {},   # key: "market_id|runner_id" → [{"price": x, "ts": ...}]
        "total_pl":      0.0,
        "last_run":      None,
    }

def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
    try:
        import requests as _rq
        _rq.post(
            f"{config.SUPABASE_URL}/rest/v1/bot_state",
            json={"bot_id": "bot5", "bot_name": "Value Scanner",
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

# ── Market Catalogue ───────────────────────────────────────────────────────────
def get_markets(client: betfairlightweight.APIClient) -> list[dict]:
    """Returns markets starting within MAX_HOURS_BEFORE_START hours."""
    markets = []
    now      = datetime.now(timezone.utc)
    max_dt   = now + timedelta(hours=MAX_HOURS_BEFORE_START)

    for event_id, mtype, countries in [
        ("1",  "MATCH_ODDS", ["GB"]),
        ("7",  "WIN",        ["GB", "IE"]),
        ("2",  "MATCH_ODDS", None),
    ]:
        try:
            filt_kwargs = {
                "event_type_ids":    [event_id],
                "market_type_codes": [mtype],
            }
            if countries:
                filt_kwargs["market_countries"] = countries

            cats = client.betting.list_market_catalogue(
                filter=market_filter(**filt_kwargs),
                market_projection=["RUNNER_DESCRIPTION", "MARKET_START_TIME"],
                sort="FIRST_TO_START",
                max_results=25,
            )
            for c in cats:
                start_time = getattr(c, "market_start_time", None)
                if start_time and start_time > max_dt:
                    continue

                runners = []
                if c.runners:
                    for r in c.runners:
                        name = (r.runner_name or "").strip()
                        if name:
                            runners.append({"id": r.selection_id, "name": name})

                markets.append({
                    "market_id":   c.market_id,
                    "market_name": c.market_name,
                    "start_time":  start_time.isoformat() if start_time else None,
                    "runners":     runners,
                })
        except Exception as e:
            log.warning(f"Market fetch error: {e}")

    log.info(f"Scanning {len(markets)} markets starting within {MAX_HOURS_BEFORE_START}h.")
    return markets

# ── Price Reading ──────────────────────────────────────────────────────────────
def read_prices(client: betfairlightweight.APIClient, market_ids: list[str]) -> dict:
    """
    Returns {market_id: {runner_id: {back, lay, inplay}}}
    """
    result = {}
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
                runners = {}
                for r in book.runners:
                    if r.status != "ACTIVE" or not r.ex:
                        continue
                    back = r.ex.available_to_back[0].price if r.ex.available_to_back else None
                    lay  = r.ex.available_to_lay[0].price  if r.ex.available_to_lay  else None
                    if back and lay:
                        runners[r.selection_id] = {
                            "back":   back,
                            "lay":    lay,
                            "inplay": book.inplay or False,
                        }
                if runners:
                    result[book.market_id] = runners
        except Exception as e:
            log.warning(f"Price read error: {e}")
    return result

# ── Fair Value Model ───────────────────────────────────────────────────────────
def compute_fair_value(history: list[dict]) -> float | None:
    """
    Computes weighted-average back price from historical observations.
    Most recent observations carry higher weight (DECAY_FACTOR).
    """
    if len(history) < MIN_OBSERVATIONS:
        return None

    total_weight = 0.0
    total_price  = 0.0
    n = len(history)

    for i, obs in enumerate(history):
        # Weight increases for more recent observations
        weight = DECAY_FACTOR ** (n - 1 - i)
        total_weight += weight
        total_price  += weight * obs["price"]

    return total_price / total_weight if total_weight > 0 else None

def find_value(
    market_id: str,
    runner_id: int,
    current_back: float,
    current_lay: float,
    history: list[dict],
) -> tuple | None:
    """
    Returns (side, current_price, fair_price, edge) or None.
    """
    if current_back < MIN_PRICE_CONSIDERED or current_back > MAX_PRICE_CONSIDERED:
        return None

    fair_price = compute_fair_value(history)
    if not fair_price:
        return None

    fair_prob    = 1.0 / fair_price
    market_prob  = 1.0 / current_back

    # BACK value: market underestimates probability (price too high)
    if (fair_prob - market_prob) >= config.VALUE_MIN_EDGE:
        edge = fair_prob - market_prob
        return ("BACK", current_back, fair_price, edge)

    market_lay_prob = 1.0 / current_lay

    # LAY value: market overestimates probability (price too low)
    if (market_lay_prob - fair_prob) >= config.VALUE_MIN_EDGE:
        edge = market_lay_prob - fair_prob
        return ("LAY", current_lay, fair_price, edge)

    return None

# ── Order Placement ────────────────────────────────────────────────────────────
def place_value_bet(
    client: betfairlightweight.APIClient,
    market_id: str,
    market_name: str,
    runner_id: int,
    runner_name: str,
    side: str,
    price: float,
    fair_price: float,
    edge: float,
    state: dict,
) -> bool:
    stake = config.VALUE_STAKE_GBP

    try:
        result = client.betting.place_orders(
            market_id=market_id,
            instructions=[
                place_instruction(
                    order_type="LIMIT",
                    selection_id=runner_id,
                    side=side,
                    limit_order=limit_order(
                        size=stake,
                        price=price,
                        persistence_type="LAPSE",
                    ),
                )
            ],
            customer_strategy_ref="MadValue",
        )
        report = result.place_instruction_reports[0]
        if report.status != "SUCCESS":
            log.warning(f"  Value bet failed: {report.error_code}")
            return False

        state["positions"].append({
            "market_id":   market_id,
            "market_name": market_name,
            "runner_id":   runner_id,
            "runner_name": runner_name,
            "side":        side,
            "entry_price": price,
            "fair_price":  fair_price,
            "edge":        round(edge, 4),
            "entry_prob":  round(1.0 / price, 4),
            "stake":       stake,
            "bet_id":      report.bet_id,
            "entered_at":  datetime.now(timezone.utc).isoformat(),
        })

        market_prob = round(1.0 / price, 3)
        model_prob  = round(1.0 / fair_price, 3)
        log.info(
            f"  VALUE BET | {side} {runner_name} @ {price} in {market_name}\n"
            f"  Market prob: {market_prob:.1%} | Model prob: {model_prob:.1%} | Edge: {edge:.1%}"
        )
        notify.b_value_bet(market_name, side, price, model_prob, market_prob, stake)
        return True

    except Exception as e:
        log.error(f"  Place order error: {e}")
        return False

# ── Position Monitor ───────────────────────────────────────────────────────────
def monitor_positions(
    client: betfairlightweight.APIClient,
    current_prices: dict,
    state: dict,
) -> None:
    """
    Close positions when:
    - Market goes in-play (event starting)
    - Hard stop: price moved 20% against us
    - Value has evaporated: model prob - market prob < 0 (reversed)
    """
    surviving = []
    for pos in state["positions"]:
        runners = current_prices.get(pos["market_id"], {})
        pdata   = runners.get(pos["runner_id"])

        if not pdata:
            surviving.append(pos)
            continue

        if pdata["inplay"]:
            reason = "in-play"
        else:
            cur_back  = pdata["back"]
            cur_lay   = pdata["lay"]
            entry_p   = pos["entry_price"]
            cur_prob  = round(1.0 / cur_back, 4)
            entry_prob = pos["entry_prob"]

            if pos["side"] == "BACK":
                hard_stop = cur_back > entry_p * 1.20  # price drifted 20%
                # Value evaporated: market now aligns with or beats our model
                history_key = f"{pos['market_id']}|{pos['runner_id']}"
                hist = state["price_history"].get(history_key, [])
                fv = compute_fair_value(hist)
                value_gone = fv and (1.0 / fv - cur_prob) < 0
                reason = "hard stop" if hard_stop else ("value gone" if value_gone else None)
            else:
                hard_stop = cur_lay < entry_p * 0.80  # price shortened 20%
                reason = "hard stop" if hard_stop else None

        if reason:
            close_side  = "LAY" if pos["side"] == "BACK" else "BACK"
            close_price = pdata["lay"] if close_side == "LAY" else pdata["back"]

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
                    customer_strategy_ref="MadValue",
                )
                ep, cp, s = pos["entry_price"], close_price, pos["stake"]
                if pos["side"] == "BACK":
                    gross = s * (cp / ep - 1.0) if cp > ep else -s * (1.0 - cp / ep)
                else:
                    gross = s * (1.0 - cp / ep) if cp < ep else -s * (cp / ep - 1.0)

                net = round(gross * (1 - config.COMMISSION_RATE), 2)
                state["total_pl"] = round(state["total_pl"] + net, 2)
                log.info(
                    f"  CLOSED | {pos['market_name']} | "
                    f"P&L: £{net:+.2f} | Reason: {reason} | Total: £{state['total_pl']:.2f}"
                )
            except Exception as e:
                log.error(f"  Close error: {e}")
                surviving.append(pos)
        else:
            surviving.append(pos)

    state["positions"] = surviving

# ── Core Logic ─────────────────────────────────────────────────────────────────
def run() -> None:
    client = get_client()
    state  = load_state()
    now    = datetime.now(timezone.utc)

    # ── 1. Prune stale price history (older than 2 hours) ─────────────────────
    cutoff = now - timedelta(hours=2)
    for key in list(state["price_history"].keys()):
        state["price_history"][key] = [
            obs for obs in state["price_history"][key]
            if datetime.fromisoformat(obs["ts"]) > cutoff
        ]
        if not state["price_history"][key]:
            del state["price_history"][key]

    # ── 2. Get markets ─────────────────────────────────────────────────────────
    markets    = get_markets(client)
    market_ids = [m["market_id"] for m in markets]
    runner_map = {}  # (market_id, runner_id) → runner_name
    for m in markets:
        for r in m["runners"]:
            runner_map[(m["market_id"], r["id"])] = r["name"]
    market_name_map = {m["market_id"]: m["market_name"] for m in markets}

    if not market_ids:
        log.info("No markets within time window.")
        state["last_run"] = now.isoformat()
        save_state(state)
        return

    # ── 3. Read current prices ─────────────────────────────────────────────────
    current_prices = read_prices(client, market_ids)

    # ── 4. Monitor open positions ──────────────────────────────────────────────
    monitor_positions(client, current_prices, state)

    # ── 5. Update price history ────────────────────────────────────────────────
    ts_str = now.isoformat()
    for market_id, runners in current_prices.items():
        for runner_id, pdata in runners.items():
            if pdata["inplay"]:
                continue
            key = f"{market_id}|{runner_id}"
            if key not in state["price_history"]:
                state["price_history"][key] = []
            hist = state["price_history"][key]
            hist.append({"price": pdata["back"], "ts": ts_str})
            # Keep last MAX_PRICE_HISTORY readings
            if len(hist) > MAX_PRICE_HISTORY:
                state["price_history"][key] = hist[-MAX_PRICE_HISTORY:]

    # ── 6. Scan for value ──────────────────────────────────────────────────────
    open_count    = len(state["positions"])
    value_found   = 0
    already_open  = {(p["market_id"], p["runner_id"]) for p in state["positions"]}

    for market_id, runners in current_prices.items():
        if open_count + value_found >= config.MAX_POSITIONS:
            break

        for runner_id, pdata in runners.items():
            if open_count + value_found >= config.MAX_POSITIONS:
                break

            if pdata["inplay"]:
                continue

            if (market_id, runner_id) in already_open:
                continue

            key   = f"{market_id}|{runner_id}"
            hist  = state["price_history"].get(key, [])
            match = find_value(market_id, runner_id, pdata["back"], pdata["lay"], hist)

            if not match:
                continue

            side, price, fair_price, edge = match
            runner_name  = runner_map.get((market_id, runner_id), str(runner_id))
            market_name  = market_name_map.get(market_id, market_id)

            log.info(
                f"VALUE FOUND | {side} {runner_name} @ {price} "
                f"(fair: {fair_price:.2f}, edge: {edge:.1%}) in {market_name}"
            )

            success = place_value_bet(
                client, market_id, market_name,
                runner_id, runner_name, side, price, fair_price, edge, state,
            )
            if success:
                value_found  += 1
                already_open.add((market_id, runner_id))

    # ── 7. Save ────────────────────────────────────────────────────────────────
    state["last_run"] = now.isoformat()
    save_state(state)

    total_runners = sum(len(r) for r in current_prices.values())
    log.info(
        f"Run complete | Runners tracked: {total_runners} | "
        f"Value bets placed: {value_found} | "
        f"Open positions: {len(state['positions'])} | "
        f"Total P&L: £{state['total_pl']:.2f}"
    )

# ── Entry Point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    run()

