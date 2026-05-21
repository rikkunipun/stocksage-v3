import asyncio
import os
import sys
import json
import warnings
from datetime import datetime, time as dt_time, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

# PTB v20 changed weekday alignment; suppress the advisory warning (our 0=Mon is correct)
warnings.filterwarnings("ignore", message="Prior to v20.0 the `days` parameter")

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from telegram import Update
from telegram.ext import Application, CommandHandler, CallbackContext
from telegram.constants import ParseMode

load_dotenv()

IST = ZoneInfo("Asia/Kolkata")

REQUIRED_ENV = [
    "ANGEL_API_KEY", "ANGEL_CLIENT_CODE", "ANGEL_CLIENT_PASSWORD",
    "TOTP_SECRET", "NEWSAPI_KEY", "CLAUDE_API_KEY",
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
]

# ── Startup validation ─────────────────────────────────────────────────────────

def _check(label: str, ok: bool, detail: str = ""):
    icon = "✅" if ok else "❌"
    print(f"  {icon} {label}{' — ' + detail if detail else ''}")
    return ok


def validate_startup() -> bool:
    print("\n── StockSage v3 Startup Check ──────────────────────────")
    all_ok = True

    # 1. Environment variables
    missing = [k for k in REQUIRED_ENV if not os.getenv(k)]
    if missing:
        _check("Environment", False, f"Missing keys: {', '.join(missing)}")
        all_ok = False
    else:
        _check("Environment", True, "all keys present")

    # 2. Angel One
    try:
        from portfolio import fetch_portfolio
        holdings = fetch_portfolio()
        all_ok &= _check("Angel One", True, f"connected ({len(holdings)} holdings)")
    except Exception as e:
        all_ok &= _check("Angel One", False, str(e))
        holdings = []

    # 3. NewsAPI
    try:
        from news_feed import fetch_newsapi
        articles = fetch_newsapi()
        all_ok &= _check("NewsAPI", True, "connected")
    except Exception as e:
        all_ok &= _check("NewsAPI", False, str(e))

    # 4. Claude API
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=os.getenv("CLAUDE_API_KEY"))
        client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            messages=[{"role": "user", "content": "ping"}],
        )
        all_ok &= _check("Claude API", True, "connected")
    except Exception as e:
        all_ok &= _check("Claude API", False, str(e))

    # 5. Signal log schema
    try:
        from signal_logger import get_all_signals
        df = get_all_signals()
        all_ok &= _check("Signal log", True, f"{len(df)} existing signal(s)")
    except RuntimeError as e:
        all_ok &= _check("Signal log", False, str(e))

    # 6. NSE calendar
    try:
        data_path = os.path.join(os.path.dirname(__file__), "data", "nse_holidays.json")
        with open(data_path) as f:
            holidays = json.load(f).get("holidays", [])
        all_ok &= _check("NSE calendar", True, f"{len(holidays)} holidays loaded")
    except Exception as e:
        all_ok &= _check("NSE calendar", False, str(e))

    print("────────────────────────────────────────────────────────")

    if all_ok:
        print("🚀 StockSage v3 starting...\n")
    else:
        print("⚠️  Startup check failed. Fix errors above before running.\n")

    return all_ok


# ── Async job: morning briefing ────────────────────────────────────────────────

async def job_morning_briefing(context: CallbackContext):
    """Run full analysis at 8:30 AM IST Mon-Fri and send to Telegram."""
    from portfolio import fetch_portfolio
    from news_feed import get_news
    from technicals import get_all_technicals
    from analyst import analyze_portfolio
    from bot import build_signal_message, TELEGRAM_CHAT_ID as CHAT_ID
    from bot import _state as bot_state

    print("[morning_briefing] Running morning briefing...")
    chat_id = int(os.getenv("TELEGRAM_CHAT_ID", "0"))

    async def _run_briefing():
        await context.bot.send_message(chat_id=chat_id, text="⏳ Running morning briefing…")
        holdings   = fetch_portfolio()
        news       = get_news()
        technicals = get_all_technicals([h["symbol"] for h in holdings])
        result     = analyze_portfolio(holdings, news, technicals, event_type="morning_briefing")

        bot_state["last_run"] = datetime.now(IST)
        buy_sell = [s for s in result.get("signals", []) if s["action"] in ("BUY", "SELL")]
        bot_state["signals_today"] += len(buy_sell)

        msg = build_signal_message(result, technicals)
        await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.HTML)

        logged = result.get("signal_ids", [])
        print(f"[morning_briefing] Done. Logged {len(logged)} signal(s): {logged}")

    try:
        await _run_briefing()
    except Exception as e:
        print(f"[morning_briefing] ERROR: {e} — retrying in 2 minutes")
        await asyncio.sleep(120)
        try:
            await _run_briefing()
        except Exception as e2:
            print(f"[morning_briefing] ERROR (retry failed): {e2}")
            await context.bot.send_message(
                chat_id=chat_id,
                text="⚠️ Morning briefing failed today. Use /analyze for manual analysis.",
            )


# ── Async job: geo-alert loop ──────────────────────────────────────────────────

async def job_geo_alert(context: CallbackContext):
    """Score latest headlines and fire geo-alerts every 15 minutes."""
    from bot import geo_alert_loop
    await geo_alert_loop(context.application)


# ── Sync jobs (BackgroundScheduler) ───────────────────────────────────────────

def job_daily_backup():
    try:
        from signal_logger import create_daily_backup
        path = create_daily_backup()
        print(f"[backup] Daily backup created: {path}")
    except Exception as e:
        print(f"[backup] ERROR: {e}")


def job_outcome_tracking():
    try:
        from outcome_tracker import run_tracking
        summary = run_tracking()
        print(f"[tracker] Updated {summary.get('updated', 0)} signal(s): {summary.get('signals', [])}")
    except Exception as e:
        print(f"[tracker] ERROR: {e}")


def job_reset_hourly():
    try:
        from alert_throttle import reset_hourly_count
        reset_hourly_count()
    except Exception as e:
        print(f"[throttle] ERROR: Hourly reset failed — {e}")


def job_reset_daily():
    try:
        from alert_throttle import reset_daily_count
        reset_daily_count()
    except Exception as e:
        print(f"[throttle] ERROR: Daily reset failed — {e}")


def _start_background_scheduler() -> BackgroundScheduler:
    bg = BackgroundScheduler(timezone=IST)

    bg.add_job(job_daily_backup,      CronTrigger(hour=0,  minute=0,  timezone=IST), id="daily_backup",
               name="Daily CSV backup",                misfire_grace_time=300)
    bg.add_job(job_reset_daily,       CronTrigger(hour=0,  minute=5,  timezone=IST), id="reset_daily",
               name="Reset daily alert counter",       misfire_grace_time=300)
    bg.add_job(job_reset_hourly,      CronTrigger(minute=0,            timezone=IST), id="reset_hourly",
               name="Reset hourly alert counter",      misfire_grace_time=60)
    bg.add_job(job_outcome_tracking,  CronTrigger(hour=16, minute=0,  day_of_week="mon-fri", timezone=IST),
               id="outcome_tracking", name="4 PM outcome tracking", misfire_grace_time=300)

    bg.start()
    return bg


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    all_ok = validate_startup()
    if not all_ok:
        sys.exit(1)

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        print("ERROR: TELEGRAM_BOT_TOKEN not set")
        sys.exit(1)

    # Import bot handlers
    from bot import (
        cmd_start, cmd_analyze, cmd_briefing,
        cmd_status, cmd_mute, cmd_unmute,
    )

    # Build Application with JobQueue enabled
    app = (
        Application.builder()
        .token(token)
        .build()
    )

    # Register command handlers
    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("analyze",  cmd_analyze))
    app.add_handler(CommandHandler("briefing", cmd_briefing))
    app.add_handler(CommandHandler("status",   cmd_status))
    app.add_handler(CommandHandler("mute",     cmd_mute))
    app.add_handler(CommandHandler("unmute",   cmd_unmute))

    # Async scheduled jobs via telegram's built-in JobQueue
    app.job_queue.run_daily(
        callback=job_morning_briefing,
        time=dt_time(hour=8, minute=30, tzinfo=IST),
        days=(0, 1, 2, 3, 4),   # Mon–Fri
        name="morning_briefing",
    )
    app.job_queue.run_repeating(
        callback=job_geo_alert,
        interval=timedelta(minutes=15),
        first=timedelta(seconds=30),   # first run 30s after startup
        name="geo_alert_loop",
    )

    # Sync jobs in background thread (APScheduler BackgroundScheduler)
    bg_scheduler = _start_background_scheduler()

    print("Scheduled jobs:")
    print("  [morning_briefing]  08:30 IST Mon-Fri")
    print("  [geo_alert_loop]    every 15 minutes")
    print("  [outcome_tracking]  16:00 IST Mon-Fri")
    print("  [daily_backup]      00:00 IST daily")
    print("  [reset_daily]       00:05 IST daily")
    print("  [reset_hourly]      HH:00 every hour")
    print("\nStockSage v3 is running. Press Ctrl+C to stop.\n")

    try:
        app.run_polling(allowed_updates=Update.ALL_TYPES)
    except (KeyboardInterrupt, SystemExit):
        print("\nStockSage shutting down...")
    finally:
        if bg_scheduler.running:
            bg_scheduler.shutdown(wait=False)
            print("Scheduler stopped.")
        print("Done.")


if __name__ == "__main__":
    main()
