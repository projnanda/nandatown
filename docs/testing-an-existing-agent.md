# Test an existing agent with Town

Town can test an existing A2A endpoint or a program that joins its HTTP mailbox.
Start with one synthetic quote, read the stage report, then check the evidence.
You do not need to move an A2A agent or give its model credentials to Town.

## 1. Install and check the local control

From a checkout of the current repository, with Python 3.11 or newer:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
nandatown run quote-clean
```

This checks Town's own reference participants. It does not test your agent yet.
The command prints the result and evidence directory.

## 2. Choose the connection your agent supports

| Agent setup | Town entry point |
| --- | --- |
| A2A AgentCard and `message/send` endpoint | `nandatown test-agent --url URL` |
| Program that joins Town's HTTP mailbox | `nandatown test-agent --cmd "your command"` |
| Mailbox participant started separately | `nandatown test-agent --wait` |

The runtime name alone is not a connector. OpenClaw, Hermes, LangGraph and other
agents need one of these interfaces or a small adapter. Town does not install
skills into them, sign them into a model provider, or turn an ordinary chat CLI
into an A2A server. Record the adapter alongside the agent version when testing.

For the mailbox interface, `examples/byoa_seller.py` shows a standard-library
Python participant. Calibrate that path with:

```bash
nandatown test-agent --role seller --cmd "python examples/byoa_seller.py"
```

It receives `TOWN_URL`, `RUN_ID`, `NAME`, `TOKEN`, `STATE_DIR` and
`DEADLINE`, the seconds this run will wait, plus `FAULT` for a profile that
scripts one, which only Town's own scripted participants act on; these are
temporary test credentials and state. Commands run with your OS privileges.
Review custom commands and plugins before executing them. This example is a
Town fixture, so its success is not an independent-agent result.

For external mailbox participants, Town records the connector kind but not an
immutable agent release. Keep your agent and adapter versions with the result.
Rerun recipes omit your raw command or A2A connector URL; resupply those inputs
instead of expecting the bundle to contain credentials or a runnable copy of your agent.

## 3. Run the synthetic quote against your endpoint

Set the URL to an endpoint you own or are authorized to test:

```bash
AGENT_URL=http://127.0.0.1:8940
nandatown test-agent --url "$AGENT_URL" --path-profile a2a-capability-fulfillment@0.3
```

Town sends a JSON quote request for two widgets at 1,995 cents each, with a fresh
`request_id` and nonce. The returned quote must match the selected profile and
request ID. Town deliberately repeats the same logical request. Use a synthetic
test agent: a production tool that treats this message as purchase authorization
is not an appropriate target.

This pinned profile uses evaluator contract `path-evaluator@0.2` and records
result evaluator version `path-0.3`. It requires a `completed` task and exactly one text
part containing the JSON quote on each attempt. Multiple text parts or a task
still marked `working` do not meet that contract. This is a small synthetic
workflow, not a universal A2A output rule.

An endpoint behind basic authentication can take its credentials in the URL,
as `http://user:password@host:port`. Town sends them to that endpoint as
written, and never prints or records the credentials it recognises: output,
evidence and Pulse history show the URL with them as `<credentials 1a2b3c4d>`,
a digest keyed by a secret in your Town home, so two sets of credentials for one
host stay distinguishable without revealing either. Receipts show
`<credentials withheld>`, and so does anything recorded while that key cannot be
read or created; Town warns when that happens, and what it recorded meanwhile
stays withheld. The recorded rerun asks for the URL again, because the
credentials were never written down.

Town recognises credentials only where httpx finds them: everything before the
last `@` that comes before the first `/`, `?` or `#`. A password may contain
quotes, brackets or spaces. Two kinds of secret can go unrecognised:

- **A `/`, `?` or `#` in a password.** Percent-encode it (as `%2F`, `%3F`,
  `%23`). Unencoded, httpx ends the host at that character, and what happens
  depends on what comes before it:
  - **It no longer parses,** as in `http://user:pass/word@host`, where `pass`
    is not a port. Town does not call it, and withholds everything before its
    last `@` as it would credentials.
  - **It still reads as a host,** as in `http://user:1234/word@host`,
    `http://token/word@host` or `http://user:a@b/c@host`. The URL goes to the
    host before that character (`user`, `token` and `b` in these examples),
    with no credentials or only the part of the password before an `@`, which
    is recognised. The rest of the password is printed and recorded as part of
    the URL, exactly as written. Town cannot tell it from an ordinary `@` in a
    path, such as `/users/a@b`, so it neither refuses nor rewrites the URL. `test-agent --url`, `a2a test` and `pulse` print a note,
    without the URL, when they see an `@` after the host; a URL read from an
    `--index` file gets no note.
- **A secret anywhere else,** such as a token in a query string or a header,
  which is used, printed and recorded as written.

An agent card that repeats the URL with its credentials is shown and recorded
with them withheld, but its recorded digest covers the card as served, so a
weak password could be guessed offline from a bundle. Don't let a card
advertise credentials.

Labels belong to one Town home:
Pulse history read from another home labels its older entries afresh. A Town
from before this change reports a new receipt's withheld subject as not matching
its bundle when checked with `--bundle`; checked without it, the receipt
verifies. Evidence recorded by an earlier Town still holds credentials on disk:
reports, `replay`, `visualize` and new receipts withhold the ones Town
recognises, but the bundle itself is left as recorded, so rerun rather than
share it.

For local A2A calibration, run `nandatown a2a serve --port 8940` in another
terminal first. Stop it with Ctrl-C when finished. To test your own agent,
replace that server with your agent and use its actual URL.

A reachable LAN endpoint works too, including an agent on another laptop. Use
that agent's reachable A2A port; an OpenClaw gateway port is not automatically an
A2A port. `--wait` has a different network shape: the remote participant must
reach Town's coordinator, which the automatic runner binds to loopback. Use a
tunnel or the [shared coordinator guide](operators.md). For a participant on a
different machine, replace the printed `STATE_DIR` with its own writable local
directory; Town's filesystem path does not exist on the remote machine. The
printed credentials include `DEADLINE`, the seconds this run will wait, and
never `FAULT`: an agent joining from outside is the subject, and Town does
not tell a subject to misbehave.

## 4. Read what passed and what did not run

The report separates resolution, card retrieval, descriptor consistency,
protocol invocation, semantic result and duplicate-request behavior. It names
the first failed or inconclusive boundary. A Town driver error is a Town error,
not evidence that the agent failed the task.

An unpinned run can pass the exercised checks while leaving
`descriptor_consistency` **Not tested**. That receipt is useful evidence, but
cannot receive `TOWN-TESTED`.

For descriptor coverage, obtain the expected full AgentCard fingerprint from
the owner or a separately pinned artifact. Town fingerprints the parsed card
using `nandatown.records.fingerprint`, not arbitrary JSON whitespace. The
observed digest is in the `card_retrieved` event. Copying that observation back
as the expected value tests consistency on the next run; it does not independently
authenticate the owner.

```bash
nandatown test-agent --url "$AGENT_URL" --path-profile a2a-capability-fulfillment@0.3 --pin-card-digest sha256:THE_FULL_EXPECTED_CARD_DIGEST
```

Use `--path-profile` to select a versioned Path contract. `--profile` belongs to
mailbox Track runs. For explicit item/color/merchant/currency requirements, see
the [quote-intent profile](../README.md#check-the-item-not-only-the-price).

## 5. Verify and share a reproducible result

Replace the directory below with the one Town printed:

```bash
BUNDLE=runs/YOUR_RUN_ID
nandatown report "$BUNDLE"
nandatown verify "$BUNDLE"
nandatown visualize "$BUNDLE"
nandatown receipt "$BUNDLE"
nandatown verify-receipt "$BUNDLE/receipt.json" --bundle "$BUNDLE"
nandatown proof "$BUNDLE"
```

`proof` returns nonzero with a reason when coverage is partial, the result did
not pass, evidence is stale, or verification fails. A refusal does not invalidate
an honest signed partial receipt. Complete passing evidence can render the scoped
badge; the signature proves key commitment, not agent safety or independent agreement.
`receipt` and `verify-receipt --bundle` refuse a bundle that fails `verify`,
with one exception: a bundle recorded by a known earlier evaluator version for
its mode is accepted, and the receipt states `evaluator replay not checked`
among its signed limitations, so the disclosure travels with the receipt.
A bundle naming an unrecognised evaluator version is refused.

Review URLs, labels, events, receipts and attachments for secrets before sharing.
Do not send `state/` or private keystores in a public handoff. Preserve the five
canonical files and `manifest.json` unchanged; include the optional receipt and
attestation when relevant.

Send a reviewer this small packet:

- Town commit/version, selected profile and evaluator version.
- Agent/runtime/adapter versions, expected card digest, and what remains unpinned.
- Exact command with credentials removed, bundle, and bundle fingerprint.
- Baseline result, useful failure, change made, and rerun result.
- Whether to verify offline, repeat the live run, or both.

Offline verification repeats evaluation over recorded observations. Live
reproduction calls the agent again and can change with its environment. Neither
is established merely by attaching a screenshot.

## What counts as a real product test

Use an independently developed agent and identify a failure that its existing
tests missed. After fixing it, have another operator verify the evidence and,
where practical, reproduce the live run. Record what Town made easier to test or
diagnose. Reference-server tests are prerequisites, not proof of outside adoption
or full NANDA/A2A conformance.

The local JSON index option is a fixture, not a deployed NANDA Index integration.
Discovery, identity, tools, payment and physical outcomes need profiles and actual
components that exercise them. See [proposal alignment](convergence.md).
