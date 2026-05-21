import asyncio, argparse, logging, os, sys, traceback
from datetime import datetime
import pytz

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from agent_core import run_analysis
from bot import build_signal_message
from briefing_sender import send_message_http

IST = pytz.timezone("Asia/Kolkata")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
log = logging.getLogger("run_briefing")

async def run_morning_briefing(dry_run=False, debug=False):
    ist_now = datetime.now(IST)
    log.info(f"=== StockSage v3 Manual Briefing === {ist_now.strftime('%Y-%m-%d %H:%M:%S IST')}")
    try:
        result = await run_analysis(event_type="morning_briefing")
        signals_dict = result.get("signals", {})
        technicals = result.get("technicals", {})
    except Exception:
        log.error("run_analysis() failed:\n" + traceback.format_exc())
        if not dry_run:
            await send_message_http("⚠️ <b>StockSage Morning Briefing Failed</b>\n\nAnalysis pipeline crashed. Check server logs.")
        return
    try:
        message_text = build_signal_message(signals_dict, technicals)
    except Exception:
        log.error("build_signal_message() failed:\n" + traceback.format_exc())
        if not dry_run:
            await send_message_http("⚠️ <b>StockSage: Signals generated but formatting failed</b>")
        return
    if dry_run:
        print("\n" + "="*60)
        print("[DRY RUN] Message that would be sent:")
        print("="*60)
        print(message_text)
        return
    success = await send_message_http(message_text)
    if success:
        log.info("✅ Morning briefing delivered.")
    else:
        log.error("❌ Telegram send failed.")
        with open("briefing_fallback.txt", "w") as f:
            f.write(f"FAILED — {ist_now.isoformat()}\n\n{message_text}")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
    asyncio.run(run_morning_briefing(dry_run=args.dry_run, debug=args.debug))

if __name__ == "__main__":
    main()
