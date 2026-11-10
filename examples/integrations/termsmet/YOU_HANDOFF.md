# TermsMet HW8: You → Sheila handoff

## Status and versions

This branch contains the bridge, fixtures, runner framework, interface contract, and tests. It does **not** yet contain the formal Town plugin, clean/lost-reply scenarios, validators, formal evidence, project README, upstream PR, or Demo Day slide. A real TermsMet transaction was tested through the bridge, and the runner was exercised separately with a test-only plugin. Neither is a completed Town TermsMet transaction or proof of a Town message drop.

- Team fork: https://github.com/wywyou56/nandatown.git; branch `integration/termsmet-acceptance`. Use the handoff commit supplied with the handoff message; `git rev-parse HEAD` identifies your checked-out revision. Town base: `6f9a7c064486c4a0ae9242f1ff341b8a4aad0244`.
- Public runtime: https://github.com/wywyou56/termsmet-runtime.git, commit `7ee09295226d2a41555293f391e13a8c46020031`; original TermsMet source commit `e797d7cc8f3fdabd6ff3e2ceec90433e4e6b56be`. The public runtime's `SOURCE_PROVENANCE.json` records copied-file hashes.
- Both interpreters must be Python **3.11.5**; both MCP environments use `mcp==2.3.0`. `project_version.json` records the actual versions and settings. The runner checks these versions, the runtime commit, and tracked runtime changes.
- Sheila's GitHub username is `sheilahf860316`. Write access to the fork was **unverified** when this handoff was prepared; the GitHub integration returned 403 for the permission lookup. Confirm access and invitation acceptance before treating the shared-branch handoff as complete. The owner can use the fork's Settings → Collaborators and teams to add or confirm write access.

## Reconstruct and test

From a new parent directory, preserving any existing work, use a `python3.11` executable that reports **3.11.5**. The Town runner requires POSIX process inspection with `ps` and is supported on macOS/Linux. Local HTTP checks in Town's full suite require permission to bind loopback sockets.

```sh
python3.11 --version
git clone https://github.com/wywyou56/nandatown.git nandatown
cd nandatown
git switch integration/termsmet-acceptance
git rev-parse HEAD
cd ..
git clone https://github.com/wywyou56/termsmet-runtime.git termsmet-runtime
cd termsmet-runtime
git checkout 7ee09295226d2a41555293f391e13a8c46020031
python3.11 -m venv .venv
.venv/bin/python --version
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip check
cd ../nandatown
python3.11 -m venv .venv
.venv/bin/python --version
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pip install -r examples/integrations/termsmet/requirements.lock
.venv/bin/python -m pip check
.venv/bin/python -W error::ResourceWarning -m unittest discover -s tests -p 'test_termsmet_*.py' -v
git diff --check
```

The focused suite contains 23 tests with the pinned runtime present: eight bridge tests, three portability regressions, and twelve runner tests. The latest independent review reported **23 passed**. Earlier implementation runs separately reported 8 bridge tests, 3 portability tests and 12 runner tests passing. The clean Town `.[dev]` environment without MCP previously ran the full Town pytest suite: **1,866 passed, 3 skipped, 8 warnings**. In that environment the runner suite had 10 passes and 2 MCP-related skips; offline verification was also exercised without a runtime checkout. These are recorded results, not a claim that the formal scenarios have run. If a fresh run differs, record the command, versions, error and location of diagnostics; do not label a skipped real-runtime test as passed.

The bridge boundary test exercises real buyer/provider SDK sessions, version-2 agreement and simulated funding, a computed `MISSING_ORDER` A102 FAIL, repair PASS, rejection of an identical extra submission, state/report confirmation, review, one simulated release, and retrieval against the independent expected fixture. The real review deadline is two seconds. The test confirms release was not recorded before that deadline. The test-only `tests/termsmet_smoke_plugin.py` checks framework behavior and includes a connectivity smoke; it is not formal evidence.

## Interface and data

Read `interface.md` for the precise runner, plugin lifecycle and message contract. The bridge is internal, not a new server tool: `TermsMetBridge(project_dir, project_python, db_path, review_seconds=2, startup_timeout=20, call_timeout=10)` provides `start()`, `call(identity, tool, arguments, request_id=...)`, `records`, and `close()`. It starts two processes with server identities from `TERMSMET_IDENTITY`, sharing one SQLite file. It returns unchanged business dictionaries, including business errors; SDK/transport/startup failures raise categorized `BridgeError`. Queue time counts toward the call deadline; a queued timeout does not run later, and an in-flight timeout has an unknown outcome and closes the bridge. Records snapshot request arguments and results and can contain private data: expose them only to the observer/validator and review before publication.

The runner passes only `TERMSMET_PROJECT_DIR`, `TERMSMET_PROJECT_PYTHON`, `TERMSMET_DB_PATH`, and `TERMSMET_REVIEW_SECONDS` as plugin context, creates a fresh database and isolated `NANDATOWN_HOME`, and uses seed 42. Each live run has a 120-second real-time cap; startup and individual calls are bounded at 20 and 10 seconds. Review polling should be at most every 0.1 seconds and bounded by the run deadline. Town `later(2)` advances logical time and cannot substitute for two real seconds. Cleanup requests orderly exit, then force-stops tracked survivors after five seconds and waits another five; forced termination or unconfirmed cleanup fails the run. Tracking depends on `ps`; a process that escapes observation is outside the guarantee.

The fixtures are `fixtures/orders.csv` (including the final newline), `fixtures/initial_delivery.json` (A101 and A103), and `fixtures/expected_delivery.json` (independently hand-computed, validator-only). Expected totals: 3 orders, 6 units, 3,560 cents. Provider repair must use the actual FAIL report and agreed CSV, not the expected fixture or server checker/parser. Monetary conversion uses Decimal and ROUND_HALF_UP. All funding, release, and refund events are simulated; this is not a payment service.

Roles are `tm_buyer`, `tm_provider`, and `tm_adapter`. Buyer/provider send `tm_tool_request` with `request_id`, `tool`, `arguments`, and `purpose`; the adapter maps the Town sender to a fixed bridge identity. Results use `tm_tool_result`, with business errors inside `result` and transport failures separately. The first repair response uses `tm_repair_result`; lost-reply YAML must drop its first occurrence. Provider marks the request `unconfirmed`, retries the identical delivery once with a new request ID, then queries the job and submission 2 report before confirming PASS. `unconfirmed` is not checker `UNKNOWN`; a rejected retry is not proof the first submission failed. No role reads bridge observer records or expected fixtures.

## Sheila's remaining steps

1. **S1 — Reconstruct.** Confirm fork write access, check out the exact handoff commit, use the commands above and read `interface.md` and `project_version.json`. Record actual results. Contact You only for a concrete bridge/service contract defect with a minimal reproduction.
2. **S2 — Roles.** Implement `termsmet_plugin.py` with the scripted buyer, provider, and adapter. Register `termsmet_clean` and `termsmet_lost_reply` validators and provide `close_termsmet()`; importing the plugin for verification must not import MCP or start a bridge. Roles advance only from Town messages they actually receive. Keep callbacks bounded and avoid waiting for another Town message inside a callback.
3. **S3 — Scenarios and validators.** Add `termsmet_workflow.yaml`, `termsmet_lost_reply.yaml`, and integration tests. Use the current Town ScenarioSpec, no `plugin_files`, and the validator IDs in `interface.md`. The clean sequence is proposal v1 → early buyer retrieval denied → provider counter to 10 minutes/v2 → both accept v2 → stale v1 acceptance and locked counter rejected → simulated funding → partial submission FAIL → report-driven repair PASS → real review → one simulated release → buyer retrieval. Lost-reply uses the same sequence but drops the first `tm_repair_result` through Town transport; prove the dropped message was the actual submission-2 PASS, then prove the provider's unconfirmed state, exactly one rejected retry, subsequent state/report query and confirmed PASS. Either IN_REVIEW or RELEASED is acceptable at retry; do not create another job or submission.
4. **S4 — Run and verify.** Run clean and lost-reply with fresh output directories and databases. Preserve failed attempts. Validate the four required stages (`agreement_funding`, `delivery_repair`, `release_gating`, `retrieved_artifact`) and the lost-reply-only `lost_reply_recovery` stage; missing evidence must not pass. Confirm receipt agreement/source/checker versions, exactly two submissions (FAIL/PASS), one simulated funding and release, no refund, and release timestamp at or after the real review deadline. Compare the retrieved artifact exactly with the independent fixture. Review public evidence for private paths, secrets and logs before publication; do not edit signed bundle records after creation. Reconstruct in another directory and run offline verification with the formal plugin loaded and no server path, plus live reproduction separately. Run relevant tests, Town's `python -m pytest -q`, and `git diff --check`; run distribution checks if packaging resources change.
5. **S5 — Document and submit.** Add a project README with exact anonymous dependencies, commands, fingerprints, expected/actual results, clock and observer limits. Open one authorized PR from the team fork to current `projnanda/nandatown:main`, titled `TermsMet: add MCP acceptance workflow and lost-reply recovery check`, with Summary, Integration, Reproduce, Results and limitations, and Course context (MIT AI Studio Fall 2026, HW8). Obtain the actual shared Demo Day deck through the course page; add exactly one TermsMet slide while preserving the blank template. Each member submits the same actual open PR URL and truthful slide confirmation in Canvas. Do not claim those steps are complete until observed.

## Boundaries

The participants are scripted; this does not test LLM decisions, independent user adoption, full NANDA/A2A compliance, real payments, production cross-organization identity isolation, subjective arbitration, or all crash/concurrency cases. Observer records are not independent proof of server internals. The first delivery is an explicitly controlled fault injection; verdicts and release come from the real server. No model API key is needed. The runner's formal commands intentionally report missing files until S2/S3 are implemented. Preserve the existing server defaults and the separate HW7 freeze.
