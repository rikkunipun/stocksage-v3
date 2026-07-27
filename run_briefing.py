import asyncio, logging, os, sys, traceback
from datetime import datetime
import pytz
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

IST = pytz.timezone("Asia/Kolkata")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("run_briefing")

async def main(dry_run=False):
    from portfolio import fetch_portfolio
    from news_feed import get_news
    from technicals import get_all_technicals
    from analyst import analyze_portfolio
    from bot import build_signal_message
    from briefing_sender import send_message_http

    log.info("=== StockSage Manual Briefing ===")
    try:
        holdings   = fetch_portfolio()
        news       = get_news()
        technicals = get_all_technicals([h["symbol"] for h in holdings])
        result     = analyze_portfolio(holdings, news, technicals, event_type="morning_briefing")
        message    = build_signal_message(result, technicals)
    except Exception:
        log.error("Pipeline failed:\n" + traceback.format_exc())
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
