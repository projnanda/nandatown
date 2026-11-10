#!/usr/bin/env bash
# Run every JANG scenario against a live JANG server, verify each bundle with
# this example's validators loaded, keep the bundles under evidence/, and write
# evidence/summary.json.
#
#   JANG_URL=http://127.0.0.1:3939 bash examples/jang/run_all.sh            # rule strategy only
#   JANG_URL=http://127.0.0.1:3939 bash examples/jang/run_all.sh --claude   # plus the Claude run
#
# Expected: jang_market, jang_mandate_violation, jang_tampered_agreement and
# jang_no_zopa PASSED; jang_tampered_agreement_unverified FAILED on purpose.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
PY="${PYTHON:-python}"
OUT="$(mktemp -d)"
EVIDENCE="$HERE/evidence"
SCENARIOS=(jang_market jang_mandate_violation jang_tampered_agreement
           jang_tampered_agreement_unverified jang_no_zopa)
[[ "${1:-}" == "--claude" ]] && SCENARIOS+=(jang_claude)

"$PY" - "${JANG_URL:-http://127.0.0.1:3939}" <<'EOF'
import sys, httpx
try:
    assert httpx.get(sys.argv[1] + "/api/health", timeout=5).json()["ok"]
except Exception as exc:
    sys.exit(f"JANG is not answering at {sys.argv[1]} ({exc}); start it with `npm run dev`")
EOF

mkdir -p "$EVIDENCE"
for s in "${SCENARIOS[@]}"; do
  echo "== $s"
  before=$(ls "$OUT")
  # A relative scenario path keeps the recorded rerun command free of local paths.
  (cd "$ROOT" && nandatown run "examples/jang/$s.yaml" --out "$OUT") > "$OUT/$s.log" 2>&1 || true
  bundle=$(comm -13 <(echo "$before") <(ls "$OUT") | grep '^sim-' | head -1)
  grep -E "Verdict" "$OUT/$s.log"
  "$PY" "$HERE/verify_bundle.py" "$OUT/$bundle"
  rm -rf "${EVIDENCE:?}/$s"
  cp -R "$OUT/$bundle" "$EVIDENCE/$s"
done

"$PY" - "$EVIDENCE" "$ROOT" "${SCENARIOS[@]}" <<'EOF'
import json, os, subprocess, sys
evidence, root, names = sys.argv[1], sys.argv[2], sys.argv[3:]
town = subprocess.run(["git", "-C", root, "rev-parse", "--short", "HEAD"],
                      capture_output=True, text=True).stdout.strip()
summary = {"town_commit": town, "jang_commit": os.environ.get("JANG_COMMIT", "4fb4eca"),
           "signer": os.environ.get("JANG_SIGNER", "python"), "runs": {}}
existing = os.path.join(evidence, "summary.json")
if os.path.exists(existing):
    summary["runs"] = json.load(open(existing)).get("runs", {})
for name in names:
    d = os.path.join(evidence, name)
    result = json.load(open(os.path.join(d, "result.json")))
    manifest = json.load(open(os.path.join(d, "manifest.json")))
    summary["runs"][name] = {
        "run_id": result["run_id"], "verdict": result["verdict"],
        "bundle_fingerprint": manifest["bundle_fingerprint"],
        "evaluator_version": result["evaluator_version"],
        "stages": {s["name"]: s["status"] for s in result["stages"]},
    }
json.dump(summary, open(existing, "w"), indent=1)
for name, run in summary["runs"].items():
    print(f"{name:40s} {run['verdict']:10s} {run['bundle_fingerprint'][:23]}")
EOF
