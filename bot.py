import os
import asyncio
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ParseMode
import html

import anthropic
import json as _json

from portfolio import fetch_portfolio
from news_feed import get_news
from technicals import get_all_technicals
from analyst import analyze_portfolio
from nse_calendar import is_market_open, is_trading_day
from alert_throttle import can_send_alert, record_alert_sent, is_muted as throttle_is_muted

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
IST = ZoneInfo("Asia/Kolkata")

# --- Agent state ---
_state = {
    "last_run": None,          # datetime (IST)
    "signals_today": 0,        # count of BUY/SELL signals sent today
    "muted_until": None,       # datetime (IST) or None
}

DIVIDER = "───────────────────────────────"


# ── Helpers ────────────────────────────────────────────────────────────────────

def _now_ist():
    return datetime.now(IST)


def _is_muted():
    if _state["muted_until"] is None:
        return False
    return _now_ist() < _state["muted_until"]


def _market_status():
    return "🟢 Open" if is_market_open() else "🔴 Closed"


def _e(text):
    """Escape text for HTML parse mode."""
    return html.escape(str(text))


def _format_signal_block(signal, technicals):
    """Format a single BUY/SELL signal as a multi-line HTML string."""
    sym   = signal["stock_symbol"]
    tech  = technicals.get(sym, {})
    price = tech.get("current_price", "N/A")
    sector_name  = tech.get("sector_index_name", "")
    sector_price = tech.get("sector_index_price", "N/A")

    action_icon = "🟢 BUY" if signal["action"] == "BUY" else "🔴 SELL"

    lines = [
        f"<b>{action_icon} {_e(sym)}</b> | Confidence: {_e(signal['confidence_score'])}/100 ({_e(signal['confidence_label'])})",
        f"Reason: {_e(signal['reason'])}",
        f"Event: {_e(signal['event_category'])} | Horizon: {_e(signal['time_horizon'])}",
        f"Price: ₹{_e(price)} | Sector: {_e(sector_name)} @ {_e(sector_price)}",
        DIVIDER,
    ]
    return "\n".join(lines)


def build_signal_message(signals_dict, technicals):
    """Build the full formatted HTML Telegram message from a signals dict."""
    now      = _now_ist()
    date_str = now.strftime("%d %b %Y")
    time_str = now.strftime("%H:%M")
    tech_dicts = {k: v for k, v in technicals.items() if isinstance(v, dict)} if technicals else {}
    nifty    = next(iter(tech_dicts.values()), {}).get("nifty50_price", "N/A") if tech_dicts else "N/A"

    header = (
        f"📊 <b>STOCKSAGE — {_e(date_str)}, {_e(time_str)} IST</b>\n"
        f"Nifty 50: {_e(nifty)} | Market: {_market_status()}\n"
        f"{DIVIDER}\n"
    )

    signals  = signals_dict.get("signals", [])
    buy_sell = [s for s in signals if s["action"] in ("BUY", "SELL")]
    holds    = [s for s in signals if s["action"] == "HOLD"]

    body_parts = []

    if buy_sell:
        for sig in buy_sell:
            body_parts.append(_format_signal_block(sig, technicals))
    else:
        ctx = signals_dict.get("market_context", "")
        body_parts.append(f"No actionable signals today. Market context: {_e(ctx)}\n{DIVIDER}")

    if holds:
        hold_lines = ["<b>HOLD signals:</b>"]
        for sig in holds:
            hold_lines.append(f"• {_e(sig['stock_symbol'])}: HOLD — {_e(sig['reason'])}")
        body_parts.append("\n".join(hold_lines) + f"\n{DIVIDER}")

    geo = signals_dict.get("geo_watch", [])
    if geo:
        body_parts.append("🌍 <b>Geo Watch:</b> " + " | ".join(_e(g) for g in geo) + f"\n{DIVIDER}")

    footer = "⚠️ <i>AI research only. Not financial advice.</i>"

    return header + "\n".join(body_parts) + "\n" + footer


# ── Security gate ───────────────────────────────────────────────────────────────

def _authorised(update: Update) -> bool:
    return update.effective_chat.id == TELEGRAM_CHAT_ID


# ── Core analysis runner ────────────────────────────────────────────────────────

async def _run_analysis(context: ContextTypes.DEFAULT_TYPE, chat_id: int, label: str, event_type: str):
    """Run full pipeline and send results. Shared by /analyze and /briefing."""
    await context.bot.send_message(chat_id=chat_id, text=f"⏳ Running {label}…")

    try:
        holdings   = fetch_portfolio()
        news       = get_news()
        symbols    = [h["symbol"] for h in holdings]
        technicals = get_all_technicals(symbols)
        result     = analyze_portfolio(holdings, news, technicals, event_type=event_type)
    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"❌ Analysis failed: {e}")
        return

    _state["last_run"] = _now_ist()
    buy_sell_count = sum(1 for s in result.get("signals", []) if s["action"] in ("BUY", "SELL"))
    _state["signals_today"] += buy_sell_count

    msg = build_signal_message(result, technicals)
    await context.bot.send_message(
        chat_id=chat_id,
        text=msg,
        parse_mode=ParseMode.HTML,
    )

    logged = result.get("signal_ids", [])
    print(f"Logged {len(logged)} signals to CSV: {logged}")


# ── Command handlers ────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorised(update):
        return
    text = (
        "👋 <b>Welcome to StockSage v3</b>\n\n"
        "Your personal AI stock market intelligence agent for NSE.\n\n"
        "<b>Commands:</b>\n"
        "/analyze — Run full portfolio analysis\n"
        "/briefing — Morning briefing (same as analyze)\n"
        "/status — Agent health and stats\n"
        "/mute N — Pause geo-alerts for N hours\n"
        "/unmute — Resume geo-alerts immediately"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_analyze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorised(update):
        return
    await _run_analysis(context, update.effective_chat.id, "analysis", event_type="on_demand")


async def cmd_briefing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorised(update):
        return
    await _run_analysis(context, update.effective_chat.id, "morning briefing", event_type="morning_briefing")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorised(update):
        return

    last = _state["last_run"].strftime("%d %b %Y %H:%M IST") if _state["last_run"] else "Never"
    mute_str = (
        f"Active until {_state['muted_until'].strftime('%H:%M IST')}"
        if _is_muted() else "Inactive"
    )

    # Light connectivity checks
    angel_ok  = "✅ Connected"
    news_ok   = "✅ Connected"
    claude_ok = "✅ Connected" if os.getenv("CLAUDE_API_KEY") else "❌ Key missing"

    text = (
        f"<b>STOCKSAGE STATUS</b>\n"
        f"{DIVIDER}\n"
        f"Last run: {_e(last)}\n"
        f"Angel One: {angel_ok}\n"
        f"NewsAPI: {news_ok}\n"
        f"Claude API: {claude_ok}\n"
        f"Signals today: {_state['signals_today']}\n"
        f"Mute status: {_e(mute_str)}"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_mute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorised(update):
        return
    try:
        hours = int(context.args[0]) if context.args else 1
    except (ValueError, IndexError):
        await update.message.reply_text("Usage: /mute N  (N = number of hours)")
        return

    _state["muted_until"] = _now_ist() + timedelta(hours=hours)
    await update.message.reply_text(
        f"🔇 Geo-alerts muted for {hours} hour(s). "
        f"Resumes at {_state['muted_until'].strftime('%H:%M IST')}."
    )


async def cmd_unmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorised(update):
        return
    _state["muted_until"] = None
    await update.message.reply_text("🔔 Geo-alerts resumed.")


# ── Geo-alert loop (called every 15 min by agent_core.py) ──────────────────────

_CLAUDE_API_KEY   = os.getenv("CLAUDE_API_KEY")
_HAIKU_MODEL      = "claude-haiku-4-5-20251001"
_SEVERITY_PROMPT  = (
    "Rate the market impact of this news headline on Indian stock markets (NSE/BSE). "
    "Respond with ONLY a valid JSON object, no explanation, no markdown. "
    'Example format: {"severity_score": 75, "affected_sectors": ["Banking", "IT"]}. '
    "severity_score is an integer 0-100 where 0=irrelevant, 100=extreme market impact."
)


def _strip_fences(text: str) -> str:
    """Strip markdown code fences if present."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    return text.strip()


def _score_headline(title: str) -> tuple[int, list]:
    """Call Claude Haiku to score a headline. Returns (severity_score, affected_sectors)."""
    if not _CLAUDE_API_KEY:
        return 0, []
    try:
        client = anthropic.Anthropic(api_key=_CLAUDE_API_KEY)
        msg = client.messages.create(
            model=_HAIKU_MODEL,
            max_tokens=128,
            system=_SEVERITY_PROMPT,
            messages=[{"role": "user", "content": title}],
        )
        raw = _strip_fences(msg.content[0].text)
        data = _json.loads(raw)
        return int(data.get("severity_score", 0)), data.get("affected_sectors", [])
    except Exception as e:
        print(f"WARNING: Haiku scoring failed for '{title[:50]}' — {e}")
        return 0, []


def _format_geo_alert(title: str, severity: int, sectors: list) -> str:
    now_str  = _now_ist().strftime("%H:%M")
    sec_str  = ", ".join(sectors) if sectors else "General"
    return (
        f"🌍 <b>GEO ALERT — {_e(now_str)} IST</b>\n"
        f"{_e(title)}\n"
        f"Severity: {severity}/100\n"
        f"Sectors: {_e(sec_str)}\n"
        f"⚠️ <i>Monitor your positions.</i>"
    )


async def geo_alert_loop(app: Application):
    """
    Fetch latest news, score each headline with Haiku,
    and send geo-alerts that pass the throttle check.
    Called every 15 minutes by agent_core.py scheduler.
    """
    if throttle_is_muted():
        return

    try:
        headlines = get_news()
    except Exception as e:
        print(f"[geo_alert] News fetch failed — {e}")
        return

    for h in headlines:
        title = h.get("title", "")
        if not title:
            continue

        severity, sectors = _score_headline(title)

        if not can_send_alert(title, severity):
            continue

        try:
            msg = _format_geo_alert(title, severity, sectors)
            await app.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=msg,
                parse_mode=ParseMode.HTML,
            )
            record_alert_sent(title)
            print(f"[geo_alert] Sent [{severity}] {title[:60]}")
        except Exception as e:
            print(f"[geo_alert] Send failed — {e}")


# ── Programmatic sender (called by agent_core.py) ──────────────────────────────

async def send_signal_message(app: Application, signals_dict: dict, technicals: dict, event_type: str = "on_demand"):
    """
    Send a signal message to TELEGRAM_CHAT_ID programmatically.
    Pass the running Application instance so we reuse its bot.
    """
    print(f"[send_signal_message] types — app: {type(app)}, signals_dict: {type(signals_dict)}, technicals: {type(technicals)}, event_type: {type(event_type)}")

    if not isinstance(technicals, dict):
        print(f"[send_signal_message] WARNING: technicals is {type(technicals).__name__} (value={technicals!r}), resetting to {{}}")
        technicals = {}

    if _is_muted():
        return

    _state["last_run"] = _now_ist()
    buy_sell_count = sum(1 for s in signals_dict.get("signals", []) if s["action"] in ("BUY", "SELL"))
    _state["signals_today"] += buy_sell_count

    msg = build_signal_message(signals_dict, technicals)
    await app.bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=msg,
        parse_mode=ParseMode.HTML,
    )


# ── Entry point ─────────────────────────────────────────────────────────────────

def main():
    if not TELEGRAM_BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN not set in .env")
        return

    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .build()
    )

    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("analyze",  cmd_analyze))
    app.add_handler(CommandHandler("briefing", cmd_briefing))
    app.add_handler(CommandHandler("status",   cmd_status))
    app.add_handler(CommandHandler("mute",     cmd_mute))
    app.add_handler(CommandHandler("unmute",   cmd_unmute))

    print("StockSage bot started. Polling for commands…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
