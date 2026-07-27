import csv
import fcntl
import json
import os
import tempfile
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import yfinance as yf
import pandas as pd

from nse_calendar import is_trading_day, trading_days_from, get_trading_days_elapsed
from signal_logger import (
    COLUMNS, DATA_DIR, SIGNALS_FILE,
    get_pending_tracking, get_all_signals,
)

IST = ZoneInfo("Asia/Kolkata")

# Sector index name → yfinance ticker
SECTOR_TICKER_MAP = {
    "Nifty IT":     "^CNXIT",
    "Nifty Bank":   "^NSEBANK",
    "Nifty Pharma": "NIFTY_PHARMA.NS",
    "Nifty 50":     "^NSEI",
}


# ── Price fetching ─────────────────────────────────────────────────────────────

def fetch_closing_price(symbol: str, target_date: date, allow_stale_after_days: int = 10) -> float | None:
    """
    Return the closing price of symbol (NSE) EXACTLY on target_date.

    target_date is always a trading day (computed via trading_days_from), so an
    exact bar should exist once the EOD data is published. If it isn't published
    yet, return None — the row stays pending and is retried on the next run.
    This replaces the old "last close on or before" fallback, which silently
    recorded wrong-date prices when run before EOD data landed.

    Escape hatch: if target_date is more than `allow_stale_after_days` calendar
    days in the past and still has no bar (unscheduled market closure, missing
    holiday entry), fall back to the last close before it — loudly.
    """
    ns_ticker = symbol if symbol.startswith("^") else f"{symbol}.NS"
    start = target_date - timedelta(days=7)
    end   = target_date + timedelta(days=1)

    try:
        df = yf.download(
            ns_ticker,
            start=str(start),
            end=str(end),
            auto_adjust=True,
            progress=False,
        )
        if df.empty:
            return None

        # Flatten MultiIndex if present (single ticker can still return MultiIndex)
        if isinstance(df.columns, pd.MultiIndex):
            df = df["Close"]
            if isinstance(df, pd.DataFrame):
                df = df.iloc[:, 0]
        else:
            df = df["Close"]

        df.index = pd.to_datetime(df.index).date

        exact = df[df.index == target_date]
        if not exact.empty:
            return round(float(exact.iloc[-1]), 2)

        # No bar for target_date yet.
        days_old = (datetime.now(IST).date() - target_date).days
        if days_old > allow_stale_after_days:
            eligible = df[df.index < target_date]
            if not eligible.empty:
                stale_date = eligible.index[-1]
                print(
                    f"WARNING: no bar for {symbol} on {target_date} after {days_old}d — "
                    f"using stale close from {stale_date}"
                )
                return round(float(eligible.iloc[-1]), 2)

        return None

    except Exception as e:
        print(f"WARNING: fetch_closing_price({symbol}, {target_date}) failed — {e}")
        return None


# ── Return / alpha calculation ─────────────────────────────────────────────────

def calculate_return(action: str, price_at_signal: float, price_at_tracking: float) -> float:
    """
    BUY:  ((tracking - signal) / signal) * 100   positive = correct
    SELL: ((signal - tracking) / signal) * 100   positive = correct
    """
    if price_at_signal == 0:
        return 0.0
    if action == "BUY":
        return round(((price_at_tracking - price_at_signal) / price_at_signal) * 100, 2)
    else:  # SELL
        return round(((price_at_signal - price_at_tracking) / price_at_signal) * 100, 2)


def calculate_alpha(signal_return: float, benchmark_return: float) -> float:
    """alpha = signal_return - benchmark_return. Positive = beat the market."""
    return round(signal_return - benchmark_return, 2)


# ── CSV update (atomic) ────────────────────────────────────────────────────────

def _atomic_update_row(signal_id: str, updates: dict):
    """
    Read the full CSV, update the matching row in-place, write atomically.
    Uses flock to prevent concurrent writes from scheduler + on-demand calls.
    """
    with open(SIGNALS_FILE, "r", newline="") as src:
        fcntl.flock(src, fcntl.LOCK_EX)
        try:
            rows = list(csv.DictReader(src))
            modified = False
            for row in rows:
                if row["signal_id"] == signal_id:
                    for k, v in updates.items():
                        if k in row and (row[k] == "" or row[k] is None):
                            row[k] = v
                    modified = True
                    break

            if not modified:
                print(f"WARNING: signal_id {signal_id} not found in CSV")
                return

            with tempfile.NamedTemporaryFile(
                mode="w", dir=DATA_DIR, delete=False, suffix=".tmp", newline=""
            ) as tmp:
                writer = csv.DictWriter(tmp, fieldnames=COLUMNS)
                writer.writeheader()
                writer.writerows(rows)
                tmp_path = tmp.name

            os.replace(tmp_path, SIGNALS_FILE)
        finally:
            fcntl.flock(src, fcntl.LOCK_UN)


# ── Core update logic ──────────────────────────────────────────────────────────

def update_signal_outcomes(
    signal_id: str,
    action: str,
    price_at_signal: float,
    signal_date: date,
    sector_index_name: str,
    existing: dict,
):
    """
    Determine which tracking dates are due, fetch prices, compute returns/alphas,
    and update only the still-empty columns for this signal.
    """
    elapsed = get_trading_days_elapsed(signal_date, datetime.now(IST).date())
    updates = {}

    def _get_prices_for_day(n):
        """Return (stock_price, nifty_price, sector_price) for n trading days after signal."""
        track_date = trading_days_from(signal_date, n)
        stock_p  = fetch_closing_price(existing["stock_symbol"], track_date)
        nifty_p  = fetch_closing_price("^NSEI", track_date)
        sector_ticker = SECTOR_TICKER_MAP.get(sector_index_name, "^NSEI")
        sector_p = fetch_closing_price(sector_ticker, track_date)
        return stock_p, nifty_p, sector_p, track_date

    # 1-day tracking
    if elapsed >= 1 and not existing.get("price_1d"):
        stock_p, _, _, td = _get_prices_for_day(1)
        if stock_p:
            updates["price_1d"]      = stock_p
            updates["return_1d_pct"] = calculate_return(action, price_at_signal, stock_p)
            print(f"  [1d] {existing['stock_symbol']} @ {stock_p} on {td} → {updates['return_1d_pct']:+.2f}%")

    # 3-day tracking (includes benchmark + alpha)
    if elapsed >= 3 and not existing.get("price_3d"):
        stock_p, nifty_p, sector_p, td = _get_prices_for_day(3)
        if stock_p:
            signal_ret  = calculate_return(action, price_at_signal, stock_p)
            nifty_at_sig = float(existing.get("nifty50_at_signal") or 0)
            sector_at_sig = float(existing.get("sector_index_at_signal") or 0)

            nifty_ret  = round(((nifty_p  - nifty_at_sig)  / nifty_at_sig  * 100), 2) if nifty_p  and nifty_at_sig  else None
            sector_ret = round(((sector_p - sector_at_sig) / sector_at_sig * 100), 2) if sector_p and sector_at_sig else None

            updates["price_3d"]              = stock_p
            updates["return_3d_pct"]         = signal_ret
            updates["nifty50_return_3d_pct"] = nifty_ret  if nifty_ret  is not None else ""
            updates["sector_return_3d_pct"]  = sector_ret if sector_ret is not None else ""
            updates["alpha_vs_nifty_3d"]     = calculate_alpha(signal_ret, nifty_ret)  if nifty_ret  is not None else ""
            updates["alpha_vs_sector_3d"]    = calculate_alpha(signal_ret, sector_ret) if sector_ret is not None else ""
            print(f"  [3d] {existing['stock_symbol']} @ {stock_p} → signal={signal_ret:+.2f}% | α_nifty={updates['alpha_vs_nifty_3d']} | α_sector={updates['alpha_vs_sector_3d']}")

    # 5-day tracking
    if elapsed >= 5 and not existing.get("price_5d"):
        stock_p, _, _, td = _get_prices_for_day(5)
        if stock_p:
            updates["price_5d"]      = stock_p
            updates["return_5d_pct"] = calculate_return(action, price_at_signal, stock_p)
            print(f"  [5d] {existing['stock_symbol']} @ {stock_p} on {td} → {updates['return_5d_pct']:+.2f}%")

    # 10-day tracking (matches "5-7 day" / "7-10 day" signal horizons)
    if elapsed >= 10 and not existing.get("price_10d"):
        stock_p, _, _, td = _get_prices_for_day(10)
        if stock_p:
            updates["price_10d"]      = stock_p
            updates["return_10d_pct"] = calculate_return(action, price_at_signal, stock_p)
            print(f"  [10d] {existing['stock_symbol']} @ {stock_p} on {td} → {updates['return_10d_pct']:+.2f}%")

    # 20-day tracking (~1 month; catches slow theses and mean-reversion reversals)
    if elapsed >= 20 and not existing.get("price_20d"):
        stock_p, _, _, td = _get_prices_for_day(20)
        if stock_p:
            updates["price_20d"]      = stock_p
            updates["return_20d_pct"] = calculate_return(action, price_at_signal, stock_p)
            print(f"  [20d] {existing['stock_symbol']} @ {stock_p} on {td} → {updates['return_20d_pct']:+.2f}%")

    if updates:
        _atomic_update_row(signal_id, updates)

    return updates


# ── Heartbeat ──────────────────────────────────────────────────────────────────

HEARTBEAT_FILE = os.path.join(DATA_DIR, "tracker_heartbeat.json")


def _write_heartbeat(summary: dict):
    """Record that the tracker ran. run_briefing.py checks this for staleness."""
    try:
        payload = {"last_run": datetime.now(IST).isoformat(), "summary": summary}
        with open(HEARTBEAT_FILE, "w") as f:
            json.dump(payload, f)
    except Exception as e:
        print(f"WARNING: heartbeat write failed — {e}")


# ── Main runner ────────────────────────────────────────────────────────────────

def run_tracking() -> dict:
    """
    Process all pending signals and fill in outcome prices.
    Run daily after EOD data publishes (see run_tracking.py entrypoint).
    Returns a summary dict.
    """
    today = datetime.now(IST).date()
    if not is_trading_day(today):
        print(f"[tracker] {today} is not a trading day — skipping")
        summary = {"skipped": True, "reason": "non-trading day"}
        _write_heartbeat(summary)
        return summary

    pending = get_pending_tracking()
    if pending.empty:
        print("[tracker] No pending signals to track")
        summary = {"updated": 0, "signals": []}
        _write_heartbeat(summary)
        return summary

    print(f"[tracker] Processing {len(pending)} pending signal(s)...")
    summary = {"updated": 0, "signals": []}

    for _, row in pending.iterrows():
        sig_id = row["signal_id"]
        # Outcomes only make sense for directional calls; HOLD/test rows would
        # be scored with SELL semantics by calculate_return — skip them.
        if row.get("action") not in ("BUY", "SELL"):
            continue
        try:
            signal_date = datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S").date()
            price_at_signal = float(row["price_at_signal"])
        except (ValueError, TypeError) as e:
            print(f"WARNING: Cannot parse row for {sig_id} — {e}")
            continue

        print(f"\nTracking {sig_id} (signal date: {signal_date})")
        try:
            updates = update_signal_outcomes(
                signal_id        = sig_id,
                action           = row["action"],
                price_at_signal  = price_at_signal,
                signal_date      = signal_date,
                sector_index_name = row.get("sector_index_name", "Nifty 50"),
                existing         = row.fillna("").to_dict(),
            )
            if updates:
                summary["updated"] += 1
                summary["signals"].append(sig_id)
        except Exception as e:
            print(f"WARNING: Tracking failed for {sig_id} — {e}")
            continue

    print(f"\n[tracker] Done. Updated {summary['updated']} signal(s).")
    _write_heartbeat(summary)
    return summary


# ── main ───────────────────────────────────────────────────────────────────────

def main():
    import csv as _csv
    from signal_logger import COLUMNS, _atomic_append

    # Insert a test signal dated 5 trading days ago
    from nse_calendar import trading_days_from
    signal_date = trading_days_from(date.today(), -5) if False else None

    # Walk back manually — trading_days_from only goes forward
    # Find a date 5 trading days before today
    d = date.today() - timedelta(days=1)
    steps = 0
    while steps < 5:
        if is_trading_day(d):
            steps += 1
            if steps == 5:
                break
        d -= timedelta(days=1)
    signal_date = d

    ts = datetime.combine(signal_date, datetime.min.time().replace(hour=10)).strftime("%Y-%m-%d %H:%M:%S")
    test_row = {col: "" for col in COLUMNS}
    test_row.update({
        "signal_id":              f"sig_{signal_date.strftime('%Y%m%d')}_1000_HDFCBANK",
        "timestamp":              ts,
        "stock_symbol":           "HDFCBANK",
        "action":                 "BUY",
        "price_at_signal":        "876.97",
        "confidence_score":       "60",
        "confidence_label":       "MEDIUM",
        "time_horizon":           "3-5 days",
        "reason":                 "RSI 32.74, oversold, below both MAs, awaiting bounce",
        "event_category":         "technical_only",
        "event_type":             "test",
        "nifty50_at_signal":      "22713.1",
        "sector_index_at_signal": "51548.75",
        "sector_index_name":      "Nifty Bank",
        "schema_version":         "3",
    })

    print(f"Inserting test signal for HDFCBANK on {signal_date}...\n")
    _atomic_append(test_row)

    print("Running outcome tracker...\n")
    summary = run_tracking()

    print("\n--- Updated CSV row ---")
    df = get_all_signals()
    target = df[df["signal_id"] == test_row["signal_id"]]
    if not target.empty:
        show_cols = [
            "signal_id", "stock_symbol", "action",
            "price_at_signal", "price_1d", "price_3d", "price_5d",
            "return_1d_pct", "return_3d_pct", "return_5d_pct",
            "alpha_vs_nifty_3d", "alpha_vs_sector_3d",
        ]
        print(target[show_cols].to_string(index=False))
    else:
        print("Row not found — check signal_id")


if __name__ == "__main__":
    main()
