# Registry snapshot profile: claims checked against a record that outlives the run

A signature proves that a key signed. It cannot tell you whether that key still speaks for the identity it claims. If an external registry moved control of an identity from key K1 to key K2, K1 still produces perfect signatures. Only the registry's record knows that its authority lapsed.

This profile adds that check to the Lab. A card may carry a **registry claim**: a statement that an identity's controller key signed once, outside any run, naming the town agent that speaks for it. When an agent publishes its own card, the registry layer checks the claim against a **pinned snapshot of the external registry**. The snapshot holds each identity's current controller and every transaction that moved it.

A former controller that presents its claim is refused before anyone talks to it, however valid its signatures. A former controller that leaves its claim off is an ordinary seller. The town does not refuse it, but the validator still fails any run that lists, contacts or pays it.

The core (`registry_snapshot.py`) is neutral and names no source. It holds:
- a `registry-snapshot/v1` format;
- a claim statement;
- a policy;
- a validator.

This directory ships one source: a synthetic mock snapshot. A real registry plugs in through a small adapter (see Adding a source).

## The runs

```
nandatown run examples/registry_snapshot/scenarios/rotation_mock.yaml
nandatown run examples/registry_snapshot/scenarios/rotation_mock_sigonly_control.yaml
nandatown compare examples/registry_snapshot/scenarios/rotation_mock.yaml --swap registry=snapshot.mock.sigonly.v1
```

Two providers claim the same identity. The snapshot records that control moved from K1 to K2. The K1 provider prices below K2 and delivers honestly, so a buyer that cannot see the transfer has every reason to pick it.

| Run | Verdict | What happens |
|---|---|---|
| `rotation_mock` | passed | K2's claim resolves to the recorded controller and its card is listed. K1's claim signature is valid, and its card is refused on the record before any traffic. The buyer trades with K2. |
| `rotation_mock_sigonly_control` | failed on purpose | Same agents and seed. The registry still checks every town and claim signature but does not compare the record. K1 is listed, asked, picked and paid. |
| `compare --swap registry=snapshot.mock.sigonly.v1` | baseline passed, swapped failed | See below. |

**What the control changes, stage by stage:**
- **Fail on the run's evidence:** `former_controller_contained` (K1 was listed, contacted and paid) and `current_controller_paid` (K2 was not paid).
- **`not_tested` under the control:** `former_controller_refused` and `current_controller_verified`. A registry that does not compare records can neither refuse nor verify on one, so those stages are not judged rather than failed.
- **With K1 priced out of the buyer's cap:** the control pays K2 and `current_controller_paid` passes. That is tested.

## Situations and outcomes

| Situation | Outcome | Event |
|---|---|---|
| The current controller publishes its own card with its claim | resolved and listed | `registry_claim_resolved`, recorded only once the card is listed |
| A former controller publishes its own card with its claim, every signature valid | refused before any traffic; unbound, so nothing it sends verifies | `registry_claim_refused`, reason `controller_mismatch`, with `rotation_tx`; `registry_sender_unbound` |
| A key that never controlled the identity | refused, no rotation named | `controller_mismatch`, `rotation_tx: null` |
| The controller's public key presented with another key's claim signature | refused | `claim_signature_invalid` |
| A claim signed for another agent name | refused | `claim_signature_invalid` |
| A claim the agent was not configured with, even one the controller signed | refused | `claim_not_configured_for_agent` |
| Unknown or retired identity | refused | `identity_unknown` / `identity_retired` |
| A card under some agent's name that its own town key did not sign | never evaluated as a claim; nothing about that agent changes; `index.owned.v1` refuses it or stores it unverified | none from the claim check |
| Another agent republishes a listing under someone else's name, with any claim | refused by `index.owned.v1`; the listing stays (#303's invariant) | `card_publish_refused` |
| A card-shaped message body from any agent | judged as a message under the sender's own town key | none |
| Snapshot missing, edited (fingerprint not pinned) or inconsistent with its own history | every claim refused as unavailable; the run is `incomplete` | `registry_snapshot_unavailable`, reason `snapshot_unavailable` |

**Threat model.** Roles here may reach `engine.layers` and call `registry.publish` directly, as upstream's `RivalForger` does. Whose claim a card carries is therefore decided by the card's town signature under its name's key, never by the `publisher` argument.

## How it is wired

- **identity `claims.v1`.** Each agent gets a seeded Ed25519 town key for the run, recorded in `identity_created`. An agent whose config has a `claim` puts it on its card as `facts.registry_claim`. No controller key, mock or real, ever enters a run.
- **auth `ed25519.v1`.** Every call is judged the same way, whatever the payload looks like: the signature must verify under the town key of the name it claims. A name whose own claim was refused cannot sign anything that verifies.
- **registry `snapshot.mock.v1`** (and any adapter's registry). This extends `index.owned.v1`. A card carrying a claim is evaluated only if its name's own town key signed it. Then, in order:
  1. the claim is the one configured for that agent;
  2. the snapshot is available;
  3. the claim statement verifies under the presented controller key;
  4. the identity exists and is active;
  5. blake2b-224 of the controller key equals the recorded controller.

  A refusal unbinds that agent. A pass lists the card through `index.owned.v1`, and the resolution is recorded only if the listing succeeds. The `.sigonly` controls skip only step 5.
- **The claim statement**, signed by the controller key: `{"statement": "registry-claim/v1", "agent": <name>, "identity": <id>, "registry": <registry id>}`, as canonical JSON. `sign_claim.py` makes one, using the town's own `canonical_json`.

## The snapshot

`registry-snapshot/v1` holds:
- the source and network;
- the registry id;
- the key-hash function;
- the chain tip at capture;
- for each identity: its status, current controller, registry output reference and full history (`registered`, `controller_rotated`, `updated`, `retired`, each with transaction, slot and block height).

A snapshot loads only if all of these hold:
- its fingerprint equals the pin its registry plugin carries;
- every identity's history replays to exactly its recorded status and controller;
- every history slot is in order and at or before the tip.

Otherwise it is unavailable.

The run records the whole snapshot in `registry_snapshot_loaded`, together with:
- the validator version;
- the core profile's sha256;
- the file and sha256 of the registry plugin that loaded it.

Each claim event records the card, its town signature, the town key, the claim, the controller key and its hash.

## The validator recomputes, it does not trust

`registry_rotation` (version `registry-rotation-0.2`) judges every stage from `events.jsonl` alone, per agent. Every decision that depends on cryptography or on the record is recomputed, not read from the plugin's flags.

**`snapshot_loaded`** comes first. It needs exactly one loaded snapshot, by this validator version.
- **Rebuild:** the snapshot is rebuilt from the recorded fields; it must hash to both the recorded fingerprint and the registered pin, and its histories must replay. A failure here is reported as `error` (a town fault), not as a subject failure.
- **Missing coverage:** if the snapshot lacks a configured identity, or former and current controllers claim different identities, nothing is judged.
- **No snapshot, or another version:** every stage is `not_enough_evidence`.

**`former_controller_refused`**, for every former controller: a `controller_mismatch` refusal of its configured identity before its first traffic, in either direction. The refusal must survive every recomputation:
- the card is the agent's;
- the town key is the agent's, and it signed the card;
- the claim is the one on the card, and its signature verifies;
- the recorded hash is the hash of the recorded key;
- in the recorded history, that key controlled an identity that is still active, and was rotated away.

It is `not_tested` under a control.

**`former_controller_contained`**, for every former controller: no verified listing, no message sent to it, none accepted from it, and no escrow release or settlement to it. The note gives the counts.

**`current_controller_verified`**, for every claiming seller: exactly one key; a listed claim that recomputes the same way, for its configured identity; and the claim resolves to the recorded controller. It is `not_tested` under a control.

**`current_controller_paid`**: an escrow release to the current controller, for an order sent to it after its claim was listed, correlated by order id. Under a control, the anchor is the listed unchecked claim.

The standing `ledger_conserved` and `privacy` checks also run.

## Verifying a bundle

`python examples/registry_snapshot/verify_bundle.py <bundle>` reads what the bundle recorded: the validator version, the core sha256, and the source plugin's file and sha256. It refuses to replay unless this checkout has exactly those files. Only then does it import the core and the recorded source plugin and run the stock `nandatown verify`.

The gate is a whole-file hash. Any edit to either file, a comment included, orphans earlier bundles. That is deliberate for an example: a later change to the rules can never pass for a good bundle.

**Stock `nandatown verify` still checks a bundle's hashes and attestation, but it cannot check the evaluation of these bundles.** It replays only the validators it already knows, and it rightly never imports plugin files named inside a bundle. On these bundles it reports a replay mismatch, which it also reports for other causes. Only the wrapper can check the evaluation.

Bundles also record the absolute path of each plugin file in `profile.json`, because that is how scenario `plugin_files` are resolved upstream. Check before sharing a bundle.

## Adding a source

An adapter is a few lines. Load it after the core in `plugin_files`:

```python
from pathlib import Path
from nandatown.layers import register, resolve

SnapshotIndex = next(c for c in resolve("registry", "snapshot.mock.v1").__mro__
                     if c.__name__ == "SnapshotIndex")

@register("registry", "snapshot.example.v1")
class ExampleIndex(SnapshotIndex):
    source = "example"
    pinned = "sha256:..."          # fingerprint of the captured file
    snapshot_path = Path(__file__).resolve().parent / "example.json"
    defined_in = __file__          # so bundles record this file's hash
```

A capture tool writes the `registry-snapshot/v1` file before any run. To load, the file needs three things:
- every identity's history in chain order;
- the tip read after the histories;
- a fingerprint that equals the adapter's pin.

Each claim is a statement the controller key signs once, off-chain, with `sign_claim.py`. It goes into the agent's config as `claim`.

## Tests

`pytest tests/test_registry_snapshot_profile.py` covers:
- the runs and the compare, including the control with K1 priced out and with no K1;
- the wrapper verifying, and refusing a changed profile;
- the drop-in sweep: `claims.v1` and `ed25519.v1` swapped into all 11 bundled scenarios keep their verdicts, except `capability_spoofing_weak_auth`, whose point is the `plain.v1` the swap replaces;
- every refusal reason;
- snapshot edits and inconsistencies;
- the #303 listing invariant;
- the direct-publish and card-shaped-body probes;
- traces for each validator rule, each built so that only the rule under test fails;
- mock data reproducing byte for byte.

**Mutation check.** 51 single-point weakenings of the validator, the recompute chain, the history replay, the registry and auth, each applied alone; all 51 fail a named test (the full table, each weakening with the test that kills it, is in the PR description).

## What this shows, and what it does not

It shows three things:
- a check against a registry record that outlives the run refuses a former controller that presents its claim, even when every signature is valid;
- the refusal comes before any traffic or money;
- the same run without the record comparison lists and pays that key.

It does not show any of these:
- **Trustworthiness.** It doesn't show that a resolved identity is trustworthy: a signature shows that a key committed, not that a claim is true.
- **A chain refusing anything.** The refusal is this plugin's decision over a pinned copy of the record.
- **A guard against a former that hides its claim.** The town does not refuse one; the validator does fail any run that lists or pays it.
- **Identity-pinned buying.** The buyer trades with any listed seller; it does not seek a particular identity.
- **Revocation by a third party.** A retirement is whatever the source registry records.
- **Finality.** Nothing beyond the depth recorded at capture.
- **Real-world use.** The mock snapshot and its keys are synthetic.
- **Generality.** One seeded run is one observation.

## Next

- Bind claims to deployed NANDA Index records instead of a profile-local snapshot; this is the "Next" of #303.
- `verify --plugin`, as a separate core proposal with its own trust discussion.
- A buyer that requires a resolved claim for one identity.
- Mutual verification, where the provider checks the buyer's claim.
- Retirement as a scenario.
