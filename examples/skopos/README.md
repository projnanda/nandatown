# SKOPOS: whole-prototype conjunction triage in Nanda Town

MIT AI Studio Fall 2026 Homework 8. This integrates the existing SKOPOS browser prototype, not Guardian Veritas and not a reimplementation of its assessment function. Two scripted participants exchange actual Town mailbox messages. A separate bridge drives the unchanged HTML in real headless Chrome, reading the same queue and explanations shown to an operator.

## Reproduce

Base: Nanda Town main `6f9a7c064486c4a0ae9242f1ff341b8a4aad0244`.
Requirements: Python 3.11+, Node, installed Google Chrome, separately supplied trusted `SKOPOS_repaired.html` from the team's HW7 project. Supply the expected source SHA-256 privately via `--source-sha256`; this public contribution does not disclose the private artifact fingerprint. Obtain that expected value from your trusted project record, rather than treating a newly observed hash as independent verification. The full private project is intentionally not copied into this contribution.

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]' 'httpx[socks]'
npm --prefix examples/skopos ci --ignore-scripts --no-audit --no-fund
python examples/skopos/run_check.py --skopos-html /absolute/path/SKOPOS_repaired.html --source-sha256 "$SKOPOS_EXPECTED_SHA256" --out /tmp/skopos-result.json
python -m pytest tests/test_skopos_example.py -q
git diff --check
```

No model key, cloud service, public registration, deployment or paid API is required. macOS execution was tested with Python 3.14, Node 24.10 and Playwright 1.55.1 using installed Chrome. Browser process creation must be permitted. Logs and screenshot are local outputs, not uploaded automatically.

## Workflow and assertions

1. Operator discovers SKOPOS capabilities through Town.
2. Request renders the actual 20-warning synthetic queue.
3. Real Sort by SKOPOS control orders urgent review, uncertainty resolution, monitoring.
4. Selecting CDM-1047 explains uncertainty and recommends another track.
5. CDM-1051 shows the unresolved close-deadline escalation.
6. Real New track control updates CDM-1047 from uncertainty to urgent human review; all other warnings remain unchanged.
7. Replay of the same logical request does not apply twice.
8. Stale revision, unknown warning and unsupported maneuver command are refused, leaving state unchanged.
9. Town itself accepts identical-message replay but rejects identity reuse with changed content.
10. Real Reset control restores the original fixture. Browser reports no JavaScript errors.

The revision, request replay and allowed-operation checks belong to this NEW bridge, not the historical prototype. Transport/browser failure is UNKNOWN, not falsely described as a rejected or undone action.

## Scope and limitations

This exercises the whole existing browser-only prototype: original data, assessment, sorting, rendering, explanations, new-track mutation and reset. It is not a live orbital collision-avoidance system. Data are fabricated; no maneuver is authorized or executed. Operators decide every maneuver. The participant is deterministic, not an LLM. No clinical, orbital-safety or commercial performance claim follows from this test.

Current Town's profile schema requires a quote task, so the test uses a zero-value placeholder envelope and custom mailbox messages/assertions. It does NOT claim a native stock evaluator pass or TOWN-TESTED badge. The private HTML is required for full reproduction; the public bridge contract tests alone do not establish whole-system success. Cross-host discovery, crash recovery, a second operator reproduction and live feeds remain untested. Malformed raw data import is not exposed: the prototype's formatter is not safe for every malformed input even though its assessment function is more defensive.

## Verified local result (2026-11-12 UTC)

14/14 real-browser workflow checks passed; 10/10 public bridge tests passed. Screenshot and result JSON capture the unchanged source fingerprint and actual observations. Initial sandbox run of the upstream suite: 1864 passed, 5 signal-process failures, 1 skipped. All 14 signal-process tests subsequently passed outside that sandbox. A fresh complete-suite rerun is tracked separately; do not infer global success from the focused tests.

Final complete-suite rerun outside the process-restricting sandbox: **1875 passed, 8 warnings, 203.88 seconds**. This includes the 10 new public bridge tests. The complete local log is retained in the course review package, not committed here. Full-suite green is separate from the 14-check real-browser report.
Expected/observed queue counts: initial 5 urgent / 7 uncertain / 8 monitor; after fresh track 6 urgent / 6 uncertain / 8 monitor. Reset restores 5/7/8.
