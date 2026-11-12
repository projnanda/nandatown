# Signed Answers: an outlet’s source packages through Town

Signed Answers from News Outlets for AI Agents is a course project. A news outlet runs its own agent. A reader’s agent asks it about the outlet’s reporting over A2A. The outlet’s agent checks a subscriber credential and returns a signed source package: the exact sentences from the story, the document each sentence cites, dates, correction status and usage terms. The signature proves the package came from the outlet unchanged. It makes no claim that the reporting is true.

This example runs that exchange inside Town’s Lab. The project stays where it is. Only the Town side is here.

## What connects to what

```
reader-subscriber, reader-stranger        Town agents (role sa_reader)
   | 1. look up "news.source_package" in the Town registry
   | 2. send a signed Town message: source_request
   v
outlet                                    Town agent (role sa_outlet)
   | 3. A2A JSON-RPC SendMessage over HTTPS, with the subscriber
   |    credential for readers on its subscriber list
   v
the outlet's agent                        runs elsewhere, not in this repo
   | 4. reply text with a package link; the gateway downloads the package
   v
outlet --signed Town message: source_answer--> reader
   5. the reader checks the package id and the Ed25519 signature
      under the key on the outlet's Town card
```

Town parts in use: the registry (the outlet’s card carries its signing key), identity and auth (every message is signed and the gateway maps a verified Town sender to the credential, so no reader holds it), transport (the duplicate fault), and privacy (package text and the question are redacted from the bundle).

The gateway forwards. It never answers on its own, and the validator reads Town events only.

## Files

| File | What it is |
|---|---|
| `signed_answers_town.py` | The two roles, the A2A client, the signature check and the `signed_answers` validator |
| `scenarios/*.yaml` | Four scenarios |
| `stub_outlet.py` | A stand-in outlet agent for offline runs. It is a fixture with an invented story and a test key |
| `run_check.py` | Runs every case, compares expected and observed, verifies each bundle |
| `evidence/before-fix/`, `evidence/after-fix/` | Bundles from two runs against the real outlet agent, before and after its fix |
| `../../tests/test_signed_answers_example.py` | Sixteen offline tests, among them broken and hostile outlet replies |

## Reproduce

Town commit `6f9a7c064486c4a0ae9242f1ff341b8a4aad0244`. Outlet agent: commits `24816cc` (before the fix) and `f69f3c9` (after) of the project’s private repository, deployed as a hosted service. Python 3.12.

```bash
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -e '.[dev]'
```

Offline, with no network, model or credential (about 10 seconds):

```bash
python -m pytest -q tests/test_signed_answers_example.py
python examples/signed_answers/run_check.py
```

Live, against the real outlet agent. The address, a test credential and the question are in the pull request description. They are not in this repository.

```bash
export SIGNED_ANSWERS_URL=...
export SIGNED_ANSWERS_TOKEN=...
export SIGNED_ANSWERS_QUESTION=...
python examples/signed_answers/run_check.py --live
```

Check the saved bundles:

```bash
python examples/signed_answers/run_check.py --verify examples/signed_answers/evidence
```

`nandatown verify` alone reports a replay mismatch for these bundles. It does not load a plugin validator, so it cannot repeat the `signed_answers` stages. `run_check.py --verify` loads the validator first and then calls the same check.

## Results

Two live runs on 2026-11-11, with one question about a 2023 story that carries a correction. The first run found a failure in the outlet’s agent. I fixed the agent and ran again.

| Case | Expected | Before the fix (`24816cc`) | After the fix (`f69f3c9`) |
|---|---|---|---|
| `clean` | The subscriber’s reader gets a package and it verifies. The stranger’s reader gets none. | Passed | Passed |
| `duplicate` | One request delivered twice returns the same package. | **Failed** at `one_package_per_request`: two valid packages, two different ids | Passed: the same package id both times |
| `duplicate_remembered` | The gateway answers the redelivery from its own record. | Passed. The agent was called once. | Passed |
| `altered` | The reader rejects a package the gateway changed by one character. | Passed. Town’s message signature verified. The outlet’s signature and the package id both failed. | Passed |
| `outlet_down` | With the outlet unreachable, the gateway lists nothing and no request is sent. | Failed at `outlet_listed` and `outlet_discovered`, as it should | Same |

### The failure Town found

The outlet’s agent ignored the A2A `messageId`. Each message it received started new work, and the package carries its issue time inside the signed content. So a redelivered request produced a second package with a new id. Both packages were valid. A caller that cites a package by id then held two ids for one question, and the second call also spent the outlet’s model budget.

The project’s own tests never sent the same message twice, so they did not show this. Town’s duplicate fault did.

The fix is in the agent. It keeps each first reply by subscriber, context, `messageId` and text. A repeat gets the same reply, runs no model and signs nothing new. After the fix the `duplicate` case passes against the real agent. Offline, the stand-in outlet plays both versions: its default builds a fresh package per call, and `--idempotent` returns the first one again. `duplicate_remembered` shows the other place to absorb a redelivery, at the gateway.

## What is not tested

- The reader and the gateway are written by the project’s author. This is not an independent-agent result and not A2A conformance.
- The reader is scripted. It asks one fixed question. The outlet’s clarifying question, its follow-up offers and correction notices are not driven through Town.
- The reader checks the package id and signature only. The project’s full verifier also compares each sentence with the published story and fingerprints archived source copies. That part is not run here.
- The reader takes the signing key from the Town card, which the gateway wrote after it read the outlet’s agent card. A dishonest gateway could list its own key. `altered` covers a gateway that changes a package, not one that replaces the key. The project’s real reader pins the key ahead of time.
- The package text is redacted, so a replay of a bundle repeats the validator on the reader’s recorded check results. It cannot repeat the signature check itself.
- The agent keeps first replies in memory on one instance. A restart forgets them. Two copies of a message that arrive at the same moment can still both run.
- Town’s `hmac.v1` auth and `index.v1` registry are the defaults. No trust, payment or negotiation layer is used.
- A live run calls a model, so it is not deterministic, even though the Lab report prints “deterministic” for its own logical clock. The bundles record what happened in one run.
- One outlet, two readers, one question.
