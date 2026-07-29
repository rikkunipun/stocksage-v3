#!/usr/bin/env bash
#
# Deploy StockSage code to the Oracle server — CODE ONLY, never runtime data.
#
# This script exists because the Jul 2026 data clobber was caused by a
# hand-typed full-dir rsync that carried a stale committed data/signals_log.csv
# over the live server file. The excludes below are the whole point: never edit
# them out, and never deploy by hand-typing rsync again.
#
# Usage:
#   ./deploy.sh              # sync code, then restart the service
#   ./deploy.sh --dry-run    # show exactly what WOULD sync, change nothing
#   ./deploy.sh --no-restart # sync code but do NOT restart (run a schema
#                            #   migration on the server first, then restart
#                            #   manually). Required whenever this deploy bumps
#                            #   the CSV schema — a restart against an un-migrated
#                            #   file crash-loops the service on the header check.
#
set -euo pipefail

KEY="${STOCKSAGE_SSH_KEY:-$HOME/Downloads/ssh-key-2026-04-19.key}"
HOST="${STOCKSAGE_HOST:-ubuntu@140.245.243.246}"
REMOTE_DIR="${STOCKSAGE_REMOTE_DIR:-~/stocksage/}"
LOCAL_DIR="$(cd "$(dirname "$0")" && pwd)/"

DRY=""; RESTART=1
case "${1:-}" in
  --dry-run)    DRY="--dry-run" ;;
  --no-restart) RESTART=0 ;;
esac

# NEVER remove a --exclude below. data/ is the live datastore; .env holds
# secrets; logs/venv/pycache are host-side. --delete is deliberately NOT used
# (it would remove server-only files, including data/, on any path mismatch).
rsync -avz $DRY \
  --exclude 'data/' \
  --exclude '.env' \
  --exclude 'logs/' \
  --exclude 'venv/' \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  --exclude '.git/' \
  --exclude '.claude/' \
  --exclude 'stocksage-server-copy/' \
  -e "ssh -i $KEY" \
  "$LOCAL_DIR" "$HOST:$REMOTE_DIR"

if [[ -n "$DRY" ]]; then
  echo
  echo "[dry-run] Nothing was changed. Remove --dry-run to deploy."
  exit 0
fi

if [[ "$RESTART" -eq 0 ]]; then
  echo
  echo "Code synced. Service NOT restarted (--no-restart)."
  echo "Next: run migrations on the server, then restart, e.g.:"
  echo "  ssh -i $KEY $HOST 'cd ~/stocksage && python3 migrate_schema.py'"
  echo "  ssh -i $KEY $HOST 'cd ~/stocksage && venv/bin/python run_briefing.py --dry-run'"
  echo "  ssh -i $KEY $HOST 'sudo systemctl restart stocksage'"
  exit 0
fi

echo
echo "Restarting stocksage.service..."
ssh -i "$KEY" "$HOST" "sudo systemctl restart stocksage && sleep 2 && systemctl is-active stocksage"
echo "Deploy complete."
