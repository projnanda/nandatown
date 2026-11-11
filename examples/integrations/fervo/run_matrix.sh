#!/usr/bin/env bash
# Run the Fervo redelivery matrix: one Town Track run per row, each against a
# freshly reset Fervo server, cross-checked with check.py, and copied into
# evidence/<row>/.
#
#   run_matrix.sh [--lima | --local] [row ...]
#
# Rows: clean duplicate-delivery lost-ack crash-restart, and the negative
# controls fresh-keys-crash fresh-keys-duplicate (default: all six). Needs `nandatown` on PATH and the setup_fervo.sh inputs
# (FERVO_TGZ, or FERVO_CLI with --local). The VM is left running; finish with
# teardown_fervo.sh.
set -uo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
MODE=--lima
if [ "${1:-}" = --lima ] || [ "${1:-}" = --local ]; then MODE=$1; shift; fi
ROWS=${*:-clean duplicate-delivery lost-ack crash-restart fresh-keys-crash fresh-keys-duplicate}
WORK=${FERVO_WORK:-${TMPDIR:-/tmp}/fervo-hw8}
WORK=${WORK%/}
export FERVO_WORK=$WORK
RUNS=${TOWN_RUNS:-$WORK-runs}
EVIDENCE=${FERVO_EVIDENCE:-$HERE/evidence}
TIMEOUT=${TOWN_TIMEOUT:-60}
PYTHON=${PYTHON:-python3}
mkdir -p "$RUNS" "$EVIDENCE"

profile_of() {
  case "$1" in
    clean) echo quote-clean ;;
    duplicate-delivery|fresh-keys-duplicate) echo quote-duplicate-delivery ;;
    lost-ack) echo quote-lost-ack ;;
    crash-restart|fresh-keys-crash) echo quote-crash-restart ;;
    *) echo "unknown row $1" >&2; return 1 ;;
  esac
}

status=0
for row in $ROWS; do
  profile=$(profile_of "$row") || exit 2
  keys=derived; case "$row" in fresh-keys-*) keys=fresh ;; esac
  echo "=== $row: profile $profile, keys $keys ($MODE)"
  bash "$HERE/setup_fervo.sh" "$MODE" || exit 1
  # shellcheck disable=SC1091
  . "$WORK/env.sh"

  log=$RUNS/$row.log
  FERVO_KEYS=$keys nandatown test-agent --role seller --profile "$profile" \
    --timeout "$TIMEOUT" --out "$RUNS" \
    --cmd "$PYTHON $HERE/fervo_contributor.py" | tee "$log"
  town_exit=${PIPESTATUS[0]}
  bundle=$(sed -n 's/^Evidence bundle: //p' "$log" | tail -1)
  [ -n "$bundle" ] || { echo "no bundle for $row" >&2; status=1; continue; }

  out=$EVIDENCE/$row
  rm -rf "$out" && mkdir -p "$out/bundle"
  "$PYTHON" "$HERE/check.py" "$bundle" --out "$out/fervo_check.json" \
    --events-out "$out/fervo_events.jsonl" | tee "$out/fervo_check.txt"
  check_exit=${PIPESTATUS[0]}

  nandatown verify "$bundle" > "$out/verify.txt" 2>&1
  nandatown receipt "$bundle" > "$RUNS/$row.receipt.log" 2>&1
  for f in profile.json run.json intents.jsonl events.jsonl result.json \
           manifest.json attestation.json receipt.json; do
    [ -f "$bundle/$f" ] && cp "$bundle/$f" "$out/bundle/"
  done
  # The seller's own log has no secrets; state/ as a whole is not shared.
  log_file=$(find "$bundle/state" -name fervo_contributor.log 2>/dev/null | head -1)
  [ -n "$log_file" ] && cp "$log_file" "$out/fervo_contributor.log"
  cp "$WORK/versions.txt" "$out/versions.txt"
  printf 'row=%s\nprofile=%s\nkeys=%s\ntown_exit=%s\ncheck_exit=%s\n' \
    "$row" "$profile" "$keys" "$town_exit" "$check_exit" > "$out/outcome.txt"

  expected_check=0; [ "$keys" = fresh ] && expected_check=1
  if [ "$check_exit" != "$expected_check" ]; then
    echo "!!! $row: check.py exited $check_exit, expected $expected_check" >&2
    status=1
  fi
done
exit $status
