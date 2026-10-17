"""
Bot 3: Sharp Money Copier
==========================
Detects large bets being placed on Betfair and copies them proportionally.

How it detects sharp money:
  The Betfair REST API doesn't expose individual bet sizes, but we can infer
  large trades by comparing snapshots between runs (every 1 minute):

  Signal = large volume matched + price moved in one direction

  BACK signal:
    → total_matched jumped by >= SHARP_MIN_TRADE in the last minute
    → AND best back price shortened (decreased) by >= 1 tick
    → Inference: someone placed a large back bet, pushing the price in

  LAY signal:
    → total_matched jumped by >= SHARP_MIN_TRADE in the last minute
    → AND best lay price drifted (increased) by >= 1 tick
    → Inference: someone placed a large lay bet, pushing the price out

  Copy size = min(inferred_trade * SHARP_SCALE, MAX_STAKE_GBP)
  e.g. £500 sharp trade × 10% = £50 copy (our max)

Positions are closed when the market goes in-play (event starts),
or if they hit a hard stop of 15% probability reversal.

Schedule: Every 1 minute via Windows Task Scheduler
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

# ── Tick helpers ───────────────────────────────────────────────────────────────
_TICK_BANDS = [
    (1.01, 2.0,   0.01), (2.0,  3.0,   0.02), (3.0,  4.0,   0.05),
    (4.0,  6.0,   0.10), (6.0,  10.0,  0.20), (10.0, 20.0,  0.50),
    (20.0, 30.0,  1.00), (30.0, 50.0,  2.00), (50.0, 100.0, 5.00),
]

def tick_size(price: float) -> float:
    for lo, hi, t in _TICK_BANDS:
        if lo <= price < hi:
            return t
    return 10.0

# ── State ──────────────────────────────────────────────────────────────────────
def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {
        "positions":        [],
        "market_snapshots": {},
        "copied_signals":   [],   # prevents re-copying the same move
        "total_pl":         0.0,
        "last_run":         None,
    }

def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
    try:
        import requests as _rq
        _rq.post(
            f"{config.SUPABASE_URL}/rest/v1/bot_state",
            json={"bot_id": "bot3", "bot_name": "Sharp Copy",
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

# ── Market Scan ────────────────────────────────────────────────────────────────
def scan_markets(client: betfairlightweight.APIClient) -> list[dict]:
    markets = []
    for event_id, mtype, countries in [
        ("1", "MATCH_ODDS", ["GB"]),
        ("7", "WIN",        ["GB", "IE"]),
        ("2", "MATCH_ODDS", None),
    ]:
        try:
            filt = market_filter(
                event_type_ids=[event_id],
                market_type_codes=[mtype],
                **({"market_countries": countries} if countries else {}),
            )
            cats = client.betting.list_market_catalogue(
                filter=filt,
                market_projection=["RUNNER_DESCRIPTION"],
                sort="FIRST_TO_START",
                max_results=20,
            )
            for c in cats:
                markets.append({"market_id": c.market_id, "market_name": c.market_name})
        except Exception as e:
            log.warning(f"Scan error: {e}")
    return markets

# ── Snapshot ───────────────────────────────────────────────────────────────────
def take_snapshot(client: betfairlightweight.APIClient, market_ids: list[str]) -> dict:
    """
    Returns {market_id: {runner_id, best_back, best_lay, total_matched, inplay}}
    for the favourite in each market.
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
                best_runner = None
                best_lay    = float("inf")
                for r in book.runners:
                    if r.status != "ACTIVE" or not r.ex or not r.ex.available_to_lay:
                        continue
                    lp = r.ex.available_to_lay[0].price
                    if lp < best_lay:
                        best_lay    = lp
                        best_runner = r

                if not best_runner:
                    continue

                ex         = best_runner.ex
                back_price = ex.available_to_back[0].price if ex.available_to_back else None
                lay_price  = ex.available_to_lay[0].price  if ex.available_to_lay  else None

                if back_price and lay_price:
                    result[book.market_id] = {
                        "runner_id":     best_runner.selection_id,
                        "best_back":     back_price,
                        "best_lay":      lay_price,
                        "total_matched": book.total_matched or 0,
                        "inplay":        book.inplay or False,
                        "ts":            datetime.now(timezone.utc).isoformat(),
                    }
        except Exception as e:
            log.warning(f"Snapshot batch error: {e}")
    return result

# ── Signal Detection ───────────────────────────────────────────────────────────
def detect_sharp_signal(
    market_id: str,
    current: dict,
    previous: dict | None,
) -> str | None:
    """
    Returns 'BACK', 'LAY', or None.
    Requires volume jump >= SHARP_MIN_TRADE and at least 1-tick price move.
    """
    if not previous:
        return None

    volume_jump = current["total_matched"] - previous["total_matched"]
    if volume_jump < config.SHARP_MIN_TRADE:
        return None

    prev_back = previous["best_back"]
    cur_back  = current["best_back"]
    prev_lay  = previous["best_lay"]
    cur_lay   = current["best_lay"]

    # Price shortened + large volume = sharp back bet
    if cur_back < prev_back - tick_size(prev_back) * 0.5:
        log.info(
            f"  SHARP BACK signal | {market_id} | "
            f"Volume +£{volume_jump:,.0f} | "
            f"Back: {prev_back} → {cur_back}"
        )
        return "BACK"

    # Price drifted + large volume = sharp lay bet
    if cur_lay > prev_lay + tick_size(prev_lay) * 0.5:
        log.info(
            f"  SHARP LAY signal | {market_id} | "
            f"Volume +£{volume_jump:,.0f} | "
            f"Lay: {prev_lay} → {cur_lay}"
        )
        return "LAY"

    return None

# ── Order Placement ────────────────────────────────────────────────────────────
def copy_trade(
    client: betfairlightweight.APIClient,
    market_id: str,
    market_name: str,
    runner_id: int,
    side: str,
    price: float,
    volume_jump: float,
    state: dict,
) -> bool:
    stake = min(
        round(volume_jump * config.SHARP_SCALE, 2),
        config.MAX_STAKE_GBP,
    )
    if stake < config.MIN_STAKE_GBP:
        log.info(f"  Copy stake £{stake:.2f} below minimum — skipping.")
        return False

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
            customer_strategy_ref="MadSharpCopy",
        )
        report = result.place_instruction_reports[0]
        if report.status != "SUCCESS":
            log.warning(f"  Copy order failed: {report.error_code}")
            return False

        bet_id = report.bet_id
        state["positions"].append({
            "market_id":    market_id,
            "market_name":  market_name,
            "runner_id":    runner_id,
            "side":         side,
            "entry_price":  price,
            "entry_prob":   round(1.0 / price, 4),
            "stake":        stake,
            "bet_id":       bet_id,
            "entered_at":   datetime.now(timezone.utc).isoformat(),
        })

        log.info(
            f"  COPIED | {side} {market_name} @ {price} | "
            f"Stake: £{stake:.2f} (from £{volume_jump:,.0f} sharp trade)"
        )
        notify.b_sharp_copied(market_name, side, price, stake, volume_jump)
        return True

    except Exception as e:
        log.error(f"  Copy trade error: {e}")
        return False

# ── Position Monitor ───────────────────────────────────────────────────────────
def monitor_positions(
    client: betfairlightweight.APIClient,
    current_snapshots: dict,
    state: dict,
) -> None:
    """Close positions when market goes in-play (event started) or hard stop hit."""
    surviving = []
    for pos in state["positions"]:
        snap = current_snapshots.get(pos["market_id"])

        if not snap:
            surviving.append(pos)
            continue

        cur_prob   = round(1.0 / snap["best_lay"], 4)
        entry_prob = pos["entry_prob"]

        # Hard stop: 15% probability reversal
        if pos["side"] == "BACK":
            hard_stop = cur_prob < entry_prob - 0.15
        else:
            hard_stop = cur_prob > entry_prob + 0.15

        # Close if market went in-play (race started / match kicked off)
        close_on_inplay = snap["inplay"]

        if hard_stop or close_on_inplay:
            reason = "in-play" if close_on_inplay else "hard stop"
            close_side  = "LAY" if pos["side"] == "BACK" else "BACK"
            close_price = snap["best_lay"] if close_side == "LAY" else snap["best_back"]

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
                    customer_strategy_ref="MadSharpCopy",
                )
                ep, cp, s = pos["entry_price"], close_price, pos["stake"]
                if pos["side"] == "BACK":
                    gross = s * (1.0 - cp / ep) if cp < ep else -s * (cp / ep - 1.0)
                else:
                    gross = s * (cp / ep - 1.0) if cp > ep else -s * (1.0 - cp / ep)

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

    # ── 1. Scan & snapshot ─────────────────────────────────────────────────────
    markets    = scan_markets(client)
    market_ids = [m["market_id"] for m in markets]
    market_map = {m["market_id"]: m["market_name"] for m in markets}
    log.info(f"Monitoring {len(market_ids)} markets for sharp money...")

    current = take_snapshot(client, market_ids)

    # ── 2. Monitor existing positions ──────────────────────────────────────────
    monitor_positions(client, current, state)

    # ── 3. Detect signals and copy ─────────────────────────────────────────────
    open_count = len(state["positions"])
    prev_snaps = state.get("market_snapshots", {})

    # Prune old copied signals (older than 6 hours)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=6)
    state["copied_signals"] = [
        s for s in state.get("copied_signals", [])
        if datetime.fromisoformat(s["ts"]) > cutoff
    ]
    already_copied = {s["market_id"] for s in state["copied_signals"]}

    for market_id, snap in current.items():
        if open_count >= config.MAX_POSITIONS:
            break

        if market_id in already_copied:
            continue

        if snap["inplay"]:
            continue

        prev      = prev_snaps.get(market_id)
        signal    = detect_sharp_signal(market_id, snap, prev)

        if not signal:
            continue

        price      = snap["best_back"] if signal == "BACK" else snap["best_lay"]
        vol_jump   = snap["total_matched"] - (prev["total_matched"] if prev else 0)
        market_name = market_map.get(market_id, market_id)

        success = copy_trade(
            client, market_id, market_name,
            snap["runner_id"], signal, price, vol_jump, state,
        )
        if success:
            open_count += 1
            state["copied_signals"].append({
                "market_id": market_id,
                "ts":        datetime.now(timezone.utc).isoformat(),
            })

    # ── 4. Update snapshots & save ─────────────────────────────────────────────
    state["market_snapshots"] = current
    state["last_run"]         = datetime.now(timezone.utc).isoformat()
    save_state(state)
    log.info(
        f"Run complete | Open positions: {len(state['positions'])} | "
        f"Total P&L: £{state['total_pl']:.2f}"
    )

# ── Entry Point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    run()

