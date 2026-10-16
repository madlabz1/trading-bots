"""
Shared helper — push bot state to Supabase via REST API.
Non-fatal: if the push fails the bot continues normally.
"""

import logging
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)


def push_state(url: str, anon_key: str, bot_id: str, bot_name: str, state: dict) -> None:
    if not url or not anon_key:
        return
    try:
        positions = state.get("positions") or state.get("open_orders", [])
        payload = {
            "bot_id":         bot_id,
            "bot_name":       bot_name,
            "total_pl":       round(float(state.get("total_pl", 0.0)), 4),
            "open_positions": positions,
            "last_run":       state.get("last_run"),
            "updated_at":     datetime.now(timezone.utc).isoformat(),
        }
        headers = {
            "apikey":        anon_key,
            "Authorization": f"Bearer {anon_key}",
            "Content-Type":  "application/json",
            "Prefer":        "resolution=merge-duplicates",
        }
        resp = requests.post(
            f"{url}/rest/v1/bot_state",
            json=payload,
            headers=headers,
            timeout=8,
        )
        resp.raise_for_status()
    except Exception as exc:
        log.warning(f"Supabase push failed ({bot_id}): {exc}")
