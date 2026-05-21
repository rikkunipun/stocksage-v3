import os
import sys
import pyotp
from SmartApi import SmartConnect
from dotenv import load_dotenv

load_dotenv()

ANGEL_API_KEY = os.getenv("ANGEL_API_KEY")
ANGEL_API_SECRET = os.getenv("ANGEL_API_SECRET")
ANGEL_CLIENT_CODE = os.getenv("ANGEL_CLIENT_CODE")
ANGEL_CLIENT_PASSWORD = os.getenv("ANGEL_CLIENT_PASSWORD")
TOTP_SECRET = os.getenv("TOTP_SECRET")


def login():
    """Login to Angel One SmartAPI and return an authenticated SmartConnect instance."""
    if not all([ANGEL_API_KEY, ANGEL_CLIENT_CODE, ANGEL_CLIENT_PASSWORD, TOTP_SECRET]):
        print("ERROR: Missing one or more Angel One credentials in .env")
        sys.exit(1)

    try:
        totp = pyotp.TOTP(TOTP_SECRET).now()
        api = SmartConnect(api_key=ANGEL_API_KEY)
        data = api.generateSession(ANGEL_CLIENT_CODE, ANGEL_CLIENT_PASSWORD, totp)

        if data.get("status") is False:
            print(f"ERROR: Login failed — {data.get('message', 'unknown error')}")
            sys.exit(1)

        return api

    except Exception as e:
        print(f"ERROR: Login failed — {e}")
        sys.exit(1)


def get_holdings(api):
    """Fetch portfolio holdings and return a clean list of dicts."""
    try:
        response = api.holding()
    except Exception as e:
        print(f"ERROR: Failed to fetch holdings — {e}")
        sys.exit(1)

    if not response or response.get("status") is False:
        print(f"ERROR: Could not retrieve holdings — {response.get('message', 'unknown error')}")
        sys.exit(1)

    raw_holdings = response.get("data") or []
    holdings = []

    for item in raw_holdings:
        try:
            symbol = item.get("tradingsymbol", "")
            quantity = int(item.get("quantity", 0))
            avg_buy_price = float(item.get("averageprice", 0))
            current_price = float(item.get("ltp", 0))

            if avg_buy_price > 0:
                pnl_percent = ((current_price - avg_buy_price) / avg_buy_price) * 100
            else:
                pnl_percent = 0.0

            holdings.append({
                "symbol": symbol,
                "nse_symbol": f"{symbol}.NS",
                "quantity": quantity,
                "avg_buy_price": round(avg_buy_price, 2),
                "current_price": round(current_price, 2),
                "pnl_percent": round(pnl_percent, 2),
            })
        except (ValueError, TypeError):
            # Skip malformed entries
            continue

    return holdings


def fetch_portfolio():
    """Top-level function: login and return clean holdings list."""
    api = login()
    return get_holdings(api)


def main():
    holdings = fetch_portfolio()

    if not holdings:
        print("No holdings found in portfolio.")
        return

    print(f"\n{'SYMBOL':<20} {'QTY':>6} {'AVG BUY':>10} {'CMP':>10} {'P&L %':>8}")
    print("-" * 58)

    for h in holdings:
        pnl_str = f"{h['pnl_percent']:+.2f}%"
        print(
            f"{h['symbol']:<20} {h['quantity']:>6} "
            f"{h['avg_buy_price']:>10.2f} {h['current_price']:>10.2f} "
            f"{pnl_str:>8}"
        )

    print("-" * 58)
    print(f"Total holdings: {len(holdings)}")


if __name__ == "__main__":
    main()
