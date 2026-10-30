import json
from contextlib import contextmanager
from pathlib import Path
from urllib.request import urlopen

from nandatown.cli import main
from nandatown.web_evidence import Answer, Citation, RunResult
from nandatown.web_evidence_demo import DEMO_QUESTION, serve_northstar_site


def completed_result() -> RunResult:
    return RunResult(
        run_id="web-test",
        status="completed",
        stop_reason="relevant source found",
        question=DEMO_QUESTION,
        answer=Answer(
            text="The project started on March 14, 2024.",
            citations=[Citation(
                evidence_id="E2",
                url="http://127.0.0.1:8765/projects.html",
            )],
        ),
        evidence_count=3,
        pages_visited=2,
        steps_used=6,
        token_usage={"total_tokens": 4321},
    )


@contextmanager
def fake_demo_site():
    yield "http://127.0.0.1:8765/index.html"


def install_fake_run(monkeypatch, observed):
    def fake_run(config, output, *, renderer):
        observed.update(config=config, output=output, renderer=renderer)
        return Path(output), completed_result()

    monkeypatch.setattr(
        "nandatown.web_evidence_demo.serve_northstar_site", fake_demo_site)
    monkeypatch.setattr("nandatown.web_evidence.run_web_evidence", fake_run)


def test_zero_argument_demo_uses_northstar_defaults_and_pretty_output(
        tmp_path, monkeypatch, capsys):
    observed = {}
    install_fake_run(monkeypatch, observed)

    assert main(["web-evidence", "--out", str(tmp_path / "run")]) == 0

    output = capsys.readouterr().out
    assert observed["config"].question == DEMO_QUESTION
    assert observed["config"].url.endswith("/index.html")
    assert observed["renderer"].enabled is True
    assert "Web evidence" in output
    assert "March 14, 2024" in output
    assert "[E2]" in output
    assert "2 pages | 6 steps | 3 evidence items" in output


def test_json_output_is_clean_and_disables_live_renderer(
        tmp_path, monkeypatch, capsys):
    observed = {}
    install_fake_run(monkeypatch, observed)

    assert main(["web-evidence", "--json", "--out",
                 str(tmp_path / "run")]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "completed"
    assert payload["answer"]["citations"][0]["evidence_id"] == "E2"
    assert observed["renderer"].enabled is False


def test_custom_run_requires_url_and_question_together(capsys):
    assert main(["web-evidence", "--url", "https://example.test"]) == 2
    assert "give both --url and --question" in capsys.readouterr().out


def test_demo_server_serves_the_real_northstar_site():
    with serve_northstar_site(port=0) as url:
        with urlopen(url, timeout=3) as response:  # noqa: S310 - loopback only
            html = response.read().decode("utf-8")

    assert "Northstar AI Labs" in html
    assert "projects.html" in html
