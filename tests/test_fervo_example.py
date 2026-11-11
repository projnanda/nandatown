"""Offline checks for examples/integrations/fervo.

The live matrix needs a Fervo server and runner (see that README). These
tests need neither: they pin the adapter's replay rules (derived keys, a
recorded parent, a byte-identical commit, a stable response) and verify
the committed evidence bundles with Town's own verifier.
"""

import importlib.util
import json
import os
import shutil
import subprocess

import pytest

from nandatown.bundle import verify_bundle

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLE = os.path.join(REPO_ROOT, "examples", "integrations", "fervo")
EVIDENCE = os.path.join(EXAMPLE, "evidence")
FIXED = {"GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
         "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
         "GIT_AUTHOR_NAME": "fixture", "GIT_AUTHOR_EMAIL": "f@example.invalid",
         "GIT_COMMITTER_NAME": "fixture",
         "GIT_COMMITTER_EMAIL": "f@example.invalid"}


def _load(name):
    spec = importlib.util.spec_from_file_location(
        f"fervo_example_{name}", os.path.join(EXAMPLE, f"{name}.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


contributor = _load("fervo_contributor")


def test_keys_name_the_run_message_and_step():
    key = contributor.fervo_key("run-a", "q-1", "claim-0")
    assert key == "town-run-a-q-1-claim-0"
    # Every delivery of one request derives the same key...
    assert key == contributor.fervo_key("run-a", "q-1", "claim-0")
    # ...while Town's buyer reuses q-1 in every run, so the run separates them.
    assert key != contributor.fervo_key("run-b", "q-1", "claim-0")
    assert key != contributor.fervo_key("run-a", "q-1", "submit-0")


def test_fresh_keys_control_mints_a_new_key_per_delivery():
    first = contributor.fervo_key("run-a", "q-1", "claim-0", "0a1b2c3d")
    second = contributor.fervo_key("run-a", "q-1", "claim-0", "4e5f6a7b")
    assert first != second
    assert first.startswith(contributor.fervo_key("run-a", "q-1", "claim-0"))


def test_plan_keeps_the_first_parent_across_restarts(tmp_path):
    path = str(tmp_path / "plan.json")
    calls = []

    def choose(node):
        def pick():
            calls.append(node)
            return node
        return pick

    assert contributor.Plan(path).parent("q-1", 0, choose("n_0")) == "n_0"
    # A restarted seller sees main at its own merge; the claim must not move.
    restarted = contributor.Plan(path)
    assert restarted.parent("q-1", 0, choose("n_merged")) == "n_0"
    assert restarted.parent("q-1", 1, choose("n_merged")) == "n_merged"
    assert calls == ["n_0", "n_merged"]


def _fixture_clone(tmp_path, name, source):
    target = tmp_path / name
    subprocess.run(["git", "clone", "--quiet", str(source), str(target)],
                   check=True)
    return str(target)


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_contribution_commit_is_byte_identical(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    for name in ("fervo.toml", "score.txt"):
        shutil.copy(os.path.join(EXAMPLE, "fixture", name), source / name)
    env = {**os.environ, **FIXED}
    for args in (["init", "--quiet", "-b", "main"], ["add", "."],
                 ["-c", "commit.gpgsign=false", "commit", "--quiet", "-m",
                  "fixture"]):
        subprocess.run(["git", *args], cwd=source, env=env, check=True)
    parent = contributor.Fervo._git(str(source), "rev-parse", "HEAD")

    first = _fixture_clone(tmp_path, "first", source)
    second = _fixture_clone(tmp_path, "second", source)
    made = contributor.commit_contribution(contributor.Fervo, first, parent,
                                           10, "q-1")
    # Recreated from scratch, as after a lost worktree: the same commit id.
    assert contributor.commit_contribution(contributor.Fervo, second, parent,
                                           10, "q-1") == made
    # Replayed in place: nothing is committed again.
    assert contributor.commit_contribution(contributor.Fervo, first, parent,
                                           10, "q-1") == made
    with open(os.path.join(first, "score.txt")) as f:
        assert f.read() == "20\n"


def test_response_carries_only_replay_stable_fields():
    record = {"node": "n_1", "state": "merged", "merged_commit": "abc",
              "outcome": "improved", "rounds": [{"lease": "l_1"}]}
    request = {"sku": "widget", "quantity": 2, "unit_price_cents": 1995}
    body = contributor.response_body(request, "q-1", record)
    assert body["total_cents"] == 3990
    assert body["request_id"] == "q-1"
    assert body["fervo"] == {"node": "n_1", "state": "merged",
                             "merged_commit": "abc"}


def _evidence_rows():
    if not os.path.isdir(EVIDENCE):
        return []
    return sorted(row for row in os.listdir(EVIDENCE)
                  if os.path.isdir(os.path.join(EVIDENCE, row, "bundle")))


@pytest.mark.parametrize("row", _evidence_rows())
def test_committed_evidence_verifies_and_matches_its_check(row):
    bundle = os.path.join(EVIDENCE, row, "bundle")
    assert verify_bundle(bundle) == []
    with open(os.path.join(EVIDENCE, row, "fervo_check.json")) as f:
        check = json.load(f)
    # Every derived-keys row applied the contribution exactly once; the
    # fresh-keys controls are committed because they do not.
    assert check["passed"] is (not row.startswith("fresh-keys"))
    with open(os.path.join(bundle, "run.json")) as f:
        assert json.load(f)["run_id"] == check["town"]["run_id"]
