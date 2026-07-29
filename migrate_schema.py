"""
Idempotent schema migration for signals_log.csv → current SCHEMA_VERSION.

Adds any columns the current schema has that the file lacks (empty-filled),
drops nothing, reorders to the canonical column order, and stamps every row
with the current schema_version. Handles v3→v4 (10d/20d), v4→v5 (entry/stop/
target/invalidation), or any future bump — it diffs against signal_logger.COLUMNS.

Reads COLUMNS + SCHEMA_VERSION directly from signal_logger.py source rather than
importing the module (import runs _init_csv, whose header check raises on an
out-of-date file — the exact thing we're here to fix).

Usage (on the machine with the live CSV):
    python3 migrate_schema.py

Writes a timestamped backup before touching the file. Safe to re-run.
"""
import csv
import os
import re
import shutil
import sys
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(BASE_DIR, "data", "signals_log.csv")


def _load_target_columns():
    """Extract COLUMNS and SCHEMA_VERSION from signal_logger.py without importing it."""
    ns = {}
    with open(os.path.join(BASE_DIR, "signal_logger.py")) as f:
        src = f.read()
    ver = int(re.search(r"^SCHEMA_VERSION\s*=\s*(\d+)", src, re.M).group(1))
    cols_src = re.search(r"^COLUMNS\s*=\s*\[.*?\]", src, re.M | re.S).group(0)
    exec(cols_src, ns)
    return ns["COLUMNS"], ver


def main():
    columns, version = _load_target_columns()

    if not os.path.exists(CSV_PATH):
        sys.exit(f"ERROR: {CSV_PATH} not found — run on the machine with the live CSV.")

    with open(CSV_PATH, newline="") as f:
        header = next(csv.reader(f), None)
    with open(CSV_PATH, newline="") as f:
        rows = list(csv.DictReader(f))

    if header == columns and all(r.get("schema_version") == str(version) for r in rows):
        print(f"Already on schema v{version} — nothing to do.")
        return

    backup = CSV_PATH.replace(".csv", f"_pre_v{version}_{datetime.now():%Y%m%d_%H%M%S}.csv")
    shutil.copy2(CSV_PATH, backup)

    migrated = []
    for r in rows:
        new_row = {col: (r.get(col) or "") for col in columns}
        new_row["schema_version"] = str(version)
        migrated.append(new_row)

    tmp = CSV_PATH + ".tmp"
    with open(tmp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns)
        w.writeheader()
        w.writerows(migrated)
    os.replace(tmp, CSV_PATH)

    print(f"Migrated {len(migrated)} row(s) → schema v{version}.")
    print(f"Backup: {backup}")


if __name__ == "__main__":
    main()
