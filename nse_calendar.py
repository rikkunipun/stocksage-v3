import json
import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

HOLIDAYS_FILE = os.path.join(os.path.dirname(__file__), "data", "nse_holidays.json")
IST = ZoneInfo("Asia/Kolkata")
MARKET_OPEN  = (9, 15)
MARKET_CLOSE = (15, 30)


def _load_holidays():
    """Load holiday dates from data/nse_holidays.json. Returns a set of date objects."""
    if not os.path.exists(HOLIDAYS_FILE):
        print("WARNING: data/nse_holidays.json not found — using empty holiday list")
        return set()
    try:
        with open(HOLIDAYS_FILE) as f:
            data = json.load(f)
        return {date.fromisoformat(d) for d in data.get("holidays", [])}
    except Exception as e:
        print(f"WARNING: Could not load nse_holidays.json — {e}")
        return set()


# Load once at import time
_HOLIDAYS = _load_holidays()


def is_trading_day(d):
    """Return True if d is a weekday and not an NSE holiday."""
    if isinstance(d, datetime):
        d = d.date()
    return d.weekday() < 5 and d not in _HOLIDAYS


def next_trading_day(d):
    """Return the next trading day strictly after d."""
    if isinstance(d, datetime):
        d = d.date()
    candidate = d + timedelta(days=1)
    while not is_trading_day(candidate):
        candidate += timedelta(days=1)
    return candidate


def trading_days_from(d, n):
    """
    Return the date that is exactly n trading days after d.
    n must be >= 1.
    """
    if isinstance(d, datetime):
        d = d.date()
    candidate = d
    steps = 0
    while steps < n:
        candidate += timedelta(days=1)
        if is_trading_day(candidate):
            steps += 1
    return candidate


def get_trading_days_elapsed(start_date, end_date):
    """
    Count trading days in the half-open interval [start_date, end_date).
    Dates may be date or datetime objects.
    """
    if isinstance(start_date, datetime):
        start_date = start_date.date()
    if isinstance(end_date, datetime):
        end_date = end_date.date()

    if end_date <= start_date:
        return 0

    count = 0
    current = start_date
    while current < end_date:
        if is_trading_day(current):
            count += 1
        current += timedelta(days=1)
    return count


def is_market_open():
    """Return True if the current IST time falls within market hours on a trading day."""
    now_ist = datetime.now(IST)
    if not is_trading_day(now_ist.date()):
        return False
    t = (now_ist.hour, now_ist.minute)
    return MARKET_OPEN <= t <= MARKET_CLOSE


def main():
    today = date.today()
    now_ist = datetime.now(IST)

    print(f"\nDate/time (IST) : {now_ist.strftime('%Y-%m-%d %H:%M')}")
    print(f"Is trading day  : {is_trading_day(today)}")
    print(f"Market open now : {is_market_open()}")
    print(f"Next trading day: {next_trading_day(today)}")
    print(f"3 trading days  : {trading_days_from(today, 3)}")
    print(f"5 trading days  : {trading_days_from(today, 5)}")


if __name__ == "__main__":
    main()
