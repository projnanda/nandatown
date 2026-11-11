#!/usr/bin/env bash
# Reproduce the GridPact compatibility check against one GridPact checkout.
#   GRIDPACT_DIR=/path/to/gridpact [GRIDPACT_PYTHON=python] [VARIANT=honest|liar] examples/gridpact/run_check.sh [out_dir]
# For each of Town's two Path profiles it starts a fresh GridPact server (synthetic fixture data, throwaway
# database) and the A2A adapter, runs the profile, stops both, and shows what GridPact recorded. Fresh state per
# profile because each profile sends its own order for the same seller and window.
# VARIANT=liar fills the orders with PVB_37, which declares 5000 kW for a 2500 kW battery: the adapter's auditor
# must challenge it and GridPact must settle the order from the trimmed commitment. The liar's orders are packed
# into 2 steps (1000 kWh/step) so they exceed its real 2500 kW battery (625 kWh/step) but fit its declared cap.
# LLM=1 adds --llm: a Claude Haiku agent decides the seller's accept and the auditor's challenge (needs ANTHROPIC_API_KEY).
# Run it from the nandatown checkout with Town installed (nandatown on PATH).
set -u
: "${GRIDPACT_DIR:?set GRIDPACT_DIR to a gridpact checkout}"
GP_PY="${GRIDPACT_PYTHON:-python}"
OUT="${1:-runs/gridpact-check}"
HERE="$(cd "$(dirname "$0")" && pwd)"
VARIANT="${VARIANT:-honest}"
MCP_PORT="${MCP_PORT:-8792}"; A2A_PORT="${A2A_PORT:-8941}"
DATA="${GRIDPACT_DATA_DIR:-$GRIDPACT_DIR/fixtures/synthetic}"
if [ "$VARIANT" = liar ]; then SELLER_ARGS="--seller PVB_37 --declared-flex-kw 5000 --steps 2"; else SELLER_ARGS="--seller PVB_17"; fi
[ "${LLM:-}" = 1 ] && SELLER_ARGS="$SELLER_ARGS --llm"
cleanup() { kill "${GP_PID:-}" "${AD_PID:-}" 2>/dev/null; wait 2>/dev/null; }
trap cleanup EXIT

echo "variant: $VARIANT"
echo "GridPact: $(cd "$GRIDPACT_DIR" && git describe --tags --always 2>/dev/null)   idempotent propose: $(grep -c request_id "$GRIDPACT_DIR/gridpact/server.py" | sed 's/^0$/no/;s/^[1-9].*/yes/')"
for P in a2a-capability-fulfillment@0.3 a2a-order-conflict@0.1; do
  TMP="$(mktemp -d)"; KEY="check-$$-$RANDOM"
  ( cd "$GRIDPACT_DIR" && GRIDPACT_DATA_DIR="$DATA" GRIDPACT_DB="$TMP/gridpact.db" GRIDPACT_PORT="$MCP_PORT" \
      GRIDPACT_OPERATOR_KEY="$KEY" exec "$GP_PY" -m gridpact.server ) >"$TMP/gridpact.log" 2>&1 &
  GP_PID=$!
  for _ in $(seq 40); do (echo >"/dev/tcp/127.0.0.1/$MCP_PORT") 2>/dev/null && break; sleep 0.5; done
  GRIDPACT_OPERATOR_KEY="$KEY" python "$HERE/gridpact_a2a.py" --port "$A2A_PORT" --mcp-url "http://127.0.0.1:$MCP_PORT/mcp" \
      $SELLER_ARGS >"$TMP/adapter.log" 2>&1 &
  AD_PID=$!
  for _ in $(seq 40); do curl -sf "http://127.0.0.1:$A2A_PORT/.well-known/agent-card.json" >/dev/null && break; sleep 0.5; done
  echo; echo "== $P"
  curl -sf "http://127.0.0.1:$A2A_PORT/.well-known/agent-card.json" >/dev/null || { echo "adapter did not start:"; tail -5 "$TMP/adapter.log"; tail -5 "$TMP/gridpact.log"; }
  nandatown test-agent --url "http://127.0.0.1:$A2A_PORT" --path-profile "$P" --out "$OUT" 2>&1 \
    | grep -E "Verdict|^  [a-z_]+ +(Passed|Failed|Not tested|Inconclusive)|note:|Evidence bundle"
  [ "${LLM:-}" = 1 ] && { echo "-- Haiku decisions"; grep -E "^(haiku|adapter error)" "$TMP/adapter.log" | cut -c1-230; }
  echo "-- what GridPact recorded"
  python - "$TMP/gridpact.db" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1]); c.row_factory = sqlite3.Row
print("offers:", c.execute("select count(*) from offers").fetchone()[0],
      "| commitments:", c.execute("select count(*) from commitments").fetchone()[0],
      "| stake locked now: $%.2f" % c.execute("select coalesce(sum(stake_locked_usd),0) from agents").fetchone()[0])
print("ledger:", dict(c.execute("select action, count(*) from ledger group by action").fetchall()))
for r in c.execute("select commitment_id, seller, kwh_per_step, trimmed_from_kwh, status from commitments"):
    print(f"commitment {r['commitment_id']}: {r['seller']} {r['kwh_per_step']:.0f} kWh/step (ordered {r['trimmed_from_kwh'] or r['kwh_per_step']:.0f}), {r['status']}")
for r in c.execute("select commitment_id, verdict, bounty_usd from challenges"):
    print(f"challenge on commitment {r['commitment_id']}: {r['verdict']}, bounty ${r['bounty_usd']:.2f}")
for r in c.execute("select commitment_id, sum(committed_kwh) com, sum(delivered_kwh) dlv, sum(shortfall_kwh) sh, sum(penalty_usd) pen from settlements group by commitment_id"):
    print(f"settled commitment {r['commitment_id']}: committed {r['com']:.0f} kWh, delivered {r['dlv']:.0f}, shortfall {r['sh']:.0f}, penalty ${r['pen']:.2f}")
PY
  kill "$GP_PID" "$AD_PID" 2>/dev/null; wait "$GP_PID" "$AD_PID" 2>/dev/null; sleep 1
done
