"""The portable evidence bundle.

One directory holds the five records: the profile (the recipe), the run
(the attempt), the intents (the requested actions), the events (the
attributed facts), and the result (the evaluator's scoped observation).
A manifest fingerprints every file. The human report is a rendered view
of the bundle, not a sixth record. Later conclusions can reference this
evidence; they never rewrite it.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import re
import stat
import time
from typing import Any

from . import __version__
from .evaluator import EVALUATOR_VERSION, EVALUATOR_VERSIONS, evaluate
from .records import (
    EvidenceResult,
    Intent,
    RunRecord,
    TestProfile,
    TownEvent,
    fingerprint,
)

RECORD_FILES = ["profile.json", "run.json", "intents.jsonl", "events.jsonl",
                "result.json"]
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
BUNDLE_MODES = {"track", "lab", "path"}


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _regular_file_problem(path: str, name: str) -> str | None:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return f"{name} missing"
    except OSError as exc:
        return f"{name} unreadable: {exc}"
    if not stat.S_ISREG(metadata.st_mode):
        return f"{name} is not a regular file"
    return None


def _duplicates(values: list[str]) -> list[str]:
    seen: set[str] = set()
    repeated: set[str] = set()
    for value in values:
        if value in seen:
            repeated.add(value)
        else:
            seen.add(value)
    return sorted(repeated)


def _jsonl_records(text: str) -> list[str]:
    """Split JSON Lines at LF only.

    str.splitlines() also breaks at U+2028, U+2029 and U+0085, which JSON
    allows unescaped inside strings, and would cut one record in two.
    Text-mode reads have already turned CRLF into LF."""
    return [line for line in text.split("\n") if line]


def write_bundle(directory: str, profile, run: RunRecord,
                 intents: list[dict[str, Any]], events: list[TownEvent],
                 result: EvidenceResult, mode: str = "track") -> dict[str, Any]:
    os.makedirs(directory, exist_ok=True)

    def write(name: str, text: str) -> None:
        with open(os.path.join(directory, name), "w", encoding="utf-8") as f:
            f.write(text)

    write("profile.json", profile.model_dump_json(indent=2))
    write("run.json", run.model_dump_json(indent=2))
    write("intents.jsonl",
          "".join(json.dumps(i) + "\n" for i in intents))
    write("events.jsonl",
          "".join(e.model_dump_json() + "\n" for e in events))
    write("result.json", result.model_dump_json(indent=2))

    files = {name: _sha256_file(os.path.join(directory, name))
             for name in RECORD_FILES}
    manifest = {
        "mode": mode,
        "files": files,
        "bundle_fingerprint": fingerprint(files),
        "created_at": time.time(),
        "nandatown_version": __version__,
        "evaluator_version": result.evaluator_version,
    }
    write("manifest.json", json.dumps(manifest, indent=2))

    from .report import render_report
    write("report.md", render_report(load_bundle(directory)))
    return manifest


def attest_bundle(directory: str, keystore=None,
                  signer: str | None = None) -> dict[str, Any]:
    """A signed, replayable attestation with provenance.

    The operator's controller key signs the bundle fingerprint and the
    verdict; anyone can replay the evidence and check the signature, so
    the attestation is measurable, not a marketing claim."""
    from .identity_portable import (
        OPERATOR_NAME,
        Keystore,
        default_keystore_dir,
    )

    keystore = keystore or Keystore(default_keystore_dir())
    signer = signer or OPERATOR_NAME
    identity = keystore.new_identity(signer)
    with open(os.path.join(directory, "manifest.json"),
              encoding="utf-8") as f:
        manifest = json.load(f)
    with open(os.path.join(directory, "result.json"), encoding="utf-8") as f:
        result = json.load(f)
    payload = {
        "bundle_fingerprint": manifest["bundle_fingerprint"],
        "run_id": result["run_id"],
        "verdict": result["verdict"],
        "evaluator_version": result["evaluator_version"],
        "signer": identity["agent_id"],
        "signed_at": time.time(),
    }
    attestation = {
        "payload": payload,
        "signature": keystore.sign(signer, payload),
        "controller_public": identity["controller_public"],
    }
    with open(os.path.join(directory, "attestation.json"), "w",
              encoding="utf-8") as f:
        json.dump(attestation, f, indent=2)
    return attestation


def load_bundle(directory: str) -> dict[str, Any]:
    def read(name: str) -> str:
        with open(os.path.join(directory, name), encoding="utf-8") as f:
            return f.read()

    manifest = json.loads(read("manifest.json"))
    mode = manifest.get("mode", "track")
    profile_json = read("profile.json")
    if mode == "lab":
        from .sim.scenario import ScenarioSpec
        profile: Any = ScenarioSpec.model_validate_json(profile_json)
    elif mode == "path":
        from .path_profiles import PathProfile
        profile = PathProfile.model_validate_json(profile_json)
    else:
        profile = TestProfile.model_validate_json(profile_json)
    return {
        "directory": directory,
        "mode": mode,
        "profile": profile,
        # The profile as its producer recorded it. profile is that document
        # read by today's model, which is what a caller wants to work with
        # but not what the run committed to: a field added to the model
        # since appears there with its default and changes the fingerprint.
        "profile_document": json.loads(profile_json),
        "run": RunRecord.model_validate_json(read("run.json")),
        "intents": [Intent.model_validate_json(line)
                    for line in _jsonl_records(read("intents.jsonl"))],
        "events": [TownEvent.model_validate_json(line)
                   for line in _jsonl_records(read("events.jsonl"))],
        "result": EvidenceResult.model_validate_json(read("result.json")),
        "manifest": manifest,
    }


# Every evaluator version main has recorded in bundles, per bundle mode.
# Source: `git log -p -G'EVALUATOR_VERSION *=' -- src/` and the
# quote-intent return in path_runner.path_evaluator_version, checked
# against each first-parent commit of main: Track evaluator.py (d32d3e1,
# which reached main in 6adfd18; 0.3.0 and 0.4.0 in #264 and #279); Lab
# sim/validators.py (cb19e0e, 7af8084, d26ca5a, f4e85d7, 9f2e361,
# 6a697f2, de57461); Path path_runner.py (5aab66a, 64ecb5f, 2209bbf,
# d55b7e3). Add a version here when it merges.
#
# This list matters only for a bundle this Town cannot replay. A bundle
# whose recorded version it still has the rules for, which today means
# every Track version, the current Lab evaluator and each Path profile
# from path-0.2, is replayed, and verify, receipts and proof judge that
# replay. A bundle naming a version listed here that this Town can no
# longer replay, such as an older Lab evaluator or path-0.1, is accepted
# for a receipt without replay, and the receipt says so; any other
# version is refused.
SHIPPED_EVALUATOR_VERSIONS: dict[str, frozenset[str]] = {
    "track": frozenset({"0.2.0", "0.3.0", "0.4.0"}),
    "lab": frozenset({"lab-0.2.0", "lab-0.2.1", "lab-0.2.2", "lab-0.2.3",
                      "lab-0.2.4", "lab-0.2.5", "lab-0.2.6"}),
    "path": frozenset({"path-0.1", "path-0.2", "path-0.3",
                       "path-quote-intent-0.1", "path-quote-intent-0.2"}),
}


class EvaluatorVersionDiffers(str):
    """The verify_bundle problem for a bundle from another evaluator version.

    Replay needs the evaluator version the bundle names, so the recorded
    result is not reproduced; every other check still runs. It is a
    ``str`` carrying the unchanged message, so verify_bundle's return
    contract is the same for every caller. Only a version listed in
    SHIPPED_EVALUATOR_VERSIONS for the bundle's mode is historical.
    """

    bundle_version: str
    local_version: str
    mode: str

    def __new__(cls, bundle_version: str, local_version: str,
                mode: str) -> EvaluatorVersionDiffers:
        problem = super().__new__(
            cls, f"evaluator version differs: bundle {bundle_version},"
                 f" local {local_version}; reproducibility not checked")
        problem.bundle_version = bundle_version
        problem.local_version = local_version
        problem.mode = mode
        return problem

    def __getnewargs__(self) -> tuple[str, str, str]:
        return self.bundle_version, self.local_version, self.mode

    @property
    def shipped(self) -> bool:
        """Whether this project shipped the bundle's version for its mode."""
        return self.bundle_version in SHIPPED_EVALUATOR_VERSIONS.get(
            self.mode, frozenset())


def verify_bundle_integrity(
        directory: str) -> tuple[list[str], EvaluatorVersionDiffers | None]:
    """verify_bundle, split for callers that accept historical bundles.

    Returns every integrity problem (records, hashes, manifest,
    cross-record bindings, unsupported or unrecognised evaluator, replay
    mismatch under the local evaluator, attestation) and, separately, the
    evaluator version difference that left replay unchecked when the
    bundle names a version shipped for its mode that this Town cannot
    replay. Any other version can be neither replayed nor recognised, so
    it is an integrity problem."""
    integrity: list[str] = []
    differs: EvaluatorVersionDiffers | None = None
    for problem in verify_bundle(directory):
        if not isinstance(problem, EvaluatorVersionDiffers):
            integrity.append(problem)
        elif problem.shipped:
            differs = problem
        else:
            integrity.append(
                f"unrecognised evaluator version {problem.bundle_version}"
                f" for {problem.mode} bundles (local"
                f" {problem.local_version}); replay not possible")
    return integrity, differs


def verify_bundle(directory: str) -> list[str]:
    """Check integrity and evaluator reproducibility. Returns problems."""
    problems: list[str] = []
    manifest_path = os.path.join(directory, "manifest.json")
    manifest_problem = _regular_file_problem(manifest_path, "manifest.json")
    if manifest_problem:
        return [manifest_problem]
    try:
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return [f"manifest.json unreadable: {exc}"]
    if not isinstance(manifest, dict):
        return ["manifest.json must contain a JSON object"]

    mode = manifest.get("mode", "track")
    if not isinstance(mode, str) or mode not in BUNDLE_MODES:
        problems.append(f"unknown bundle mode {mode!r}")
    files = manifest.get("files")
    if not isinstance(files, dict):
        problems.append("manifest.json files must be a JSON object")
        return problems
    actual_names = set(files)
    canonical_names = set(RECORD_FILES)
    if actual_names != canonical_names:
        missing = sorted(canonical_names - actual_names)
        unexpected = sorted(actual_names - canonical_names, key=repr)
        problems.append("manifest must name exactly the five canonical records:"
                        f" missing={missing}, unexpected={unexpected}")
    for name, expected in files.items():
        if not isinstance(expected, str) or not DIGEST_RE.fullmatch(expected):
            problems.append(f"{name} digest is malformed")
    bundle_fingerprint = manifest.get("bundle_fingerprint")
    if not isinstance(bundle_fingerprint, str) \
            or not DIGEST_RE.fullmatch(bundle_fingerprint):
        problems.append("bundle fingerprint is malformed")
    if problems:
        return problems

    for name in RECORD_FILES:
        problem = _regular_file_problem(os.path.join(directory, name), name)
        if problem:
            problems.append(problem)
    if problems:
        return problems

    calculated_fingerprint = fingerprint(files)
    if bundle_fingerprint != calculated_fingerprint:
        problems.append(
            "bundle fingerprint mismatch: manifest "
            f"{bundle_fingerprint}, calculated "
            f"{calculated_fingerprint}")
    for name, expected in files.items():
        path = os.path.join(directory, name)
        try:
            actual = _sha256_file(path)
        except OSError as exc:
            problems.append(f"{name} unreadable: {exc}")
            continue
        if actual != expected:
            problems.append(f"{name} hash mismatch: manifest {expected},"
                            f" actual {actual}")
    if problems:
        return problems

    try:
        bundle = load_bundle(directory)
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError,
            ValueError) as exc:
        return [f"bundle records invalid: {exc}"]
    recorded = bundle["result"]
    run = bundle["run"]
    profile = bundle["profile"]
    profile_name = (profile.ref if bundle["mode"] == "path"
                    else profile.name)
    if run.run_id != recorded.run_id:
        problems.append("run and result name different run ids")
    if run.profile_name != profile_name:
        problems.append("run profile name does not match profile")
    # Against the recorded document, not the model's reading of it: a run
    # committed to what its producer wrote, and every field the model has
    # gained since would otherwise break every older bundle at once. This
    # is also the stricter comparison, because it sees a field the model
    # would drop.
    if run.profile_fingerprint != fingerprint(bundle["profile_document"]):
        problems.append("run profile fingerprint does not match profile")
    if any(intent.run_id != run.run_id for intent in bundle["intents"]):
        problems.append("intent names a different run id")
    if any(event.run_id != run.run_id for event in bundle["events"]):
        problems.append("event names a different run id")
    participant_problems = []
    if not run.participants:
        participant_problems.append("run has no participants")
    for index, participant in enumerate(run.participants):
        for field in ("name", "role"):
            value = participant.get(field)
            if not isinstance(value, str) or not value.strip():
                participant_problems.append(
                    f"run participant {index} has no valid {field}")
    problems.extend(participant_problems)
    if manifest.get("nandatown_version") != run.releases.get("nandatown"):
        problems.append("manifest nandatown version does not match run release")
    if run.releases.get("evaluator") != recorded.evaluator_version:
        problems.append("run evaluator release does not match result")
    if manifest.get("evaluator_version") != recorded.evaluator_version:
        problems.append("manifest evaluator version does not match result")
    if bundle["mode"] in {"lab", "path"} \
            and run.config.get("mode") != bundle["mode"]:
        problems.append("run mode does not match manifest mode")
    intent_ids = [intent.intent_id for intent in bundle["intents"]]
    duplicate_intent_ids = _duplicates(intent_ids)
    if duplicate_intent_ids:
        problems.append(f"duplicate intent ids: {duplicate_intent_ids}")
    event_ids = [event.event_id for event in bundle["events"]]
    duplicate_event_ids = _duplicates(event_ids)
    if duplicate_event_ids:
        problems.append(f"duplicate event ids: {duplicate_event_ids}")
    stage_names = [stage.name for stage in recorded.stages]
    duplicate_names = _duplicates(stage_names)
    if duplicate_names:
        problems.append(f"result has duplicate stage names: {duplicate_names}")
    records_coherent = not problems

    try:
        if bundle["mode"] == "lab":
            from .sim.validators import LAB_EVALUATOR_VERSION, evaluate_scenario
            expected_version = LAB_EVALUATOR_VERSION
            replay_fn = evaluate_scenario
        elif bundle["mode"] == "path":
            from .path_runner import evaluate_path, path_evaluator_version
            expected_version = path_evaluator_version(bundle["profile"])
            replay_fn = evaluate_path
        else:
            # A recorded Track version replays under its own rules; any
            # other version is reported as a difference below.
            expected_version = (
                recorded.evaluator_version
                if recorded.evaluator_version in EVALUATOR_VERSIONS
                else EVALUATOR_VERSION)
            replay_fn = functools.partial(evaluate, version=expected_version)
    except (KeyError, ValueError) as exc:
        expected_version = None
        replay_fn = None
        problems.append(str(exc))
    if expected_version is not None \
            and recorded.evaluator_version != expected_version:
        problems.append(EvaluatorVersionDiffers(
            recorded.evaluator_version, expected_version, bundle["mode"]))
    elif replay_fn is not None and records_coherent:
        try:
            replay = replay_fn(
                bundle["profile"], recorded.run_id, bundle["events"])
        except Exception as exc:
            problems.append(
                "evaluator replay failed on the recorded evidence:"
                f" {type(exc).__name__}: {exc}")
        else:
            recorded_result = recorded.model_dump(exclude={"evaluated_at"})
            replay_result = replay.model_dump(exclude={"evaluated_at"})
            if recorded_result != replay_result:
                problems.append(
                    "evaluator replay mismatch: result.json does not match a"
                    " fresh deterministic evaluation")

    attestation_path = os.path.join(directory, "attestation.json")
    if os.path.lexists(attestation_path):
        from .identity_portable import verify_signature
        from .records import fingerprint as _fingerprint

        attestation_problem = _regular_file_problem(
            attestation_path, "attestation.json")
        if attestation_problem:
            problems.append(attestation_problem)
            return problems
        try:
            with open(attestation_path, encoding="utf-8") as f:
                attestation = json.load(f)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            problems.append(f"attestation.json unreadable: {exc}")
            return problems
        if not isinstance(attestation, dict) \
                or not isinstance(attestation.get("payload"), dict):
            problems.append("attestation.json must contain an object payload")
            return problems
        payload = attestation["payload"]
        expected_claims = {
            "bundle_fingerprint": bundle["manifest"]["bundle_fingerprint"],
            "run_id": recorded.run_id,
            "verdict": recorded.verdict,
            "evaluator_version": recorded.evaluator_version,
        }
        if payload.get("bundle_fingerprint") != \
                expected_claims["bundle_fingerprint"]:
            problems.append("attestation names a different bundle"
                            " fingerprint")
        for name in ("run_id", "verdict", "evaluator_version"):
            if payload.get(name) != expected_claims[name]:
                problems.append(
                    f"attestation {name.replace('_', ' ')} does not match bundle")
        controller_public = attestation.get("controller_public")
        signature = attestation.get("signature")
        if not isinstance(controller_public, str) \
                or not isinstance(signature, str):
            problems.append("attestation signature and controller key must be strings")
        elif not verify_signature(controller_public, payload, signature):
            problems.append("attestation signature does not verify")
        if isinstance(controller_public, str):
            derived = ("did:town:" + _fingerprint(
                controller_public)
                .removeprefix("sha256:")[:24])
            if derived != payload.get("signer"):
                problems.append("attestation signer id does not match"
                                " its controller key")
    return problems
