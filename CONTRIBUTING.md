# Contributing to Town

Start from current `origin/main`. Historical code lives on `archive/legacy`;
importing a legacy contribution requires an explicit current requirement and
tests against the rebuilt implementation.

## Python and CLI

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m pytest -q
nandatown run marketplace --out /tmp/town-lab
nandatown run quote-crash-restart --out /tmp/town-track
```

CI exercises Python 3.11 and 3.12. Local HTTP tests need permission to bind
loopback sockets. Built-in scripted and `mock:v1` runs need no model credentials.
Use a temporary `NANDATOWN_HOME` to isolate keys in manual tests.

Regenerate schemas after changing shared records:

```bash
nandatown schemas --out schemas
python -m pytest -q tests/test_schema_sync.py
git diff --check
```

The test compares every generated schema with the checked-in contracts. There
is no separate Python linter/formatter gate; avoid unrelated style changes.
Build and check the artifacts when changing packaging or bundled assets:

```bash
python -m pip install build
python -m build
python scripts/check_distributions.py dist
```

CI also installs that wheel into a clean Python 3.11 environment and runs the
Lab and Track controls outside the checkout, so source-tree imports cannot
hide missing packaged files.

### Windows setup (PowerShell)

NandaTown supports Python 3.11 and 3.12. The following steps were verified on Windows with Python 3.11.9.

**1. Preserve test fixture line endings**

Previous test fixtures contain SHA-256-verified evidence. Git's automatic LF-to-CRLF conversion on Windows changes their hashes and causes bundle verification tests to fail.

For a new clone, disable conversion before cloning:

```powershell
git -c core.autocrlf=false clone https://github.com/projnanda/nandatown.git
cd nandatown
```

For an existing checkout:

```powershell
git config core.autocrlf false
git restore --source=HEAD --worktree -- tests/fixtures
```

The restore command discards local changes to `tests/fixtures`. Preserve any intentional changes before running it.

Verify that the affected fixtures show `i/lf w/lf`:

```powershell
git ls-files --eol tests/fixtures
```

**2. Create and activate a virtual environment**

From the repository root:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

If PowerShell blocks activation:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

**3. Install development dependencies**

```powershell
python -m pip install -e ".[dev]"
python -m pip check
```

The development extra includes pytest. No additional test dependencies were required in the verified clean environment.

**4. Run tests**

```powershell
python -m pytest -q tests/test_bundle.py
python -m pytest -q
```

**5. Run CLI smoke tests**

```powershell
nandatown run marketplace --out runs/lab
nandatown run quote-crash-restart --out runs/track
```

Both scenarios run without model API credentials.

**Windows test limitations**

On Windows with Python 3.11.9, the historical bundle suite passed all 47 tests after correcting line endings.

The full suite reported 1,830 passed, 9 failed, and 26 skipped. Remaining failures involved:

- POSIX file permission assumptions (`0o600` and read-only directories).
- A test that creates a filename containing control characters, which Windows rejects.
- Subprocess timeout tests where the child PID file was not created before the deadline.
- A concurrent key-creation test where some child processes produced empty output.

The subprocess and concurrency failures require further investigation. These results do not establish that the corresponding functionality is unsupported on Windows.

Both Lab and Track CLI smoke tests completed with a `PASSED` verdict.


## Dashboard

Read `apps/nest-dashboard/AGENTS.md` and the relevant installed Next.js guides
before changing dashboard code. From that directory:

```bash
npm ci --ignore-scripts --no-audit --no-fund
npm test
npm run typecheck
npm run lint
npm run build
```

Use the Node version required by the dashboard package and CI. See its
[README](apps/nest-dashboard/README.md) for environment and deployment inputs.

## Keep changes reviewable

For a correctness fix, add a behavioral regression, observe its failure, make
the smallest repair, and run affected tests. Explain the trigger, resulting
behavior, compatibility impact and verification in the PR. Separate independent
fixes from large refactors or new protocol policies.

Keep evidence meanings precise: partial receipts are valid observations,
unchecked stages are not passes, and signatures do not establish truth or
independence. Version evaluation changes honestly and preserve an explicit
explanation when an older bundle needs its matching evaluator.

For a real-agent integration, include the bounded workflow and failure case
from the [testing guide](docs/testing-an-existing-agent.md). Town's own fixture
must not be presented as independently adopted compatibility.
