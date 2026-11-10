# TermsMet Town integration interface

Y4 provided the runner and bridge scaffolding. Stage two added the formal plugin (`termsmet_plugin.py`), both scenarios, validators and recorded evidence; see `README.md` for results.

## Files and lifecycle

Place `termsmet_plugin.py`, `termsmet_workflow.yaml` (clean), and `termsmet_lost_reply.yaml` (lost-reply) in this directory. The plugin must register both Town validators `termsmet_clean` and `termsmet_lost_reply` at import time and define `close_termsmet()` with no arguments. Importing the plugin for verification must not import the MCP SDK or bridge, start a server, or require a project checkout. Live roles may load the neighboring `mcp_bridge.py` by file path with `importlib.util` when needed; the plugin is loaded by filename and is not a Python package. `close_termsmet()` must close all plugin-owned bridge sessions, be safe after partial initialization, and raise if cleanup fails. The runner calls it once after a live worker, including exceptions and interruptions; offline verification does not call it.

Set each YAML's `validator` to its corresponding ID. Do not list `plugin_files`: the runner loads the plugin once. Each live run gets `TERMSMET_PROJECT_DIR`, `TERMSMET_PROJECT_PYTHON`, `TERMSMET_DB_PATH`, and `TERMSMET_REVIEW_SECONDS=2`, plus an isolated `NANDATOWN_HOME`. The database is fresh. The plugin must keep paths and stderr out of Town events and public records.

Required passing stages for both cases: `agreement_funding`, `delivery_repair`, `release_gating`, `retrieved_artifact`. The lost-reply case additionally requires `lost_reply_recovery`. A missing, duplicate, incomplete, failed, or error stage cannot pass. Town's complete verdict and bundle verification must also pass.

## Bridge and messages

`TermsMetBridge(project_dir, project_python, db_path, review_seconds=2, startup_timeout=20, call_timeout=10)` exposes `start()`, `call(identity, tool, arguments, request_id=...)`, `records`, and idempotent `close()`. Results preserve server business errors; `BridgeError` identifies transport/startup/timeout errors. A dispatched timeout has an unknown outcome and closes the bridge. Records are observer-only snapshots, potentially containing private data; they require review before publication.

Fixed participants are `tm_buyer`, `tm_provider`, and `tm_adapter`. Requests use `tm_tool_request` with `request_id`, `tool`, `arguments`, and `purpose`; the adapter maps the envelope sender to the fixed identity. Replies use `tm_tool_result` with `request_id` and either the unchanged `result` or a separate `transport_error`. The first repair response uses `tm_repair_result`; lost-reply drops its first occurrence. A retry uses a new request ID, the same job and delivery, and records which request was unconfirmed. The provider must query the actual job and submission 2 report before confirming PASS; it cannot read observer records. `unconfirmed` is not the checker's `UNKNOWN`.

## Runner

```
.venv/bin/python examples/integrations/termsmet/run.py --project ../termsmet-runtime --case clean --out /tmp/termsmet-hw8-clean
.venv/bin/python examples/integrations/termsmet/run.py --project ../termsmet-runtime --case lost-reply --out /tmp/termsmet-hw8-lost
.venv/bin/python examples/integrations/termsmet/run.py --verify examples/integrations/termsmet/evidence
```

`--project-python` overrides the default `<project>/.venv/bin/python`. Live runs check the pinned runtime commit, clean tracked runtime files, both recorded Python versions, and MCP SDK version. They refuse nonempty output directories, use seed 42, and cap a trial at 120 seconds of real time. The output directory is the canonical bundle; failed attempts retain a `failure.json` and any available partial bundle under `.pending`. Do not reuse or overwrite it. `--verify` expects `clean/` and `lost_reply/` canonical bundles beneath its argument and loads validators without the runtime or MCP.

On macOS and Linux, the supervisor observes trial descendants throughout execution. It requests orderly worker cleanup on timeout or interruption, waits five seconds, then forcibly terminates tracked survivors and waits another five seconds. It reaps its direct child and reports any forced termination or surviving tracked process as cleanup failure. Process observation requires `ps`; if it is unavailable the live run fails. Processes that deliberately escape observation are outside this local demonstration's guarantees. No recorded bundle files are modified after Town writes them.
