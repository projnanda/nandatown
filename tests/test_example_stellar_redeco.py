"""The stellar-redeco example must never pass without its external agent.

The subject (stellar-redeco-agent) is hosted separately and is not installed
in CI, so this checks only the Town side: with no agent listening, every
judged stage is inconclusive and the run is not a pass. A live compatibility
result needs the agent; see examples/integrations/stellar-redeco/README.md.
"""

import os

from nandatown.sim.runner import run_lab

HERE = os.path.join(os.path.dirname(__file__), "..", "examples", "integrations", "stellar-redeco")


def test_unreachable_agent_is_inconclusive_not_passed(tmp_path, monkeypatch):
    monkeypatch.setenv("REDECO_AGENT_URL", "http://127.0.0.1:9")  # nothing listens on the discard port
    for name in ("redeco_compliance.yaml", "redeco_portal_down.yaml"):
        _, result = run_lab(os.path.join(HERE, name), str(tmp_path))
        judged = [s for s in result.stages if s.name not in ("ledger_conserved", "privacy")]
        assert result.verdict != "passed"
        assert judged and all(s.status == "not_enough_evidence" for s in judged), \
            [(s.name, s.status) for s in judged]
