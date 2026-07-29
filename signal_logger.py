import os
import csv
import json
import fcntl
import shutil
import tempfile
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
from nse_calendar import is_trading_day

DATA_DIR      = os.path.join(os.path.dirname(__file__), "data")
SIGNALS_FILE  = os.path.join(DATA_DIR, "signals_log.csv")
FAILSAFE_FILE = os.path.join(DATA_DIR, "failsafe.jsonl")
BACKUPS_DIR   = os.path.join(DATA_DIR, "backups")
SCHEMA_VERSION = 4

IST = ZoneInfo("Asia/Kolkata")

COLUMNS = [
    "signal_id", "timestamp", "stock_symbol", "action",
    "price_at_signal", "confidence_score", "confidence_label",
    "time_horizon", "reason", "event_category", "event_type",
    "source_headlines", "rsi_at_signal", "ma20_position",
    "ma50_position", "nifty50_at_signal", "sector_index_at_signal",
    "sector_index_name", "price_1d", "price_3d", "price_5d",
    "price_10d", "price_20d",
    "return_1d_pct", "return_3d_pct", "return_5d_pct",
    "return_10d_pct", "return_20d_pct",
    "nifty50_return_3d_pct", "sector_return_3d_pct",
    "alpha_vs_nifty_3d", "alpha_vs_sector_3d", "schema_version",
]


# ── Startup validation ─────────────────────────────────────────────────────────

def _ensure_data_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(BACKUPS_DIR, exist_ok=True)


def _init_csv():
    """Create CSV with headers if it doesn't exist; validate headers if it does."""
    _ensure_data_dirs()

    if not os.path.exists(SIGNALS_FILE):
        with open(SIGNALS_FILE, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=COLUMNS)
            writer.writeheader()
        return

    # Validate existing headers
    with open(SIGNALS_FILE, newline="") as f:
        reader = csv.reader(f)
        try:
            existing = next(reader)
        except StopIteration:
            # Empty file — rewrite header
            with open(SIGNALS_FILE, "w", newline="") as fw:
                csv.DictWriter(fw, fieldnames=COLUMNS).writeheader()
            return

    if existing != COLUMNS:
        missing  = set(COLUMNS) - set(existing)
        extra    = set(existing) - set(COLUMNS)
        raise RuntimeError(
            f"signals_log.csv header mismatch.\n"
            f"  Missing columns : {missing or 'none'}\n"
            f"  Unexpected cols : {extra or 'none'}\n"
            f"Rename or delete the file to start fresh."
        )


# Run at import time
_init_csv()


# ── Internal helpers ───────────────────────────────────────────────────────────

def _now_ist():
    return datetime.now(IST)


def _make_signal_id(symbol, ts):
    return f"sig_{ts.strftime('%Y%m%d_%H%M')}_{symbol}"


def _atomic_append(row: dict):
    """
    Append one row to signals_log.csv using a temp-file + os.replace() pattern
    with an exclusive flock to prevent concurrent writes.
    """
    tmp_path = SIGNALS_FILE + ".tmp"

    with open(SIGNALS_FILE, "r", newline="") as src:
        fcntl.flock(src, fcntl.LOCK_EX)
        try:
            content = src.read()
            with tempfile.NamedTemporaryFile(
                mode="w", dir=DATA_DIR, delete=False, suffix=".tmp", newline=""
            ) as tmp:
                tmp.write(content)
                writer = csv.DictWriter(tmp, fieldnames=COLUMNS)
                writer.writerow(row)
                tmp_path = tmp.name
            os.replace(tmp_path, SIGNALS_FILE)
        finally:
            fcntl.flock(src, fcntl.LOCK_UN)


def _write_failsafe(data: dict):
    """Append a JSON record to failsafe.jsonl — last resort if CSV write fails."""
    _ensure_data_dirs()
    with open(FAILSAFE_FILE, "a") as f:
        f.write(json.dumps(data) + "\n")
    print(f"WARNING: Signal saved to failsafe.jsonl — {data.get('signal_id')}")


def is_duplicate_signal(symbol: str, action: str, days: int = 3) -> bool:
    """
    Check if the same stock + action was logged in the last N trading days.
    Returns True if duplicate found (should skip logging).
    """
    try:
        df = get_all_signals()
        if df.empty:
            return False

        # Filter for same symbol and action
        mask = (df["stock_symbol"] == symbol) & (df["action"] == action)
        recent = df[mask].copy()

        if recent.empty:
            return False

        # Count trading days backward from today
        today = datetime.now(IST).date()
        cutoff_date = today
        trading_days_counted = 0

        while trading_days_counted < days:
            cutoff_date -= timedelta(days=1)
            if is_trading_day(cutoff_date):
                trading_days_counted += 1

        # Parse timestamps and check if any fall within the window
        for ts_str in recent["timestamp"]:
            try:
                sig_date = datetime.strptime(str(ts_str), "%Y-%m-%d %H:%M:%S").date()
                if sig_date > cutoff_date:
                    return True
            except (ValueError, TypeError):
                continue

        return False

    except Exception as e:
        print(f"WARNING: is_duplicate_signal() failed — {e}")
        return False  # On error, allow the signal


# ── Public API ─────────────────────────────────────────────────────────────────

def log_signal(
    signal: dict,
    technicals: dict,
    nifty_price,
    sector_price,
    sector_name: str,
    headlines: list,
    event_type: str,
) -> str:
    """
    Log one signal to signals_log.csv.
    Returns the signal_id.
    Falls back to failsafe.jsonl if CSV write fails twice.
    """
    ts     = _now_ist()
    symbol = signal.get("stock_symbol", "UNKNOWN")
    action = signal.get("action", "")

    # FIX 2: Skip duplicate signals (same stock + action in last 3 trading days)
    if is_duplicate_signal(symbol, action, days=3):
        print(f"[Dedup] Skipping duplicate {action} signal for {symbol} (logged in last 3 trading days)")
        return f"SKIPPED_{symbol}_{action}"

    sig_id = _make_signal_id(symbol, ts)

    tech = technicals.get(symbol, {})
    ss   = signal.get("supporting_signals", {})

    # Serialise headlines as pipe-separated titles (max 3)
    headline_str = " | ".join(h.get("title", "") for h in (headlines or [])[:3])

    row = {
        "signal_id":               sig_id,
        "timestamp":               ts.strftime("%Y-%m-%d %H:%M:%S"),
        "stock_symbol":            symbol,
        "action":                  signal.get("action", ""),
        "price_at_signal":         tech.get("current_price", ""),
        "confidence_score":        signal.get("confidence_score", ""),
        "confidence_label":        signal.get("confidence_label", ""),
        "time_horizon":            signal.get("time_horizon", ""),
        "reason":                  signal.get("reason", ""),
        "event_category":          signal.get("event_category", ""),
        "event_type":              event_type,
        "source_headlines":        headline_str,
        "rsi_at_signal":           ss.get("rsi_14", ""),
        "ma20_position":           ss.get("ma20_position", tech.get("ma20_position", "")),
        "ma50_position":           ss.get("ma50_position", tech.get("ma50_position", "")),
        "nifty50_at_signal":       nifty_price if nifty_price is not None else "",
        "sector_index_at_signal":  sector_price if sector_price is not None else "",
        "sector_index_name":       sector_name,
        # Outcome columns — filled later by outcome_tracker.py
        "price_1d":                "",
        "price_3d":                "",
        "price_5d":                "",
        "price_10d":               "",
        "price_20d":               "",
        "return_1d_pct":           "",
        "return_3d_pct":           "",
        "return_5d_pct":           "",
        "return_10d_pct":          "",
        "return_20d_pct":          "",
        "nifty50_return_3d_pct":   "",
        "sector_return_3d_pct":    "",
        "alpha_vs_nifty_3d":       "",
        "alpha_vs_sector_3d":      "",
        "schema_version":          SCHEMA_VERSION,
    }

    for attempt in range(1, 3):
        try:
            _atomic_append(row)
            return sig_id
        except Exception as e:
            print(f"WARNING: CSV write attempt {attempt} failed — {e}")

    # Both attempts failed — use failsafe
    _write_failsafe(row)
    return sig_id


def get_all_signals() -> pd.DataFrame:
    """
    Read signals_log.csv and return as DataFrame.
    Raises RuntimeError if schema_version != 3 on any row.
    Returns empty DataFrame if file has no data rows.
    """
    if not os.path.exists(SIGNALS_FILE):
        return pd.DataFrame(columns=COLUMNS)

    df = pd.read_csv(SIGNALS_FILE, dtype=str)

    if df.empty:
        return df

    bad_versions = df[df["schema_version"].astype(str) != str(SCHEMA_VERSION)]
    if not bad_versions.empty:
        raise RuntimeError(
            f"{len(bad_versions)} rows have schema_version != {SCHEMA_VERSION}. "
            "Run `python migrate_schema_v4.py` before reading."
        )

    return df


def get_pending_tracking() -> pd.DataFrame:
    """Return rows where price_1d, price_3d, or price_5d is null/empty."""
    df = get_all_signals()
    if df.empty:
        return df

    mask = (
        df["price_1d"].isna()  | (df["price_1d"] == "")  |
        df["price_3d"].isna()  | (df["price_3d"] == "")  |
        df["price_5d"].isna()  | (df["price_5d"] == "")  |
        df["price_10d"].isna() | (df["price_10d"] == "") |
        df["price_20d"].isna() | (df["price_20d"] == "")
    )
    return df[mask].reset_index(drop=True)


def get_signal_history(symbol: str, limit: int = 5) -> list:
    """
    Return the most recent past signals for `symbol` with their realized
    outcomes, so the deep-dive call can see its own track record and avoid
    re-issuing a thesis that already failed (e.g. the repeated DUMMY_B SELL).
    Excludes suppressed/test rows. Newest first.
    """
    base = symbol.split("-")[0].upper()
    try:
        df = get_all_signals()
        if df.empty:
            return []
        sub = df[(df["stock_symbol"].str.upper() == base) &
                 (df["action"].isin(["BUY", "SELL"])) &
                 (~df["event_type"].isin(["suppressed", "test"]))].copy()
        if sub.empty:
            return []
        sub = sub.sort_values("timestamp", ascending=False).head(limit)
        out = []
        for _, r in sub.iterrows():
            out.append({
                "date":       str(r["timestamp"])[:10],
                "action":     r["action"],
                "confidence": r.get("confidence_score", ""),
                "return_5d":  r.get("return_5d_pct", ""),
                "return_20d": r.get("return_20d_pct", ""),
            })
        return out
    except Exception as e:
        print(f"WARNING: get_signal_history({symbol}) failed — {e}")
        return []


def create_daily_backup() -> str:
    """
    Copy signals_log.csv to data/backups/signals_log_YYYYMMDD.csv.
    Returns the backup filepath.
    """
    _ensure_data_dirs()
    date_str    = _now_ist().strftime("%Y%m%d")
    backup_path = os.path.join(BACKUPS_DIR, f"signals_log_{date_str}.csv")
    shutil.copy2(SIGNALS_FILE, backup_path)
    return backup_path


# ── main ───────────────────────────────────────────────────────────────────────

def main():
    # Two synthetic test signals
    test_signals = [
        {
            "stock_symbol":    "RELIANCE",
            "action":          "BUY",
            "confidence_score": 72,
            "confidence_label": "HIGH",
            "time_horizon":    "3-5 days",
            "reason":          "RSI 28 + crude oil drop boosting refining margins",
            "event_category":  "commodity_shock",
            "supporting_signals": {
                "rsi_14":         28.4,
                "ma20_position":  "below",
                "ma50_position":  "below",
                "news_sentiment": "positive",
            },
        },
        {
            "stock_symbol":    "HDFCBANK",
            "action":          "HOLD",
            "confidence_score": 45,
            "confidence_label": "MEDIUM",
            "time_horizon":    "1-3 days",
            "reason":          "RSI 33 approaching oversold; wait for RBI policy clarity",
            "event_category":  "rate_decision",
            "supporting_signals": {
                "rsi_14":         33.1,
                "ma20_position":  "below",
                "ma50_position":  "below",
                "news_sentiment": "neutral",
            },
        },
    ]

    test_technicals = {
        "RELIANCE": {
            "current_price":       1350.50,
            "ma20_position":       "below",
            "ma50_position":       "below",
            "nifty50_price":       22713.1,
            "sector_index_price":  22713.1,
            "sector_index_name":   "Nifty 50",
        },
        "HDFCBANK": {
            "current_price":       750.90,
            "ma20_position":       "below",
            "ma50_position":       "below",
            "nifty50_price":       22713.1,
            "sector_index_price":  51548.75,
            "sector_index_name":   "Nifty Bank",
        },
    }

    test_headlines = [
        {"title": "Crude oil drops on demand fears"},
        {"title": "RBI holds rates steady amid global volatility"},
        {"title": "Nifty tests 21,700 support level"},
    ]

    print("Logging test signals...\n")

    for sig in test_signals:
        sym   = sig["stock_symbol"]
        tech  = test_technicals.get(sym, {})
        sig_id = log_signal(
            signal       = sig,
            technicals   = test_technicals,
            nifty_price  = 22713.1,
            sector_price = tech.get("sector_index_price"),
            sector_name  = tech.get("sector_index_name", ""),
            headlines    = test_headlines,
            event_type   = "scheduled_analysis",
        )
        print(f"  Logged: {sig_id}")

    print("\n--- signals_log.csv contents ---\n")
    df = get_all_signals()
    display_cols = [
        "signal_id", "stock_symbol", "action",
        "confidence_score", "price_at_signal", "rsi_at_signal",
        "event_category", "schema_version",
    ]
    print(df[display_cols].to_string(index=False))

    print(f"\nPending tracking rows: {len(get_pending_tracking())}")

    backup = create_daily_backup()
    print(f"Backup created: {backup}")


if __name__ == "__main__":
    main()
