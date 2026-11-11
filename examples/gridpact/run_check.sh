#!/usr/bin/env bash
# Reproduce the GridPact compatibility check against one GridPact checkout.
#   GRIDPACT_DIR=/path/to/gridpact [GRIDPACT_PYTHON=python] examples/gridpact/run_check.sh [out_dir]
# Starts a fresh GridPact server (synthetic fixture data, throwaway database), starts the A2A adapter,
# runs Town's two Path profiles against it, verifies the bundles, then stops both and shows what GridPact
# recorded. Run it from the nandatown checkout with Town installed (nandatown on PATH).
set -u
: "${GRIDPACT_DIR:?set GRIDPACT_DIR to a gridpact checkout}"
GP_PY="${GRIDPACT_PYTHON:-python}"
OUT="${1:-runs/gridpact-check}"
HERE="$(cd "$(dirname "$0")" && pwd)"
TMP="$(mktemp -d)"; MCP_PORT="${MCP_PORT:-8792}"; A2A_PORT="${A2A_PORT:-8941}"
DATA="${GRIDPACT_DATA_DIR:-$GRIDPACT_DIR/fixtures/synthetic}"
cleanup() { kill "${GP_PID:-}" "${AD_PID:-}" 2>/dev/null; }
trap cleanup EXIT

( cd "$GRIDPACT_DIR" && GRIDPACT_DATA_DIR="$DATA" GRIDPACT_DB="$TMP/gridpact.db" GRIDPACT_PORT="$MCP_PORT" \
    exec "$GP_PY" -m gridpact.server ) >"$TMP/gridpact.log" 2>&1 &
GP_PID=$!
for _ in $(seq 40); do (echo >"/dev/tcp/127.0.0.1/$MCP_PORT") 2>/dev/null && break; sleep 0.5; done
python "$HERE/gridpact_a2a.py" --port "$A2A_PORT" --mcp-url "http://127.0.0.1:$MCP_PORT/mcp" >"$TMP/adapter.log" 2>&1 &
AD_PID=$!
for _ in $(seq 40); do curl -sf "http://127.0.0.1:$A2A_PORT/.well-known/agent-card.json" >/dev/null && break; sleep 0.5; done

echo "GridPact: $(cd "$GRIDPACT_DIR" && git describe --tags --always 2>/dev/null)   idempotent propose: $(grep -c request_id "$GRIDPACT_DIR/gridpact/server.py" | sed 's/^0$/no/;s/^[1-9].*/yes/')"
for P in a2a-capability-fulfillment@0.3 a2a-order-conflict@0.1; do
  echo; echo "== $P"
  nandatown test-agent --url "http://127.0.0.1:$A2A_PORT" --path-profile "$P" --out "$OUT" 2>&1 \
    | grep -E "Verdict|^  [a-z_]+ +(Passed|Failed|Not tested|Inconclusive)|note:|Evidence bundle"
done
echo; echo "== what GridPact recorded"
python - "$TMP/gridpact.db" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
print("offers:", c.execute("select count(*) from offers").fetchone()[0],
      "| commitments:", c.execute("select count(*) from commitments").fetchone()[0],
      "| stake locked:", c.execute("select coalesce(sum(stake_usd),0) from commitments").fetchone()[0])
print("ledger:", dict(c.execute("select action, count(*) from ledger group by action").fetchall()))
PY
