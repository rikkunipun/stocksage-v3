"""
One-time recovery merge: restore the June/July signals lost in the Jul 2026
data clobber by unioning a pre-clobber snapshot with the current live file.

Inputs:
  --recovered   pre-clobber snapshot (v3, ~151 rows through Jul 27 08:31)
  --live        current server file  (v4, ~33 rows: Apr-May survivors + post-
                clobber signals)
  --out         merged output path

Merge rule (union by signal_id):
  - A signal_id present in BOTH → keep the LIVE row. Live is v4 and already
    carries backfilled outcomes for the Apr-May rows; the recovered copies of
    those rows have empty outcomes.
  - A signal_id present only in RECOVERED → migrate it v3→v4 (add empty
    price_10d/20d + return_10d/20d cols, schema_version=4) and include it.
    This is the restored June/July gap.
  - A signal_id present only in LIVE → include as-is (post-clobber signals).
  Result is sorted by timestamp. Empty outcome cells are filled afterward by
  run_tracking.py (idempotent — it only writes empty cells).

--dry-run prints a full reconciliation and writes NOTHING.
"""
import argparse
import csv
import sys

from signal_logger import COLUMNS  # v4 schema (33 cols) — single source of truth


def _load(path: str) -> dict:
    """Return {signal_id: row_dict} keyed by signal_id, in file order."""
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    out = {}
    for r in rows:
        sid = r.get("signal_id", "")
        if sid:
            out[sid] = r
    return out


def _to_v4(row: dict) -> dict:
    """Coerce any row (v3 or v4) into a full v4 row; unknown/missing cols empty."""
    v4 = {col: (row.get(col) or "") for col in COLUMNS}
    v4["schema_version"] = "4"
    return v4


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--recovered", required=True)
    ap.add_argument("--live", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    recovered = _load(args.recovered)
    live = _load(args.live)

    all_ids = set(recovered) | set(live)
    both = set(recovered) & set(live)
    only_recovered = set(recovered) - set(live)   # the restored gap
    only_live = set(live) - set(recovered)         # post-clobber signals

    merged = {}
    for sid in all_ids:
        src = live[sid] if sid in live else recovered[sid]  # live wins on collision
        merged[sid] = _to_v4(src)

    merged_rows = sorted(merged.values(), key=lambda r: r.get("timestamp", ""))

    # ── Reconciliation report ────────────────────────────────────────────────
    def _span(rows):
        ts = sorted(r["timestamp"] for r in rows if r.get("timestamp"))
        return (ts[0], ts[-1]) if ts else ("-", "-")

    jun_jul_restored = sorted(
        sid for sid in only_recovered
        if any(recovered[sid].get("timestamp", "").startswith(p)
               for p in ("2026-06", "2026-07"))
    )

    print("── RECOVERY MERGE — DRY RUN ──" if args.dry_run else "── RECOVERY MERGE ──")
    print(f"recovered snapshot : {len(recovered):>4} rows   span {_span(recovered.values())}")
    print(f"current live file  : {len(live):>4} rows   span {_span(live.values())}")
    print(f"merged result      : {len(merged_rows):>4} rows   span {_span(merged_rows)}")
    print()
    print(f"  shared signal_ids (live kept)     : {len(both):>4}")
    print(f"  only in live (post-clobber kept)  : {len(only_live):>4}")
    print(f"  RESTORED from snapshot            : {len(only_recovered):>4}")
    print(f"    of which June/July signals      : {len(jun_jul_restored):>4}")
    print(f"  net new rows added to live        : {len(merged_rows) - len(live):>4}")
    print()
    print(f"  DUMMY_F present in merge : {'sig_20260722_0830_DUMMY_F' in merged}")
    print()
    print("  Restored June/July signal_ids:")
    for sid in jun_jul_restored:
        r = recovered[sid]
        print(f"    {r['timestamp']}  {r['action']:>4}  {r['stock_symbol']}")

    # sanity: every merged row must have exactly the v4 columns
    bad = [r for r in merged_rows if set(r.keys()) != set(COLUMNS)]
    if bad:
        sys.exit(f"ERROR: {len(bad)} merged rows have wrong columns — aborting.")

    if args.dry_run:
        print("\n[dry-run] No file written. Re-run without --dry-run to write --out.")
        return

    with open(args.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(merged_rows)
    print(f"\nWrote {len(merged_rows)} rows → {args.out}")


if __name__ == "__main__":
    main()
