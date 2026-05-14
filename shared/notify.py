"""
Shared Telegram Notification Module
=====================================
Used by all 3 bots to send alerts to your Telegram.
Fires-and-forgets — never crashes the bot if Telegram is down.
"""

import requests

TELEGRAM_TOKEN   = "8651053016:AAEnizQe8ALLy73gS6gkRsiMIyhyUsy0hEA"
TELEGRAM_CHAT_ID = "6225618832"
BASE_URL         = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"


def send(message: str) -> None:
    """Sends a Telegram message. Silent on failure — never crashes the bot."""
    try:
        requests.post(
            BASE_URL,
            data={
                "chat_id":    TELEGRAM_CHAT_ID,
                "text":       message,
                "parse_mode": "Markdown",
            },
            timeout=5,
        )
    except Exception:
        pass  # Telegram being down should never stop the trading bot


# ── Bot 1 — Trailing Stop ──────────────────────────────────────────────────────

def b1_opened(symbol: str, qty: int, price: float, floor: float) -> None:
    send(
        f"🟢 *Bot 1 — Position Opened*\n"
        f"Bought `{qty}x {symbol}` @ *${price:.2f}*\n"
        f"🛡 Stop-loss floor: *${floor:.2f}*"
    )

def b1_floor_raised(symbol: str, old_floor: float, new_floor: float, price: float) -> None:
    send(
        f"📈 *Bot 1 — Floor Trailed Up*\n"
        f"`{symbol}` @ ${price:.2f}\n"
        f"Floor: ${old_floor:.2f} → *${new_floor:.2f}*"
    )

def b1_ladder_buy(symbol: str, qty: int, price: float, drop_pct: float) -> None:
    send(
        f"🪜 *Bot 1 — Ladder Buy*\n"
        f"Dip of -{drop_pct:.0f}% hit! Bought `{qty}x {symbol}` @ *${price:.2f}*"
    )

def b1_sold(symbol: str, qty: int, price: float, entry: float) -> None:
    pl     = (price - entry) * qty
    pl_pct = ((price - entry) / entry) * 100
    emoji  = "🟢" if pl >= 0 else "🔴"
    send(
        f"{emoji} *Bot 1 — Position Closed*\n"
        f"Sold `{qty}x {symbol}` @ *${price:.2f}*\n"
        f"P&L: *{'+'if pl>=0 else ''}{pl:.2f}* ({pl_pct:+.2f}%)"
    )


# ── Bot 2 — Copy Trading ───────────────────────────────────────────────────────

def b2_politician_selected(name: str, score: float) -> None:
    send(
        f"🏛 *Bot 2 — Politician Selected*\n"
        f"Now copying: *{name}*\n"
        f"Score: {score:.1f}"
    )

def b2_trade_copied(politician: str, action: str, symbol: str, qty: int, price: float) -> None:
    emoji = "🟢" if action.lower() == "buy" else "🔴"
    send(
        f"{emoji} *Bot 2 — Trade Copied*\n"
        f"*{politician}* {action.upper()}s `{symbol}`\n"
        f"Copied: `{qty} shares` @ *${price:.2f}*"
    )

def b2_skipped(symbol: str, reason: str) -> None:
    send(f"⚠️ *Bot 2 — Skipped* `{symbol}`\n_{reason}_")


# ── Bot 3 — Wheel Strategy ─────────────────────────────────────────────────────

def b3_put_sold(contract: str, strike: float, expiry: str, premium: float) -> None:
    send(
        f"🎡 *Bot 3 — Put Sold* (Stage 1)\n"
        f"Contract: `{contract}`\n"
        f"Strike: *${strike:.2f}* | Exp: {expiry}\n"
        f"Premium collected: *${premium:.2f}*"
    )

def b3_put_expired(premium: float, total: float, cycles: int) -> None:
    send(
        f"✅ *Bot 3 — Put Expired Worthless*\n"
        f"Kept premium: *${premium:.2f}*\n"
        f"Total collected: *${total:.2f}* | Cycles: {cycles}"
    )

def b3_assigned(symbol: str, strike: float, cost_basis: float) -> None:
    send(
        f"📦 *Bot 3 — Assigned!*\n"
        f"Got 100 `{symbol}` shares @ *${strike:.2f}*\n"
        f"Effective cost basis: *${cost_basis:.2f}*/share\n"
        f"Moving to Stage 2 → selling covered call..."
    )

def b3_call_sold(contract: str, strike: float, expiry: str, premium: float) -> None:
    send(
        f"📞 *Bot 3 — Covered Call Sold* (Stage 2)\n"
        f"Contract: `{contract}`\n"
        f"Strike: *${strike:.2f}* | Exp: {expiry}\n"
        f"Premium collected: *${premium:.2f}*"
    )

def b3_call_expired(premium: float, total: float) -> None:
    send(
        f"✅ *Bot 3 — Call Expired Worthless*\n"
        f"Kept premium: *${premium:.2f}*\n"
        f"Total collected: *${total:.2f}*\n"
        f"Selling another call next run..."
    )

def b3_called_away(symbol: str, strike: float, total: float, cycles: int) -> None:
    send(
        f"🏆 *Bot 3 — Wheel Cycle Complete!*\n"
        f"`{symbol}` called away @ *${strike:.2f}*\n"
        f"Total premiums collected: *${total:.2f}*\n"
        f"Cycles completed: {cycles} | Restarting wheel..."
    )

def b3_early_close(contract: str, pl: float, total: float) -> None:
    send(
        f"⚡ *Bot 3 — Early Close (50% profit)*\n"
        f"Closed `{contract}`\n"
        f"Locked in: *${pl:.2f}* | Total: *${total:.2f}*"
    )
