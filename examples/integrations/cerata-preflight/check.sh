#!/usr/bin/env bash
# Cerata x Nanda Town compatibility check.
#
# Cerata (hosted separately) plays the Track seller through cerata_seller.py;
# agent_buyer.py plays an agent that must ask before it acts. Each case is one
# Track run whose verdict is compared with the expected one.
#
#   CERATA_URL=https://... CERATA_API_KEY=ak_test_... \
#     examples/integrations/cerata-preflight/check.sh [OUT_DIR]
#
# Sections:
#   law      every case in cases.json on quote-clean (22 runs)
#   faults   Town delivery and crash faults on a block case and a clear case
#   control  negative control and failure/boundary cases
#   repeat   the listing case again, CERATA_REPEATS times (more than one cautious answer is acceptable)
#   mcp      the same service through its MCP endpoint
# CERATA_SECTIONS picks a subset (default: all). CERATA_FIXTURE=1 starts the
# local fake_cerata.py instead (a wiring rehearsal, NOT compatibility evidence).
# Exit status: 0 when every case matched its expected verdict.
set -uo pipefail
cd "$(dirname "$0")/../../.."
HERE=examples/integrations/cerata-preflight
OUT=${1:-runs/cerata}
mkdir -p "$OUT"
OUT=$(cd "$OUT" && pwd)
SECTIONS=${CERATA_SECTIONS:-law faults control repeat mcp}
REPEATS=${CERATA_REPEATS:-3}

if [ "${CERATA_FIXTURE:-0}" = "1" ]; then
  python "$HERE/fake_cerata.py" --port 8971 > "$OUT/fake_cerata.log" 2>&1 &
  FAKE_PID=$!; trap 'kill $FAKE_PID 2>/dev/null' EXIT
  export CERATA_URL=http://127.0.0.1:8971 CERATA_API_KEY=ak_test_fixture_local
  sleep 1
  echo "FIXTURE MODE: answers come from cases.json, not from Cerata"
fi
: "${CERATA_URL:=https://clearance-kohl.vercel.app}"
export CERATA_URL
if [ -z "${CERATA_API_KEY:-}" ]; then echo "set CERATA_API_KEY (a Cerata test key)" >&2; exit 2; fi
case "$CERATA_API_KEY" in ak_test_*|clr_test_*) ;; *) echo "use a test key (ak_test_...), not a live key" >&2; exit 2 ;; esac
curl -sf -m 15 "$CERATA_URL/healthz" >/dev/null || { echo "Cerata is not answering at $CERATA_URL/healthz" >&2; exit 2; }

mkdir -p "$OUT/observations"
SELLER="python $HERE/cerata_seller.py"
BUYER="python $HERE/agent_buyer.py"
SUMMARY="$OUT/summary.tsv"
printf "section\tcase\tprofile\texpected\tobserved\tmatch\tseconds\tbundle\n" > "$SUMMARY"
FAILED=0

run_case() {  # section name profile expected [VAR=value...] -- nandatown args...
  local section=$1 name=$2 profile=$3 expected=$4; shift 4
  local envs=()
  while [ "$1" != "--" ]; do envs+=("$1"); shift; done; shift
  local log="$OUT/logs/$section-$name-$profile.log"; mkdir -p "$OUT/logs"
  local t0=$SECONDS
  env CERATA_OBS_DIR="$OUT/observations/$section" "${envs[@]}" nandatown run "$profile" "$@" --out "$OUT/bundles" > "$log" 2>&1
  local secs=$((SECONDS - t0)) observed bundle match=yes
  observed=$(sed -n 's/^Verdict: *//p' "$log" | head -1)
  bundle=$(sed -n 's/^Evidence bundle: *//p' "$log" | head -1)
  if [ "$expected" = "NOT_PASSED" ]; then
    case "$observed" in FAILED|INCOMPLETE) ;; *) match=NO; FAILED=1 ;; esac
  elif [ "$observed" != "$expected" ]; then match=NO; FAILED=1; fi
  printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n" "$section" "$name" "$profile" "$expected" "${observed:-none}" "$match" "$secs" "$(basename "${bundle:-none}")" >> "$SUMMARY"
  printf "%-8s %-44s %-26s expected %-10s observed %-10s %s\n" "$section" "$name" "$profile" "$expected" "${observed:-none}" "$match"
}
both=(--agent "seller=cmd:$SELLER" --agent "buyer=cmd:$BUYER")
has() { case " $SECTIONS " in *" $1 "*) return 0 ;; *) return 1 ;; esac; }
CASES=$(python -c "import json;print(' '.join(c['id'] for c in json.load(open('$HERE/cases.json'))['cases']))")
LISTING=$(python -c "import json;print(' '.join(c['id'] for c in json.load(open('$HERE/cases.json'))['cases'] if c['agent']=='leasing'))")

# 1. The law: every case, one run each. A repeat case runs after the case it repeats.
if has law; then
  for c in $CASES; do run_case law "$c" quote-clean PASSED "CERATA_CASE=$c" -- "${both[@]}"; done
fi

# 2. Town's faults on a block case and a clear-with-token case.
if has faults; then
  for c in fl-call-after-8pm fl-clean-call; do
    for p in quote-duplicate-delivery quote-lost-ack quote-drop-wakeup quote-crash-restart quote-amnesia-restart; do
      run_case faults "$c" "$p" PASSED "CERATA_CASE=$c" -- "${both[@]}"
    done
  done
fi

# 3. Controls and failure/boundary cases.
if has control; then
  # Negative control: without the Idempotency-Key a restarted seller gets a
  # second Cerata check (new check id and receipt) and answers differently
  # under the same message identity.
  run_case control naive-adapter-amnesia quote-amnesia-restart NOT_PASSED CERATA_ADAPTER_NAIVE=1 CERATA_CASE=fl-call-after-8pm -- "${both[@]}"
  # Failure case: Cerata unreachable. The only correct answer is no decision.
  run_case control cerata-unreachable quote-clean PASSED CERATA_URL=http://127.0.0.1:9 CERATA_VERIFY_URL=http://127.0.0.1:9 CERATA_EXPECT=unavailable CERATA_CASE=fl-clean-call -- "${both[@]}"
  # ...and its control: an agent that insists on a decision does not pass.
  run_case control cerata-unreachable-expects-decision quote-clean FAILED CERATA_URL=http://127.0.0.1:9 CERATA_VERIFY_URL=http://127.0.0.1:9 CERATA_CASE=fl-clean-call -- "${both[@]}"
  # Failure case: a rejected key fails closed the same way.
  run_case control bad-api-key quote-clean PASSED CERATA_API_KEY=ak_test_not_a_real_key_000 CERATA_EXPECT=unavailable CERATA_CASE=call-national-dnc -- "${both[@]}"
  # Boundary: Town's stock buyer asks for widgets; the adapter refuses
  # anything that is not a preflight request, so the run cannot complete.
  run_case control reference-buyer quote-clean INCOMPLETE -- --agent "seller=cmd:$SELLER"
fi

# 4. The listing again: the exact answer may vary, "never clear" may not.
if has repeat; then
  for i in $(seq 1 "$REPEATS"); do
    for c in $LISTING; do run_case repeat "$c#$i" quote-clean PASSED "CERATA_CASE=$c" -- "${both[@]}"; done
  done
fi

# 5. The same service through MCP.
if has mcp; then
  if python "$HERE/mcp_check.py" call-national-dnc "$OUT/mcp.json" > "$OUT/logs/mcp.log" 2>&1; then m=yes; o=PASSED; else m=NO; o=FAILED; FAILED=1; fi
  printf "mcp\tcall-national-dnc\tmcp_check.py\tPASSED\t%s\t%s\t-\tmcp.json\n" "$o" "$m" >> "$SUMMARY"
  printf "%-8s %-44s %-26s expected %-10s observed %-10s %s\n" mcp call-national-dnc mcp_check.py PASSED "$o" "$m"
fi

# Keep the API key out of anything shared.
if grep -rqF "$CERATA_API_KEY" "$OUT" 2>/dev/null; then echo "WARNING: the API key appears under $OUT; do not share it" >&2; FAILED=1; fi
total=$(($(wc -l < "$SUMMARY") - 1)); matched=$(awk -F'\t' 'NR>1 && $6=="yes"' "$SUMMARY" | wc -l)
echo; echo "summary: $SUMMARY ($matched of $total matched)"
[ $FAILED -eq 0 ] && echo "ALL CASES MATCHED" || echo "SOME CASES DID NOT MATCH"
exit $FAILED
