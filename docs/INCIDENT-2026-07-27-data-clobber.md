# Incident: signals_log.csv data clobber (July 27, 2026)

**Severity:** High — ~2 months of production signal history lost, then fully recovered.
**Status:** Resolved. Data recovered; root cause fixed; recurrence prevented.

## What happened

On **2026-07-27**, the live signal log (`data/signals_log.csv`) on the Oracle
server was overwritten with a stale copy, dropping every signal from
**2026-05-22 through 2026-07-26** — 40 delivered signals, including the
`sig_20260722_0830_DUMMY_F` BUY. The loss was discovered on 2026-07-29 while
pulling the file for calibration analysis: the file had a two-month hole
(rows jumped May 21 → July 27) and the missing signals were in no backup.

Delivery was unaffected — the father received all briefings on Telegram
throughout the window (confirmed from message history). Only the persisted
CSV was lost.

## Root cause

`data/signals_log.csv` was **tracked in git**. The item-1 deploy synced the
whole repo directory to the server with `rsync` (the server is not a git
clone — it was originally `scp`'d, so deploys were hand-typed `rsync`). That
`rsync` carried the repo's committed `data/signals_log.csv` — a stale 27-row
April–May snapshot from the initial commit — and, with no `--exclude data/`,
wrote it over the live file.

Two aggravating factors:
1. The daily-backup job that would have caught it (`create_daily_backup`)
   shipped **in that same deploy** — it only began protecting the file one run
   *after* the clobber, so no backup captured the June/July signals.
2. The deploy was a hand-typed `rsync`; the `--exclude` was simply never typed.

**Timing (pinned to ~90 min):** the recovered snapshot ends 2026-07-27 08:31
(intact); the first daily backup at 2026-07-27 09:59 already shows the gap.
The clobber occurred in that window — the item-1 deploy.

## How it was recovered

A pre-clobber `scp -r` snapshot of the whole server directory existed on the
maintainer's Mac (`~/stocksage-server-copy/`, taken 2026-07-27 08:31),
containing all 151 rows through the clobber moment. Recovery:

1. `merge_recover.py` unioned the snapshot with the current live file by
   `signal_id` (live wins on collision — it had backfilled outcomes for the
   surviving rows), migrated the snapshot v3→v4, and dropped synthetic test
   rows. Result: 159 rows.
2. Current live file backed up server-side and locally first
   (`signals_log_pre_recovery_<ts>.csv`).
3. Merged file pushed to the server; `run_tracking.py` backfilled outcomes for
   the restored rows. Verified: 0 stale gaps, DUMMY_F present with its
   outcomes, no duplicate IDs.

Net: no data permanently lost.

## Why it cannot recur

1. **`data/` is untracked and gitignored.** No working copy carries a
   `signals_log.csv`, so no full-dir sync can push one over the server file.
   This removes the mechanism entirely. (`data/nse_holidays.json`, static
   reference data, remains tracked.)
2. **`deploy.sh` replaces hand-typed rsync** — code-only, with hardcoded
   `--exclude data/ .env logs/ venv/ __pycache__/`, no `--delete`, and a
   `--dry-run` mode. This alone would have prevented the incident.
3. **Daily backups now run** (via `run_tracking.py`), so any future data
   mistake is recoverable within one day — which was not true at the time.

## Lessons

- Runtime data must never be tracked in the same repo that gets deployed
  wholesale. Separate the datastore from the code.
- A backup mechanism only protects data written *after* it starts running;
  shipping the fix and relying on it in the same operation protects nothing.
- Automate deploys so safety flags can't be forgotten. The failure was a
  missing `--exclude`, not a complex bug.
