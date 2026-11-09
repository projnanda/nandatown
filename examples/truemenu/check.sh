#!/usr/bin/env bash
# TrueMenu x Nanda Town compatibility check.
#
# Runs TrueMenu (the seller, through truemenu_seller.py) and a lunch
# buyer (lunch_buyer.py) as two separate Track participants and compares
# each run's verdict with the expected one. TrueMenu itself runs outside
# Town; see examples/truemenu/README.md for setup.
#
#   TRUEMENU_URL=http://127.0.0.1:8088 TRUEMENU_AGENT_KEY=... \
#   TRUEMENU_HOME=~/truemenu examples/truemenu/check.sh [OUT_DIR]
#
# TRUEMENU_HOME is needed only for the stale-data case, which pauses and
# resumes TrueMenu's simulated POS sources with its ./feed command.
# Exit status: 0 when every case matched its expected verdict.
set -uo pipefail
cd "$(dirname "$0")/../.."
OUT=${1:-runs/truemenu}
mkdir -p "$OUT"
: "${TRUEMENU_URL:=http://127.0.0.1:8088}"
export TRUEMENU_URL
if [ -z "${TRUEMENU_AGENT_KEY:-}" ]; then
  echo "set TRUEMENU_AGENT_KEY (a TrueMenu key with the read scope)" >&2; exit 2
fi
curl -sf "$TRUEMENU_URL/health" >/dev/null || { echo "TrueMenu is not answering at $TRUEMENU_URL/health" >&2; exit 2; }

wait_for_quotes() {  # TrueMenu needs a fresh sync before it signs quotes again
  local i
  for i in $(seq 1 30); do
    if curl -sf -X POST "$TRUEMENU_URL/v1/find_items" -H "Content-Type: application/json" \
         -H "X-Agent-Key: $TRUEMENU_AGENT_KEY" \
         -d '{"near":"Kendall Square","available_at":"2026-11-16T12:30","limit":1}' \
       | grep -q '"quote_id"'; then return 0; fi
    sleep 2
  done
  echo "TrueMenu is not issuing quotes (are its sources stale or paused?)" >&2; return 1
}
wait_for_quotes || exit 2

SELLER="python examples/truemenu/truemenu_seller.py"
BUYER="python examples/truemenu/lunch_buyer.py"
SUMMARY="$OUT/summary.tsv"
printf "case\tprofile\texpected\tobserved\tmatch\tbundle\n" > "$SUMMARY"
FAILED=0

run_case() {  # name profile expected_verdict [env assignments...] -- nandatown args...
  local name=$1 profile=$2 expected=$3; shift 3
  local envs=()
  while [ "$1" != "--" ]; do envs+=("$1"); shift; done; shift
  local log="$OUT/$name.log"
  env "${envs[@]}" nandatown run "$profile" "$@" --out "$OUT" > "$log" 2>&1
  local observed bundle
  observed=$(sed -n 's/^Verdict: *//p' "$log" | head -1)
  bundle=$(sed -n 's/^Evidence bundle: *//p' "$log" | head -1)
  local match=yes
  if [ "$expected" = "NOT_PASSED" ]; then
    case "$observed" in FAILED|INCOMPLETE) ;; *) match=NO; FAILED=1 ;; esac
  elif [ "$observed" != "$expected" ]; then
    match=NO; FAILED=1
  fi
  printf "%s\t%s\t%s\t%s\t%s\t%s\n" "$name" "$profile" "$expected" "${observed:-none}" "$match" "${bundle:-none}" >> "$SUMMARY"
  printf "%-26s %-26s expected %-10s observed %-10s %s\n" "$name" "$profile" "$expected" "${observed:-none}" "$match"
}

both=(--agent "seller=cmd:$SELLER" --agent "buyer=cmd:$BUYER")

# 1. The workflow: a verified lunch quote, judged by the buyer.
run_case clean_quote          quote-clean              PASSED -- "${both[@]}"
# 2. Town's delivery faults applied to TrueMenu's seller.
run_case duplicate_delivery   quote-duplicate-delivery PASSED -- "${both[@]}"
run_case lost_ack             quote-lost-ack           PASSED -- "${both[@]}"
run_case dropped_wakeup       quote-drop-wakeup        PASSED -- "${both[@]}"
# 3. Negative control: an adapter without a journal calls TrueMenu again on
#    redelivery, gets a newly signed quote, and the town refuses the changed
#    reply under the same identity.
#    Depending on timing the town records a second application (FAILED) or
#    never hears the duplicate acknowledged (INCOMPLETE); both are not PASSED.
run_case naive_adapter_dup    quote-duplicate-delivery NOT_PASSED TRUEMENU_ADAPTER_NAIVE=1 -- "${both[@]}"
# 4. Boundary: Town's reference buyer asks for 2 widgets and expects a
#    total_cents field; TrueMenu declines the unsupported request.
run_case reference_buyer      quote-clean              INCOMPLETE -- --agent "seller=cmd:$SELLER"
# 5. Failure case: every TrueMenu source is stale, so no quote may be issued
#    and the correct answer is a decline.
if [ -n "${TRUEMENU_HOME:-}" ]; then
  for m in cava-kendall area-four-kendall row34-kendall; do
    "$TRUEMENU_HOME/feed" pause-source "$m" >/dev/null
  done
  echo "waiting 35 s for the paused sources to pass the stale cut (2 x 15 s sync interval)"
  sleep 35
  run_case stale_sources_decline quote-clean           PASSED TRUEMENU_EXPECT=decline -- "${both[@]}"
  run_case stale_sources_expect  quote-clean           FAILED TRUEMENU_EXPECT=quote -- "${both[@]}"
  "$TRUEMENU_HOME/feed" clear >/dev/null
  for m in cava-kendall area-four-kendall row34-kendall; do
    "$TRUEMENU_HOME/feed" resume-source "$m" >/dev/null
  done
  wait_for_quotes || echo "warning: TrueMenu sources did not recover after the stale case" >&2
else
  echo "TRUEMENU_HOME not set: skipping the stale-data case"
fi

echo; echo "summary: $SUMMARY"
[ $FAILED -eq 0 ] && echo "ALL CASES MATCHED" || echo "SOME CASES DID NOT MATCH"
exit $FAILED
