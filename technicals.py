import yfinance as yf
import pandas as pd
from ta.momentum import RSIIndicator

# Crossing threshold: price within this % of MA counts as "crossing"
CROSSING_THRESHOLD = 0.5

SECTOR_MAP = {
    "TCS":       ("^CNXIT",           "Nifty IT"),
    "INFY":      ("^CNXIT",           "Nifty IT"),
    "HDFCBANK":  ("^NSEBANK",        "Nifty Bank"),
    "ICICIBANK": ("^NSEBANK",        "Nifty Bank"),
    "SBIN":      ("^NSEBANK",        "Nifty Bank"),
    "SUNPHARMA": ("NIFTY_PHARMA.NS", "Nifty Pharma"),
    "DEFAULT":   ("^NSEI",           "Nifty 50"),
}


_EXCHANGE_SUFFIXES = {"-EQ", "-BE", "-BL", "-N1", "-N2", "-SM", "-IL"}

def _clean_symbol(symbol):
    """Strip exchange suffix (-EQ, -BE, etc.) to get the base NSE symbol."""
    for suffix in _EXCHANGE_SUFFIXES:
        if symbol.endswith(suffix):
            return symbol[:-len(suffix)]
    return symbol


def _ma_position(price, ma):
    """Return 'crossing' | 'above' | 'below' relative to a moving average."""
    if ma is None or ma == 0:
        return "unknown"
    pct_diff = (price - ma) / ma * 100
    if abs(pct_diff) <= CROSSING_THRESHOLD:
        return "crossing"
    return "above" if price > ma else "below"


def _bulk_download(tickers, period="3mo"):
    """
    Download OHLCV for a list of tickers in one yfinance call.
    Returns a dict of {ticker: DataFrame} with Close prices.
    """
    if not tickers:
        return {}

    raw = yf.download(
        tickers,
        period=period,
        auto_adjust=True,
        progress=False,
        threads=True,
    )

    result = {}

    if len(tickers) == 1:
        # Single ticker: yfinance returns flat DataFrame
        ticker = tickers[0]
        if not raw.empty and "Close" in raw.columns:
            result[ticker] = raw[["Close"]].rename(columns={"Close": "close"})
        elif not raw.empty and isinstance(raw.columns, pd.MultiIndex) and "Close" in raw.columns.get_level_values(0):
            result[ticker] = raw["Close"].rename(columns={ticker: "close"}) if ticker in raw["Close"].columns else pd.DataFrame()
    else:
        # Multi-ticker: yfinance returns MultiIndex columns (field, ticker)
        if "Close" in raw.columns.get_level_values(0):
            close_df = raw["Close"]
            for ticker in tickers:
                if ticker in close_df.columns:
                    series = close_df[ticker].dropna()
                    if not series.empty:
                        result[ticker] = series.to_frame(name="close")

    return result


def _compute_technicals(ticker, df):
    """Compute RSI-14, MA20, MA50 from a close-price DataFrame."""
    if df is None or len(df) < 20:
        return None

    close = df["close"]
    current_price = float(close.iloc[-1])

    ma20 = float(close.rolling(20).mean().iloc[-1]) if len(close) >= 20 else None
    ma50 = float(close.rolling(50).mean().iloc[-1]) if len(close) >= 50 else None

    rsi_14 = None
    if len(close) >= 15:
        rsi_series = RSIIndicator(close=close, window=14).rsi()
        if not rsi_series.empty and not pd.isna(rsi_series.iloc[-1]):
            rsi_14 = round(float(rsi_series.iloc[-1]), 2)

    return {
        "current_price": round(current_price, 2),
        "rsi_14": rsi_14,
        "ma20": round(ma20, 2) if ma20 else None,
        "ma50": round(ma50, 2) if ma50 else None,
        "ma20_position": _ma_position(current_price, ma20),
        "ma50_position": _ma_position(current_price, ma50),
    }


def get_nifty_price():
    """Return the latest Nifty 50 closing price, or None on failure."""
    try:
        data = _bulk_download(["^NSEI"], period="5d")
        df = data.get("^NSEI")
        if df is not None and not df.empty:
            val = df["close"].dropna().iloc[-1]
            return round(float(val.iloc[0]) if hasattr(val, "iloc") else float(val), 2)
    except Exception as e:
        print(f"WARNING: Could not fetch Nifty 50 price — {e}")
    return None


def get_sector_index(symbol):
    """
    Return (price, sector_name) for the sector index of a given symbol.
    Falls back to Nifty 50 if symbol not in SECTOR_MAP.
    """
    base = _clean_symbol(symbol)
    ticker, name = SECTOR_MAP.get(base, SECTOR_MAP["DEFAULT"])
    try:
        data = _bulk_download([ticker], period="5d")
        df = data.get(ticker)
        if df is not None and not df.empty:
            return round(float(df["close"].iloc[-1]), 2), name
    except Exception as e:
        print(f"WARNING: Could not fetch sector index {ticker} — {e}")
    return None, name


def get_technicals(symbol):
    """
    Fetch and compute technical indicators for a single symbol.
    Returns a result dict, or None if data is unavailable.
    """
    base = _clean_symbol(symbol)
    ns_ticker = f"{base}.NS"

    try:
        data = _bulk_download([ns_ticker], period="3mo")
        df = data.get(ns_ticker)
        tech = _compute_technicals(ns_ticker, df)
    except Exception as e:
        print(f"WARNING: technicals failed for {symbol} — {e}")
        return None

    if tech is None:
        print(f"WARNING: insufficient data for {symbol}")
        return None

    nifty_price = get_nifty_price()
    sector_price, sector_name = get_sector_index(symbol)

    return {
        "symbol": base,
        **tech,
        "nifty50_price": nifty_price,
        "sector_index_price": sector_price,
        "sector_index_name": sector_name,
    }


def get_all_technicals(symbols_list):
    """
    Efficiently fetch technicals for all symbols using bulk download.
    Returns dict of {base_symbol: result_dict}.
    """
    # Build unique (base_symbol → ns_ticker) mapping
    sym_map = {}
    for s in symbols_list:
        base = _clean_symbol(s)
        sym_map[base] = f"{base}.NS"

    ns_tickers = list(sym_map.values())

    # Bulk download all stock data in one call
    print(f"Fetching data for {len(ns_tickers)} symbols...")
    try:
        all_data = _bulk_download(ns_tickers, period="3mo")
    except Exception as e:
        print(f"WARNING: Bulk download failed — {e}")
        all_data = {}

    # Fetch Nifty 50 once
    nifty_price = get_nifty_price()

    # Fetch unique sector indices in bulk
    sector_tickers = set()
    for base in sym_map:
        ticker, _ = SECTOR_MAP.get(base, SECTOR_MAP["DEFAULT"])
        sector_tickers.add(ticker)

    try:
        sector_data = _bulk_download(list(sector_tickers), period="5d")
    except Exception as e:
        print(f"WARNING: Sector index bulk download failed — {e}")
        sector_data = {}

    results = {}
    for base, ns_ticker in sym_map.items():
        df = all_data.get(ns_ticker)
        tech = _compute_technicals(ns_ticker, df)

        if tech is None:
            continue

        sector_ticker, sector_name = SECTOR_MAP.get(base, SECTOR_MAP["DEFAULT"])
        sector_df = sector_data.get(sector_ticker)
        sector_price = None
        if sector_df is not None and not sector_df.empty:
            val = sector_df["close"].dropna().iloc[-1]
            sector_price = round(float(val.iloc[0]) if hasattr(val, "iloc") else float(val), 2)

        results[base] = {
            "symbol": base,
            **tech,
            "nifty50_price": nifty_price,
            "sector_index_price": sector_price,
            "sector_index_name": sector_name,
        }

    print(f"Technicals computed for {len(results)}/{len(sym_map)} symbols.")
    return results


def main():
    samples = ["INFY-EQ", "HDFCBANK-EQ", "SBIN-EQ", "RELIANCE-EQ", "TCS-EQ"]

    print(f"\nFetching technicals for: {[s.split('-')[0] for s in samples]}\n")
    results = get_all_technicals(samples)

    header = f"{'SYMBOL':<12} {'PRICE':>8} {'RSI':>6} {'MA20':>8} {'MA50':>8} {'vs MA20':<10} {'vs MA50':<10} {'SECTOR'}"
    print(header)
    print("-" * len(header))

    for base, r in results.items():
        print(
            f"{r['symbol']:<12} {r['current_price']:>8.2f} "
            f"{str(r['rsi_14'] or 'N/A'):>6} "
            f"{str(r['ma20'] or 'N/A'):>8} "
            f"{str(r['ma50'] or 'N/A'):>8} "
            f"{r['ma20_position']:<10} {r['ma50_position']:<10} "
            f"{r['sector_index_name']} @ {r['sector_index_price']}"
        )

    print(f"\nNifty 50: {results[next(iter(results))]['nifty50_price']}")


if __name__ == "__main__":
    main()
