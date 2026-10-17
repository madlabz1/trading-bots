"""
Bot 4: News Trigger
====================
Monitors free BBC Sport / Guardian RSS feeds for market-moving headlines.
When a relevant headline matches an active Betfair market, places a bet.

Signal logic:
  NEGATIVE keyword + team/player name in headline → LAY that runner
    (e.g. "Arsenal striker ruled out" → LAY Arsenal)

  POSITIVE keyword + team/player name in headline → BACK that runner
    (e.g. "Salah fit and available for Liverpool" → BACK Liverpool)

Matching:
  Fetches runner names from Betfair market catalogue, then checks if any
  runner name (or significant word from it) appears in the headline.
  Uses a 60-minute cooldown per market to avoid duplicate trades.

News sources (free, no API key):
  - BBC Sport Football RSS
  - BBC Sport RSS
  - BBC News RSS  (politics / current events)
  - The Guardian Football RSS

Schedule: Every 1 minute via Windows Task Scheduler
"""

import hashlib
import json
import logging
import os
import sys
from datetime import datetime, timezone, timedelta
import xml.etree.ElementTree as ET

import betfairlightweight
import requests
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

# ── News Sources ───────────────────────────────────────────────────────────────
RSS_FEEDS = [
    "https://feeds.bbci.co.uk/sport/football/rss.xml",
    "https://feeds.bbci.co.uk/sport/rss.xml",
    "https://feeds.bbci.co.uk/news/rss.xml",
    "https://www.theguardian.com/football/rss",
    "https://www.theguardian.com/sport/horseracing/rss",
    "https://www.theguardian.com/politics/rss",
]

# ── Signal Keywords ────────────────────────────────────────────────────────────
NEGATIVE_KEYWORDS = [
    "ruled out", "injury", "injured", "suspended", "suspension",
    "withdrawn", "doubt", "doubtful", "sacked", "fired",
    "banned", "red card", "hamstring", "crisis", "struggling",
    "poor form", "relegation", "winless",
]

POSITIVE_KEYWORDS = [
    "returns from injury", "fit and available", "cleared to play",
    "back in training", "available for selection", "clean bill of health",
    "winning run", "unbeaten", "in form", "strong form", "returns to squad",
]

# Short words to skip when matching runner names (avoid false positives)
STOP_WORDS = {
    "fc", "afc", "the", "and", "city", "town", "united", "rovers",
    "athletic", "wanderers", "county", "park",
}

# ── State ──────────────────────────────────────────────────────────────────────
def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {
        "positions":     [],
        "seen_headlines": [],   # hashes of processed headlines
        "triggered":     [],    # {market_id, ts} cooldown tracker
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
            json={"bot_id": "bot4", "bot_name": "News Trigger",
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

# ── RSS Feed Fetching ──────────────────────────────────────────────────────────
def fetch_recent_headlines(max_age_minutes: int = 30) -> list[dict]:
    """
    Returns list of {title, link, published_ts} for articles
    published within the last max_age_minutes.
    """
    headers = {"User-Agent": "Mozilla/5.0 (compatible; TradingBot/1.0)"}
    cutoff  = datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)
    results = []

    for url in RSS_FEEDS:
        try:
            resp = requests.get(url, headers=headers, timeout=8)
            resp.raise_for_status()
            root = ET.fromstring(resp.content)

            for item in root.iter("item"):
                title   = item.findtext("title",   "").strip()
                link    = item.findtext("link",    "").strip()
                pub_raw = item.findtext("pubDate", "")

                if not title:
                    continue

                # Parse publish date
                try:
                    from email.utils import parsedate_to_datetime
                    pub_dt = parsedate_to_datetime(pub_raw)
                    if pub_dt.tzinfo is None:
                        pub_dt = pub_dt.replace(tzinfo=timezone.utc)
                    if pub_dt < cutoff:
                        continue
                except Exception:
                    pass  # include if we can't parse the date

                results.append({"title": title, "link": link})

        except Exception as e:
            log.debug(f"RSS fetch error ({url}): {e}")

    log.info(f"Fetched {len(results)} recent headlines from {len(RSS_FEEDS)} feeds.")
    return results

# ── Market Catalogue ───────────────────────────────────────────────────────────
def get_markets_with_runners(client: betfairlightweight.APIClient) -> list[dict]:
    """
    Returns active pre-event markets with runner names for matching.
    """
    markets = []
    for event_id, mtype, countries in [
        ("1",  "MATCH_ODDS", ["GB"]),
        ("7",  "WIN",        ["GB", "IE"]),
        ("2",  "MATCH_ODDS", None),
        ("21", "WINNER",     None),   # Outright politics/specials
    ]:
        try:
            filt_kwargs = {
                "event_type_ids":   [event_id],
                "market_type_codes": [mtype],
            }
            if countries:
                filt_kwargs["market_countries"] = countries

            cats = client.betting.list_market_catalogue(
                filter=market_filter(**filt_kwargs),
                market_projection=["RUNNER_DESCRIPTION", "MARKET_START_TIME"],
                sort="FIRST_TO_START",
                max_results=30,
            )
            for c in cats:
                runners = []
                if c.runners:
                    for r in c.runners:
                        name = (r.runner_name or "").strip()
                        if name:
                            runners.append({"id": r.selection_id, "name": name})
                markets.append({
                    "market_id":   c.market_id,
                    "market_name": c.market_name,
                    "runners":     runners,
                })
        except Exception as e:
            log.warning(f"Market fetch error (event={event_id}): {e}")

    log.info(f"Loaded {len(markets)} markets with runner data.")
    return markets

# ── Headline Matching ──────────────────────────────────────────────────────────
def extract_signal(headline: str) -> str | None:
    """Returns 'BACK', 'LAY', or None based on keywords in the headline."""
    hl = headline.lower()
    for kw in NEGATIVE_KEYWORDS:
        if kw in hl:
            return "LAY"
    for kw in POSITIVE_KEYWORDS:
        if kw in hl:
            return "BACK"
    return None

def match_to_market(headline: str, markets: list[dict]) -> tuple | None:
    """
    Returns (market_id, market_name, runner_id, runner_name, signal) or None.
    Matches runner names against headline text.
    """
    signal = extract_signal(headline)
    if not signal:
        return None

    hl = headline.lower()

    for mkt in markets:
        for runner in mkt["runners"]:
            rname = runner["name"].lower()

            # Full name match
            if rname in hl:
                return (mkt["market_id"], mkt["market_name"],
                        runner["id"], runner["name"], signal)

            # Significant word match (word > 4 chars, not a stop word)
            words = [w for w in rname.split() if len(w) > 4 and w not in STOP_WORDS]
            if words and all(w in hl for w in words[:2]):
                return (mkt["market_id"], mkt["market_name"],
                        runner["id"], runner["name"], signal)

    return None

# ── Order Placement ────────────────────────────────────────────────────────────
def place_news_trade(
    client: betfairlightweight.APIClient,
    market_id: str,
    market_name: str,
    runner_id: int,
    runner_name: str,
    side: str,
    headline: str,
    state: dict,
) -> bool:
    # Get current best price
    try:
        books = client.betting.list_market_book(
            market_ids=[market_id],
            price_projection=price_projection(
                price_data=price_data(ex_best_offers=True),
                ex_best_offers_overrides=ex_best_offers_overrides(best_prices_depth=1),
            ),
        )
        price = None
        for book in books:
            if book.inplay:
                log.info(f"  {market_name} is already in-play — skipping news trade.")
                return False
            for r in book.runners:
                if r.selection_id != runner_id:
                    continue
                ex = r.ex
                if side == "BACK" and ex.available_to_back:
                    price = ex.available_to_back[0].price
                elif side == "LAY" and ex.available_to_lay:
                    price = ex.available_to_lay[0].price
    except Exception as e:
        log.warning(f"  Price fetch error: {e}")
        return False

    if not price:
        log.warning(f"  No price available for {runner_name} in {market_name}")
        return False

    stake = config.NEWS_STAKE_GBP

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
            customer_strategy_ref="MadNewsTrigger",
        )
        report = result.place_instruction_reports[0]
        if report.status != "SUCCESS":
            log.warning(f"  Order failed: {report.error_code}")
            return False

        state["positions"].append({
            "market_id":   market_id,
            "market_name": market_name,
            "runner_id":   runner_id,
            "runner_name": runner_name,
            "side":        side,
            "price":       price,
            "stake":       stake,
            "bet_id":      report.bet_id,
            "headline":    headline[:120],
            "placed_at":   datetime.now(timezone.utc).isoformat(),
        })
        log.info(
            f"  NEWS TRADE | {side} {runner_name} @ {price} in {market_name} | "
            f"Stake: £{stake:.2f}\n"
            f"  Headline: {headline[:100]}"
        )
        notify.b_news_trade(headline, market_name, side, stake)
        return True

    except Exception as e:
        log.error(f"  Place order error: {e}")
        return False

# ── Core Logic ─────────────────────────────────────────────────────────────────
def run() -> None:
    client   = get_client()
    state    = load_state()
    now      = datetime.now(timezone.utc)

    # ── 1. Prune old state ─────────────────────────────────────────────────────
    cutoff_6h  = now - timedelta(hours=6)
    cutoff_cd  = now - timedelta(minutes=config.NEWS_COOLDOWN_MIN)

    state["seen_headlines"] = state["seen_headlines"][-500:]  # keep last 500 hashes
    state["triggered"] = [
        t for t in state.get("triggered", [])
        if datetime.fromisoformat(t["ts"]) > cutoff_cd
    ]
    on_cooldown = {t["market_id"] for t in state["triggered"]}

    # ── 2. Fetch news ──────────────────────────────────────────────────────────
    headlines = fetch_recent_headlines(max_age_minutes=30)
    if not headlines:
        log.info("No recent headlines found.")
        state["last_run"] = now.isoformat()
        save_state(state)
        return

    # ── 3. Get active markets with runner names ────────────────────────────────
    markets = get_markets_with_runners(client)
    if not markets:
        log.info("No active markets found.")
        state["last_run"] = now.isoformat()
        save_state(state)
        return

    # ── 4. Process headlines ───────────────────────────────────────────────────
    open_count  = len(state["positions"])
    trades_made = 0

    for item in headlines:
        if open_count + trades_made >= config.MAX_POSITIONS:
            break

        headline = item["title"]
        hl_hash  = hashlib.md5(headline.encode()).hexdigest()

        if hl_hash in state["seen_headlines"]:
            continue
        state["seen_headlines"].append(hl_hash)

        match = match_to_market(headline, markets)
        if not match:
            continue

        market_id, market_name, runner_id, runner_name, signal = match

        if market_id in on_cooldown:
            log.info(f"  {market_name} on cooldown — skipping.")
            continue

        log.info(f"MATCH | '{headline[:80]}' → {signal} {runner_name} in {market_name}")

        success = place_news_trade(
            client, market_id, market_name,
            runner_id, runner_name, signal, headline, state,
        )
        if success:
            trades_made += 1
            on_cooldown.add(market_id)
            state["triggered"].append({
                "market_id": market_id,
                "ts":        now.isoformat(),
            })

    # ── 5. Save ────────────────────────────────────────────────────────────────
    state["last_run"] = now.isoformat()
    save_state(state)
    log.info(
        f"Run complete | Trades placed: {trades_made} | "
        f"Open positions: {len(state['positions'])} | "
        f"Total P&L: £{state['total_pl']:.2f}"
    )

# ── Entry Point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    run()

