import json
import shutil
import socket
from contextlib import contextmanager
from pathlib import Path
from urllib.request import urlopen

import pytest

from nandatown.cli import _print_web_evidence_summary, main
from nandatown.web_evidence import Answer, Citation, RunResult
from nandatown.web_evidence_demo import (
    DEMO_QUESTION,
    DemoSiteUnavailable,
    _load_demo_assets,
    find_demo_site,
    serve_northstar_site,
)


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


def test_pretty_output_neutralizes_controls_without_mutating_result(capsys):
    raw = "answer\x1b]52;c;Y2xpcGJvYXJk\x07\rhidden"
    result = completed_result().model_copy(update={
        "question": raw,
        "answer": Answer(
            text=raw,
            citations=[Citation(evidence_id="E2", url="http://x/\x1b[2J")],
        ),
    })

    _print_web_evidence_summary(result, Path("runs/control"))

    output = capsys.readouterr().out
    assert "\x1b" not in output
    assert "\\x1b]52" in output
    assert "\\x0dhidden" in output
    assert result.answer.text == raw


def test_custom_run_requires_url_and_question_together(capsys):
    assert main(["web-evidence", "--url", "https://example.test"]) == 2
    assert "give both --url and --question" in capsys.readouterr().out


def test_demo_server_serves_the_real_northstar_site():
    with serve_northstar_site(port=0) as url:
        with urlopen(url, timeout=3) as response:  # noqa: S310 - loopback only
            html = response.read().decode("utf-8")

    assert "Northstar AI Labs" in html
    assert "projects.html" in html


def test_demo_path_cannot_be_replaced_from_working_directory(
        tmp_path, monkeypatch):
    shadow = tmp_path / "examples" / "web_evidence_site"
    shadow.mkdir(parents=True)
    for name in ("index.html", "projects.html", "careers.html", "styles.css"):
        (shadow / name).write_text("untrusted", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    chosen = find_demo_site()

    assert chosen != shadow
    assert chosen.name == "web_evidence_site"
    assert "nandatown" in {part.casefold() for part in chosen.parts}


def test_demo_assets_are_hash_verified(tmp_path):
    copy = tmp_path / "site"
    shutil.copytree(find_demo_site(), copy)
    (copy / "index.html").write_text("tampered", encoding="utf-8")

    with pytest.raises(DemoSiteUnavailable, match="failed verification"):
        _load_demo_assets(copy)


def test_demo_refuses_an_occupied_port():
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        port = occupied.getsockname()[1]

        with pytest.raises(DemoSiteUnavailable, match="port .* is busy"):
            with serve_northstar_site(port=port):
                pass
