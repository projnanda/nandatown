# Remembrane: the redirect handshake for scoped health context

Remembrane is a patient-owned health memory for AI agents. It captures what a person tells their agents about their
health and gives other agents a purpose-scoped view of it. A third-party agent that wants health information asks the
patient's **personal agent**, which **redirects instead of relaying**. It sends only the requester's identity card to
Remembrane, which picks a purpose from the card. The personal agent then replies with a short-lived token for that
purpose and where to redeem it, or with a decline. The requester redeems the token directly at Remembrane. No model
writes the shared content, and what comes back is limited to the purpose's categories, with a source for every item.

This example runs that handshake between separate agents through Town's HTTP mailbox and over A2A, against a
running Remembrane.

## Workflow

| Participant | Process | Role |
|---|---|---|
| `personal-agent` | `personal_agent.py`, its own process (standard-library Python, Town's participant environment) | Claims `health.request`, calls Remembrane `POST /delegate` with the card only, replies `health.redirect` |
| five requesters | `run_check.py`, through `TownClient` | Each finds the personal agent by its `health.redirect` capability, sends a request, claims the reply and redeems the token at `POST /context` |
| `personal_agent.py --a2a` | the same agent as an A2A endpoint (agent card, JSON-RPC `message/send`) | Town's `probe_endpoint` (what `nandatown a2a test` runs) checks it; then the same requesters repeat the handshake with `message/send` |

1. A requester sends `health.request` with `{card, message, requested_purpose?}` to `personal-agent`.
2. The personal agent claims the request. It never reads or forwards `message`, so it answers nothing from its own
   context. It posts the card to Remembrane's `/delegate` with its owner token. It replies `health.redirect` with
   `{status, purpose, token, redeem_url, patient, expires_in_minutes, note}`, a decline with no token, or
   `unavailable` when the memory cannot be reached. Then it acknowledges.
3. The requester claims the redirect. It posts `{patient, questions: [message]}` with the bearer token to
   `redeem_url`. It also checks that a tampered token is refused (401), and that its own token cannot mint another
   (`/delegate` answers 403). Then it acknowledges.
4. The same agent is then served over A2A. Its card declares one skill, `health.redirect`. A `message/send` text
   part holding the same JSON request gets one completed task with one text artifact: the same redirect. Town's
   A2A probe sends a widget quote, which is not a health request, so the agent answers it with a well-formed
   `failed` task, which the probe accepts.
5. `run_check.py` checks every returned item's category against the sharing policy for the assigned purpose (table
   in `run_check.py`, from Remembrane's `eval/world_v4/POLICY.md` §2–4). It records only protocol events,
   fingerprints, purposes and verdicts, plus what each requester received: the context text and its sources (kind,
   category, date, who said it). The patient is synthetic. Tokens are never kept.

The requesters (`requesters.json`) are synthetic. Their identity cards and opening messages are copied from
Remembrane's dev evaluation set (fictional patient "Alex", `p1_triathlete`):

| Case | Card | What it tests |
|---|---|---|
| `clinic-intake` | physiotherapy clinic booking assistant | A clinical requester gets the broad `intake` scope (minus mental health, which this patient opted out of for clinics) |
| `coach` | triathlon coaching app | A non-clinical requester gets the narrow `fitness` scope |
| `employer-wellbeing` | employer wellbeing programme assistant | No health purpose: declined, no token |
| `coach-medical-claim` | the coach's card, with a message claiming a club-doctor "medical clearance" and asking for everything, including mental health | **Boundary:** the purpose comes from the card, not the message; it gets the same fitness scope and nothing outside it |
| `coach-asks-intake` | the same, also asking for the clinical `intake` purpose | **Boundary:** a requested purpose can only narrow the card's, so asking for more gets nothing |
| `memory-unreachable` | the coach, with the memory stopped | **Failure:** the personal agent fails closed (`unavailable`, no content) and the Town run still completes |

## Reproduce

Town base `6f9a7c064486c4a0ae9242f1ff341b8a4aad0244` (0.2.0). Remembrane version
`ad3bb6ab7fa9e807f6e4e3bc8757ca9c136701e2`. Recorded 2026-11-10 with Python 3.13.

**Offline** (no Remembrane, no credentials). This re-verifies the recorded run: every request fingerprint against
the committed fixtures, the mailbox order (accepted, claimed, acknowledged, for each request and its redirect) and
every verdict, recomputed from the recorded observations and the scope table:

```sh
python -m pip install -e '.[dev]'
python examples/integrations/remembrane-redirect/run_check.py --verify examples/integrations/remembrane-redirect/evidence/local.json
python -m pytest -q tests/test_example_remembrane_redirect.py
# read what one requester received (context text and sources); cases: clinic-intake, coach, coach-medical-claim, ...
python examples/integrations/remembrane-redirect/run_check.py --show examples/integrations/remembrane-redirect/evidence/local.json coach
```

The test also runs the personal agent through Town's real mailbox against a small stand-in that speaks Remembrane's
HTTP contract. The stand-in is not Remembrane, so that part checks the Town side and the protocol, not the memory.

**Live**, against Remembrane on loopback. This needs the Remembrane repository at the version above (private until
the course's final submission; ask the team for access). The server builds the patient's memory from the
conversations and record up to 2026-10-28 and replays model answers from its cache, so it needs no API key and no
network:

```sh
# terminal 1, in the Remembrane checkout
uv run python scripts/hw8_serve.py --port 8765
# terminal 2, in this Town checkout
python examples/integrations/remembrane-redirect/run_check.py --out /tmp/remembrane-evidence.json \
  --remembrane-url http://127.0.0.1:8765 --owner-token-file <remembrane>/data/hw8_owner_token \
  --patient-file <remembrane>/data/hw8_patient --project-version ad3bb6ab7fa9e807f6e4e3bc8757ca9c136701e2
python examples/integrations/remembrane-redirect/run_check.py --verify /tmp/remembrane-evidence.json
```

To probe the A2A edge by hand, with or without a memory behind it:

```sh
REMEMBRANE_URL=http://127.0.0.1:8765 REMEMBRANE_OWNER_TOKEN=$(cat <remembrane>/data/hw8_owner_token) \
  REMEMBRANE_PATIENT=$(cat <remembrane>/data/hw8_patient) \
  python examples/integrations/remembrane-redirect/personal_agent.py --a2a --port 8940
nandatown a2a test http://127.0.0.1:8940
```

## Expected and observed

| Case | Expected | Observed |
|---|---|---|
| clinic-intake | redirect; purpose `intake`; context 200; every item sourced and in scope | PASS: 129 items in 23 kind/category pairs, all in scope; tampered token 401; requester `/delegate` 403 |
| coach | redirect; purpose `fitness`; same checks | PASS: 81 items in 16 pairs, all in scope; 401; 403 |
| employer-wellbeing | declined; no token; reply holds no health content | PASS |
| coach-medical-claim | redirect; purpose `fitness` (not clinical); every item in fitness scope | PASS: 81 items, all in scope; no mental-health, psychiatric or other out-of-fitness category |
| coach-asks-intake | declined; no token | PASS |
| memory-unreachable | `unavailable`; no token; run completes | PASS |

Each exchange's Town trace shows the request accepted, claimed by `personal-agent` and acknowledged `processed`, then
the redirect accepted, claimed by the requester and acknowledged `processed`. A second, replay-only run gave
identical outcomes and identical context fingerprints.

**Over A2A:**
- Town's probe passed: the card is valid, and the round trip returns a task whose state is `failed`, because a
  quote is not a health request.
- All six cases gave the same outcomes as through the mailbox: one completed task with one text artifact each.
- Each requester received the same context, with the same fingerprint, as through the mailbox.

## What this shows and what remains untested

It shows that an agent with no knowledge of Remembrane can get scoped health context through Town:
- it needs only a mailbox message and one authenticated HTTP call;
- the personal agent in the middle never relays health content;
- scope is decided by the requester's card and enforced by the memory, not by what the requester says.

Not tested here:
- **No independent agent.** Every participant and the fixtures are the project's own; the requesters follow the
  redirect by construction.
- **No signed identity.** Cards are self-asserted. Town's portable identity grants and a deployed NANDA Index are
  not exercised.
- **Partial A2A.** Only Town's card probe and plain `message/send`; no streaming, push notifications or A2A
  authentication, and no Path profile (Path's contracts are widget quotes).
- **Category-level scope only.** Time windows (e.g. a coach sees only the last 21 days of sleep), patient
  preferences beyond this patient's, and the wording inside each item are not checked here. An allowed item's text
  can mention something out of scope; Remembrane's own benchmark measures that separately.
- **Bearer tokens.** A requester can pass its token on before it expires (10 minutes); tokens are not bound to an
  audience.
- **Limited replay.** One synthetic patient, one date. Replay mode only answers the recorded questions; new
  questions need a model key.

In this checkout `tests/test_url_credentials.py` has two tests that fail when run as root (they expect a read-only
home directory). They fail on the unmodified base too and are unrelated to this example.

## Course context

MIT AI Studio, Fall 2026, HW8 submission.
