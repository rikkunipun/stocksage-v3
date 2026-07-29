import asyncio, json, logging, os, sys, traceback
from datetime import datetime
import pytz
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

IST = pytz.timezone("Asia/Kolkata")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("run_briefing")

HEARTBEAT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "tracker_heartbeat.json")


def _tracker_staleness_warning() -> str:
    """Return an HTML warning line if the outcome tracker hasn't run recently."""
    try:
        from nse_calendar import get_trading_days_elapsed
        if not os.path.exists(HEARTBEAT_PATH):
            return ("\n⚠️ <i>Outcome tracker has never run — "
                    "signal performance is not being measured.</i>")
        with open(HEARTBEAT_PATH) as f:
            hb = json.load(f)
        last = datetime.fromisoformat(hb["last_run"]).date()
        if get_trading_days_elapsed(last, datetime.now(IST).date()) >= 2:
            return (f"\n⚠️ <i>Outcome tracker last ran {last} — "
                    f"check the tracking cron on the server.</i>")
    except Exception as e:
        log.warning(f"Heartbeat check failed: {e}")
    return ""


async def main(dry_run=False):
    from portfolio import fetch_portfolio
    from news_feed import get_news
    from market_data import get_market_news
    from technicals import get_all_technicals
    from analyst import analyze_portfolio_v2
    from bot import build_signal_message
    from briefing_sender import send_message_http, send_error_alert

    log.info("=== StockSage Manual Briefing ===")
    try:
        holdings   = fetch_portfolio()
        news       = get_market_news() or get_news()   # Marketaux primary, GDELT/NewsAPI fallback
        technicals = get_all_technicals([h["symbol"] for h in holdings])
        result     = analyze_portfolio_v2(holdings, news, technicals, event_type="morning_briefing")
        message    = build_signal_message(result, technicals) + _tracker_staleness_warning()
    except Exception as e:
        log.error("Pipeline failed:\n" + traceback.format_exc())
        if not dry_run:
            try:
                await send_error_alert("Morning briefing", e)
            except Exception:
                log.error("Also failed to send the Telegram failure alert.")
        return

    if dry_run:
        print("\n" + "="*60)
        print(message)
        print("="*60)
    else:
        success = await send_message_http(message)
        log.info("✅ Sent!" if success else "❌ Failed to send")

if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    asyncio.run(main(dry_run=dry))
