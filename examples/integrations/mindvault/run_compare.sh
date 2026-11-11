#!/usr/bin/env bash
# All HW8 runs: 2 conditions (MEMORY=off|mindvault) x 2 profiles x REPS runs.
# Each mindvault run gets a freshly reset database and a freshly started
# MindVault service, so taint and staged notes never carry over.
#
# Before running, in this terminal:
#   source ~/.config/mindvault.env      # LLM_* for MindVault's reviewer
#   export MINDVAULT_DIR=/path/to/MindVault   # checked out at tag hw8-base
#
#   bash examples/integrations/mindvault/run_compare.sh [OUT_DIR]
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
TOWN_ROOT="$(cd "$HERE/../../.." && pwd)"
OUT="${1:-$TOWN_ROOT/runs/mindvault-hw8}"
REPS="${REPS:-3}"
PORT="${MEMORY_PORT:-8000}"
: "${MINDVAULT_DIR:?set MINDVAULT_DIR to the MindVault checkout (tag hw8-base)}"
: "${LLM_API_KEY:?source ~/.config/mindvault.env first (MindVault reviewer key)}"

TOWN="${TOWN:-$TOWN_ROOT/.venv/bin/nandatown}"
PY="${PY:-$TOWN_ROOT/.venv/bin/python}"
MV_PY="$MINDVAULT_DIR/.venv/bin/python"
SEED="hw8/seed_with_pricing.json"
export MINDVAULT_URL="http://127.0.0.1:$PORT/mcp"
export MINDVAULT_AGENTS="$MINDVAULT_DIR/config/agents.json"
mkdir -p "$OUT"
MV_PID=""

stop_mv() {
  if [ -n "$MV_PID" ]; then kill "$MV_PID" 2>/dev/null || true; wait "$MV_PID" 2>/dev/null || true; MV_PID=""; fi
}
trap stop_mv EXIT

start_mv() {  # $1 = run folder name
  stop_mv
  (cd "$MINDVAULT_DIR" && SEED_FILE="$SEED" "$MV_PY" scripts/reset.py >/dev/null)
  (cd "$MINDVAULT_DIR" && MEMORY_PORT="$PORT" LABEL_MODE=taint SEARCH_MODE=full RUN_ID="hw8-$1" \
     SEED_FILE="$SEED" exec "$MV_PY" -m server.app) >"$OUT/$1/mindvault.log" 2>&1 &
  MV_PID=$!
  for _ in $(seq 1 60); do
    if curl -s -o /dev/null -X POST "$MINDVAULT_URL"; then return 0; fi
    sleep 0.5
  done
  echo "MindVault did not start; see $OUT/$1/mindvault.log" >&2; exit 1
}

for memory in off mindvault; do
  for profile in quote-clean quote-crash-restart; do
    for rep in $(seq 1 "$REPS"); do
      id="$memory-$profile-$rep"
      rm -rf "${OUT:?}/$id"; mkdir -p "$OUT/$id"
      echo "== $id"
      if [ "$memory" = mindvault ]; then start_mv "$id"; fi
      MEMORY="$memory" MV_LOG="$OUT/$id/mv-calls.jsonl" \
        "$TOWN" run "$profile" --out "$OUT/$id" \
        --agent seller="cmd:$PY $HERE/mv_seller.py" >"$OUT/$id/town.txt" 2>&1 || true
      grep -E "^Verdict" "$OUT/$id/town.txt" || echo "(no verdict, see town.txt)"
      if [ "$memory" = mindvault ]; then
        "$PY" "$HERE/intern_probe.py" --log "$OUT/$id/mv-calls.jsonl" --out "$OUT/$id/probe.json"
        cp "$MINDVAULT_DIR/logs/events.jsonl" "$OUT/$id/mindvault-events.jsonl"
        stop_mv
      fi
    done
  done
done

"$PY" "$HERE/summarize.py" "$OUT" > "$OUT/results.csv"
cat "$OUT/results.csv"
