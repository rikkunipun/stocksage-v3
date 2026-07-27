"""
One-time migration: signals_log.csv schema v3 -> v4.

Adds the 10d/20d outcome columns (price_10d, price_20d, return_10d_pct,
return_20d_pct) and stamps every row schema_version=4.

Deliberately does NOT import signal_logger — its import-time header check
raises against a v3 file, which would make the migration impossible to run.

Usage (on the server, against the live CSV):
    python3 migrate_schema_v4.py

Safe to re-run: exits early if the header already matches v4.
A timestamped backup of the original file is written next to it first.
"""
import csv
import os
import shutil
import sys
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(BASE_DIR, "data", "signals_log.csv")

# Must match signal_logger.COLUMNS exactly (kept inline — see docstring).
V4_COLUMNS = [
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


def main():
    if not os.path.exists(CSV_PATH):
        sys.exit(f"ERROR: {CSV_PATH} not found — run this on the machine with the live CSV.")

    with open(CSV_PATH, newline="") as f:
        header = next(csv.reader(f), None)

    if header == V4_COLUMNS:
        print("Already on schema v4 — nothing to do.")
        return

    with open(CSV_PATH, newline="") as f:
        rows = list(csv.DictReader(f))

    backup = CSV_PATH.replace(
        ".csv", f"_pre_v4_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    )
    shutil.copy2(CSV_PATH, backup)

    migrated = []
    for r in rows:
        new_row = {col: (r.get(col) or "") for col in V4_COLUMNS}
        new_row["schema_version"] = "4"
        migrated.append(new_row)

    tmp_path = CSV_PATH + ".tmp"
    with open(tmp_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=V4_COLUMNS)
        writer.writeheader()
        writer.writerows(migrated)
    os.replace(tmp_path, CSV_PATH)

    print(f"Migrated {len(migrated)} row(s) to schema v4.")
    print(f"Backup of original: {backup}")


if __name__ == "__main__":
    main()
