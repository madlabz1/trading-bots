"""
Betfair Trading Dashboard
==========================
Reads bot state from Supabase (cloud) so it works on Vercel.
Falls back to local state.json files when Supabase is unavailable.

Local:  python dashboard/app.py  → http://localhost:5001
Vercel: vercel deploy (from betfair/ directory)
"""

import json
import os
from datetime import datetime, timezone
from flask import Flask, render_template_string

import requests as _requests

app = Flask(__name__)

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
BOTS_DIR  = os.path.dirname(BASE_DIR)   # betfair/

SUPABASE_URL      = os.environ.get("SUPABASE_URL") or "https://gfmlonjjlkqpntupzqdf.supabase.co"
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY") or "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImdmbWxvbmpqbGtxcG50dXB6cWRmIiwicm9sZSI6ImFub24iLCJpYXQiOjE3OTIxODI5NTUsImV4cCI6MjEwNzc1ODk1NX0.Cxq09E9Kz_Ese0fFaS-1GyAbKqH7ZrYFisGmn2F8CsY"

BOTS = [
    {"id": "bot1", "name": "Market Maker",   "folder": "bot1_market_maker",  "color": "#4CAF50"},
    {"id": "bot2", "name": "Momentum",        "folder": "bot2_momentum",       "color": "#2196F3"},
    {"id": "bot3", "name": "Sharp Copy",      "folder": "bot3_sharp_copy",     "color": "#9C27B0"},
    {"id": "bot4", "name": "News Trigger",    "folder": "bot4_news_trigger",   "color": "#FF9800"},
    {"id": "bot5", "name": "Value Scanner",   "folder": "bot5_value_scanner",  "color": "#F44336"},
]

def fetch_supabase_states() -> dict:
    """Fetch all bot rows from Supabase. Returns {bot_id: row}."""
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        return {}
    try:
        resp = _requests.get(
            f"{SUPABASE_URL}/rest/v1/bot_state?select=*",
            headers={
                "apikey":        SUPABASE_ANON_KEY,
                "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
            },
            timeout=8,
        )
        resp.raise_for_status()
        return {row["bot_id"]: row for row in resp.json()}
    except Exception:
        return {}

def load_local_state(folder: str) -> dict:
    """Fallback: read state.json from local filesystem."""
    path = os.path.join(BOTS_DIR, folder, "state.json")
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}

def time_ago(ts_str: str | None) -> str:
    if not ts_str:
        return "never"
    try:
        ts = datetime.fromisoformat(ts_str)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        diff = (datetime.now(timezone.utc) - ts).total_seconds()
        if diff < 60:
            return f"{int(diff)}s ago"
        if diff < 3600:
            return f"{int(diff/60)}m ago"
        return f"{int(diff/3600)}h ago"
    except Exception:
        return "?"

def get_dashboard_data() -> dict:
    sb_states = fetch_supabase_states()

    bots_data = []
    total_pl  = 0.0
    total_pos = 0

    for bot in BOTS:
        if bot["id"] in sb_states:
            row       = sb_states[bot["id"]]
            pl        = row.get("total_pl", 0.0) or 0.0
            positions = row.get("open_positions", []) or []
            last_run  = row.get("last_run")
        else:
            # Fallback to local state.json
            state    = load_local_state(bot["folder"])
            pl       = state.get("total_pl", 0.0)
            positions = state.get("positions") or state.get("open_orders", [])
            last_run  = state.get("last_run")

        total_pl  += pl
        total_pos += len(positions)

        pos_display = []
        for p in positions:
            pos_display.append({
                "market":  p.get("market_name", "?")[:35],
                "runner":  p.get("runner_name", p.get("back_runner", "?"))[:20],
                "side":    p.get("side", "?"),
                "price":   p.get("entry_price", p.get("back_price", p.get("price", "?"))),
                "stake":   p.get("stake", "?"),
                "entered": time_ago(p.get("entered_at", p.get("placed_at"))),
            })

        bots_data.append({
            **bot,
            "pl":        pl,
            "positions": pos_display,
            "last_run":  time_ago(last_run),
            "is_active": last_run is not None,
        })

    return {
        "bots":       bots_data,
        "total_pl":   total_pl,
        "total_pos":  total_pos,
        "updated_at": datetime.now(timezone.utc).strftime("%H:%M:%S UTC"),
        "source":     "Supabase" if sb_states else "Local",
    }

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="30">
<title>Betfair Trading Dashboard</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Segoe UI', sans-serif; background: #0f1117; color: #e0e0e0; min-height: 100vh; }

  .header {
    background: linear-gradient(135deg, #1a1f2e 0%, #16213e 100%);
    padding: 20px 30px;
    border-bottom: 1px solid #2a2f3e;
    display: flex; justify-content: space-between; align-items: center;
  }
  .header h1 { font-size: 1.5rem; color: #fff; }
  .header h1 span { color: #4fc3f7; }
  .header .meta { font-size: 0.8rem; color: #888; }

  .summary {
    display: grid; grid-template-columns: repeat(3, 1fr);
    gap: 16px; padding: 20px 30px;
  }
  .summary-card {
    background: #1a1f2e; border-radius: 10px; padding: 20px;
    border: 1px solid #2a2f3e; text-align: center;
  }
  .summary-card .label { font-size: 0.75rem; color: #888; text-transform: uppercase; letter-spacing: 1px; }
  .summary-card .value { font-size: 2rem; font-weight: bold; margin-top: 6px; }
  .positive { color: #4CAF50; }
  .negative { color: #F44336; }
  .neutral  { color: #4fc3f7; }

  .bots-grid {
    display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
    gap: 16px; padding: 0 30px 30px;
  }
  .bot-card {
    background: #1a1f2e; border-radius: 10px;
    border: 1px solid #2a2f3e; overflow: hidden;
  }
  .bot-header {
    padding: 14px 18px;
    display: flex; justify-content: space-between; align-items: center;
    border-bottom: 1px solid #2a2f3e;
  }
  .bot-title { display: flex; align-items: center; gap: 10px; }
  .bot-dot { width: 10px; height: 10px; border-radius: 50%; }
  .bot-name { font-weight: 600; font-size: 0.95rem; }
  .bot-pl { font-size: 1.1rem; font-weight: bold; }

  .bot-meta { padding: 10px 18px; display: flex; gap: 20px; border-bottom: 1px solid #1e2330; }
  .bot-meta span { font-size: 0.75rem; color: #888; }
  .bot-meta strong { color: #ccc; }

  .positions { padding: 10px 18px 14px; }
  .positions-title { font-size: 0.7rem; color: #666; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 8px; }
  .position-row {
    display: grid; grid-template-columns: 1fr auto auto auto;
    gap: 6px; align-items: center;
    padding: 7px 0; border-bottom: 1px solid #1e2330; font-size: 0.82rem;
  }
  .position-row:last-child { border-bottom: none; }
  .pos-market { color: #ccc; }
  .pos-runner { color: #999; font-size: 0.75rem; }
  .side-back { color: #4CAF50; font-weight: bold; font-size: 0.75rem; }
  .side-lay  { color: #F44336; font-weight: bold; font-size: 0.75rem; }
  .pos-price { color: #4fc3f7; font-size: 0.8rem; }
  .pos-time  { color: #555; font-size: 0.72rem; }
  .no-positions { color: #444; font-size: 0.8rem; font-style: italic; padding: 4px 0; }

  .footer {
    text-align: center; padding: 20px; color: #333; font-size: 0.75rem;
    border-top: 1px solid #1e2330;
  }
  @media (max-width: 600px) {
    .summary { grid-template-columns: 1fr; }
    .bots-grid { grid-template-columns: 1fr; padding: 0 16px 20px; }
    .header, .summary { padding-left: 16px; padding-right: 16px; }
  }
</style>
</head>
<body>

<div class="header">
  <h1>&#9889; <span>Betfair</span> Trading Bots</h1>
  <div class="meta">Auto-refresh every 30s &bull; {{ data.updated_at }} &bull; {{ data.source }}</div>
</div>

<div class="summary">
  <div class="summary-card">
    <div class="label">Total P&amp;L</div>
    <div class="value {% if data.total_pl >= 0 %}positive{% else %}negative{% endif %}">
      £{{ "%.2f"|format(data.total_pl) }}
    </div>
  </div>
  <div class="summary-card">
    <div class="label">Open Positions</div>
    <div class="value neutral">{{ data.total_pos }}</div>
  </div>
  <div class="summary-card">
    <div class="label">Active Bots</div>
    <div class="value neutral">{{ data.bots|selectattr("is_active")|list|length }} / 5</div>
  </div>
</div>

<div class="bots-grid">
{% for bot in data.bots %}
  <div class="bot-card">
    <div class="bot-header">
      <div class="bot-title">
        <div class="bot-dot" style="background:{{ bot.color }}; {% if bot.is_active %}box-shadow:0 0 6px {{ bot.color }};{% endif %}"></div>
        <span class="bot-name">{{ bot.name }}</span>
      </div>
      <span class="bot-pl {% if bot.pl >= 0 %}positive{% else %}negative{% endif %}">
        £{{ "%.2f"|format(bot.pl) }}
      </span>
    </div>
    <div class="bot-meta">
      <span>Last run: <strong>{{ bot.last_run }}</strong></span>
      <span>Positions: <strong>{{ bot.positions|length }}</strong></span>
    </div>
    <div class="positions">
      {% if bot.positions %}
        <div class="positions-title">Open Positions</div>
        {% for p in bot.positions %}
        <div class="position-row">
          <div>
            <div class="pos-market">{{ p.market }}</div>
            <div class="pos-runner">{{ p.runner }}</div>
          </div>
          <span class="{% if p.side == 'BACK' %}side-back{% else %}side-lay{% endif %}">{{ p.side }}</span>
          <span class="pos-price">@ {{ p.price }}</span>
          <span class="pos-time">{{ p.entered }}</span>
        </div>
        {% endfor %}
      {% else %}
        <div class="no-positions">No open positions</div>
      {% endif %}
    </div>
  </div>
{% endfor %}
</div>

<div class="footer">madlabz trading system &bull; Betfair Exchange &bull; powered by Supabase</div>

</body>
</html>
"""

@app.route("/")
def index():
    data = get_dashboard_data()
    return render_template_string(HTML_TEMPLATE, data=data)

@app.route("/api/data")
def api_data():
    from flask import jsonify
    return jsonify(get_dashboard_data())

@app.route("/debug")
def debug():
    from flask import jsonify
    sb = fetch_supabase_states()
    return jsonify({
        "supabase_url_set": bool(SUPABASE_URL),
        "anon_key_set": bool(SUPABASE_ANON_KEY),
        "supabase_rows": len(sb),
        "bot_ids": list(sb.keys()),
        "sample": {k: {"pl": v.get("total_pl"), "last_run": v.get("last_run")} for k, v in sb.items()},
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=False)
