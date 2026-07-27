"""
Standalone outcome-tracking entrypoint.

Run daily via cron AFTER NSE EOD data publishes (18:30 IST is safe;
yfinance daily bars usually land 17:30-18:00 IST):

    30 18 * * 1-5  cd /path/to/stocksage-v3 && python3 run_tracking.py >> logs/tracking.log 2>&1

(If the server clock is UTC, 18:30 IST = 13:00 UTC → `0 13 * * 1-5`.)

Fills 1d/3d/5d/10d/20d outcome prices for pending signals, writes the
tracker heartbeat, and sends a Telegram alert if the run crashes.
The first run after migration backfills all historical signals.
"""
import asyncio
import logging
import os
import sys
import traceback

from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("run_tracking")


async def main():
    from briefing_sender import send_error_alert
    try:
        from outcome_tracker import run_tracking
        summary = run_tracking()
        log.info(f"Tracking complete: {summary}")
    except Exception as e:
        log.error("Outcome tracking failed:\n" + traceback.format_exc())
        try:
            await send_error_alert("Outcome tracking", e)
        except Exception:
            log.error("Also failed to send the Telegram failure alert.")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
