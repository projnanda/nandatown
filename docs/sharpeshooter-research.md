# SharpeShooter research Track

Town's SharpeShooter profiles exercise a bounded structured-research exchange.
They do not turn Town into a strategy evaluator. SharpeShooter owns the
workflow and receipt proof; Town owns transport observations and
requester-attributed validation stages. The result correlates those two layers
without treating one as proof of the other.

## Frozen input and fresh-consumer reproduction

The only authorized product input is the immutable allowlisted
[SharpeShooter release `hw8-ingress-42ff7b384e92`](https://github.com/sidsanghvi/sharpeshooter-hw8-reproduction/releases/tag/hw8-ingress-42ff7b384e92).
Do not substitute a moving branch or rebuild that release when reproducing the
reviewed integration. Milestone 5 ran the registered `clean` and `duplicate`
generations exactly once and promoted their sanitized evidence into the course
SharpeShooter repository. Those evidence files are not part of the public
ingress release or this Nanda PR. Never rerun, replace, or reinterpret those
registered generations. The public commands below cover installed-release
validation and deterministic fixture reproduction. The final read-only evidence
audit additionally requires authorized access to the course SharpeShooter
repository. None of these commands calls a model or repeats either project's
full suite.

The Nanda production implementation used by the registered evidence is commit
`195b60bbf3b3a166ba76e349ddff9fc34f537ab9`. Check out that exact commit when
reproducing the evidence-bound behavior. The PR head may include later
documentation-only publication updates; do not substitute a different
production commit unless the affected compatibility gates are rerun and a
separately named evidence generation is created. Public network access is
required to download the frozen SharpeShooter release and Python dependencies,
and the fixture checks require permission to bind loopback sockets.

From the root of that fresh Nanda checkout, create the consumer environment:

```bash
git checkout --detach 195b60bbf3b3a166ba76e349ddff9fc34f537ab9
test "$(git rev-parse HEAD)" = \
  195b60bbf3b3a166ba76e349ddff9fc34f537ab9
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

Download and validate the three immutable release assets in a separate
temporary directory, install only the reviewed wheel into a clean environment,
and reproduce the frozen task digest and installed workflow/receipt smoke:

```bash
RELEASE_DIR="$(mktemp -d)"
RELEASE_URL="https://github.com/sidsanghvi/sharpeshooter-hw8-reproduction/releases/download/hw8-ingress-42ff7b384e92"
curl -fL "$RELEASE_URL/hw8-projection-manifest.json" \
  -o "$RELEASE_DIR/hw8-projection-manifest.json"
curl -fL "$RELEASE_URL/sharpeshooter-0.1.0.tar.gz" \
  -o "$RELEASE_DIR/sharpeshooter-0.1.0.tar.gz"
curl -fL "$RELEASE_URL/sharpeshooter-0.1.0-py3-none-any.whl" \
  -o "$RELEASE_DIR/sharpeshooter-0.1.0-py3-none-any.whl"

printf '%s  %s\n' \
  feaa35f2dca67eba61151f3549dcce26af146b4fbbae6883ce1214b018075667 \
  "$RELEASE_DIR/hw8-projection-manifest.json" \
  de804abc0c8026e51fabcdeb43d998e06e7805f08060d6b986cea62d1468babd \
  "$RELEASE_DIR/sharpeshooter-0.1.0.tar.gz" \
  a651b256368d82f7a9f90d411a9eaaf81e4259b1bfb6b5080f506675d542900b \
  "$RELEASE_DIR/sharpeshooter-0.1.0-py3-none-any.whl" | \
  shasum -a 256 -c -

tar -xzf "$RELEASE_DIR/sharpeshooter-0.1.0.tar.gz" -C "$RELEASE_DIR"
python3.12 -m venv "$RELEASE_DIR/venv"
"$RELEASE_DIR/venv/bin/python" -m pip install \
  "$RELEASE_DIR/sharpeshooter-0.1.0-py3-none-any.whl"
TASK="$RELEASE_DIR/sharpeshooter-0.1.0/tasks/hw8/fixtures/research-task-v1.json"
TASK_DIGEST="$("$RELEASE_DIR/venv/bin/sharpeshooter-task" validate --task "$TASK")"
test "$TASK_DIGEST" = \
  sha256:cb03de74c7a8e08414d4d30cd84ad5ed1ef0b73bef3cf980095615e20d480a9a
"$RELEASE_DIR/venv/bin/python" \
  "$RELEASE_DIR/sharpeshooter-0.1.0/scripts/smoke_hw8_wheel.py" \
  --task "$TASK"
```

Development checks use the deterministic fixture process in
`tests/fixtures/sharpeshooter_task_fixture.py`. They make no model call, use no
provider credential, and do not execute the released SharpeShooter command.
Run the two fixture-only coordinator cases independently and retain their
temporary output for an explicit CLI verification and replay:

```bash
FIXTURE_ROOT="$(mktemp -d)"
.venv/bin/python -m pytest -q tests/test_sharpeshooter_research_flow.py \
  -k test_fixture_only_healthy_profile_runs_through_the_real_coordinator \
  --basetemp "$FIXTURE_ROOT/healthy"
HEALTHY_RUN_JSON="$(find "$FIXTURE_ROOT/healthy" -type f -name run.json \
  -path '*/town-bundle/*' -print -quit)"
test -n "$HEALTHY_RUN_JSON"
HEALTHY_BUNDLE="${HEALTHY_RUN_JSON%/run.json}"
.venv/bin/nandatown verify "$HEALTHY_BUNDLE"
.venv/bin/nandatown replay "$HEALTHY_BUNDLE"

.venv/bin/python -m pytest -q tests/test_sharpeshooter_research_flow.py \
  -k test_fixture_only_duplicate_profile_runs_through_the_real_coordinator \
  --basetemp "$FIXTURE_ROOT/duplicate"
DUPLICATE_RUN_JSON="$(find "$FIXTURE_ROOT/duplicate" -type f -name run.json \
  -path '*/town-duplicate-bundle/*' -print -quit)"
test -n "$DUPLICATE_RUN_JSON"
DUPLICATE_BUNDLE="${DUPLICATE_RUN_JSON%/run.json}"
.venv/bin/nandatown verify "$DUPLICATE_BUNDLE"
.venv/bin/nandatown replay "$DUPLICATE_BUNDLE"
```

These tests are deterministic development fixtures. They are not the
registered clean or duplicate cases, do not create or promote registered
evidence, and cannot support a real compatibility claim by themselves.

Finally, reviewers with authorized access to the course SharpeShooter repository
may perform this separate read-only audit at evidence head
`2936191ceef8a993722fff4be65a4b08b763c3a3`. These evidence files are not in
the public ingress release or this Nanda PR. From a fresh authorized checkout,
validate the exact evidence-only range and promoted bytes. The named range must
add only `clean.json`, `duplicate.json`, `manifest.json`, and `comparison.md`
below `tasks/hw8/evidence/`:

```bash
git checkout --detach 2936191ceef8a993722fff4be65a4b08b763c3a3
test "$(git rev-parse HEAD)" = \
  2936191ceef8a993722fff4be65a4b08b763c3a3
git diff --name-only \
  2af22d9e02752cdbf2f44d40ad0cab42e55e00cc..2936191ceef8a993722fff4be65a4b08b763c3a3
PYTHONPATH=.:src python3.12 scripts/scan_hw8_hygiene.py \
  --repo . \
  --base 2af22d9e02752cdbf2f44d40ad0cab42e55e00cc \
  --head 2936191ceef8a993722fff4be65a4b08b763c3a3 \
  --evidence-root tasks/hw8/evidence
printf '%s  %s\n' \
  588ad13f8de073ea3ca82aa63a40fd0258530b5cff4b1a79f2248107fd324670 \
  tasks/hw8/evidence/clean.json \
  715822db50eab09be51b8bf60c2d833a0787485770d591f7b69b1926c9213798 \
  tasks/hw8/evidence/duplicate.json \
  ccf392da48c92335da39011ecfdc2deb707b014cab6b9ce559a554cdf713e1c6 \
  tasks/hw8/evidence/manifest.json \
  11e70f08eb9d97df72fb92ebf11acd24612fb17205dd39d0bcf2705e9f7fcb35 \
  tasks/hw8/evidence/comparison.md | shasum -a 256 -c -
```

The adapter launches a configured executable directly with a fixed argument
vector, a minimal allowlisted environment, bounded output, and no shell. That
is a subprocess containment boundary, not a sandbox for hostile code. Its
durable per-request journal records `started` before launch and `completed`
only after validating a bounded project result. A completed entry is replayed
without relaunching; an interrupted `started` entry fails closed because v1 has
no automatic project-level resume protocol.

The requester independently binds the Town response to the request ID and
digest, task digest and content, selected specification, ordered candidate
results, receipt integrity hash, distinct accepting reviewer, OOS budget
transition, receipt consumption, and preselected operator policy. The
operator-policy fields are frozen deterministic inputs, not a live human click.
A receipt hash supplies tamper-evidence only; it is not an independently signed
truth attestation.

## Exact claim boundaries

The following ordered text is part of the frozen contract and travels with the
bounded result:

1. The task is bounded and structured; it is not unrestricted prose research.
2. Task content controls provenance and tested specifications, but the deterministic evaluator does not understand the economic thesis.
3. Synthetic metrics do not validate strategy performance.
4. Town proves transport and attributed requester checks; SharpeShooter proves its workflow/receipt; cross-system evidence correlates them.
5. A receipt hash is tamper-evidence, not an independently signed truth attestation.
6. The run does not prove real-agent reasoning, A2A/MCP conformance, hostile-code sandboxing, production readiness, or arbitrary interoperability.
7. A `started` journal fails closed after interruption; v1 does not promise automatic project-level resumption.
