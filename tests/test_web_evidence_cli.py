import json
from pathlib import Path

import pytest

from nandatown.cli import _print_web_evidence_summary, main
from nandatown.web_evidence import Answer, Citation, RunResult


QUESTION = "When was Project Alpha launched?"
URL = "http://127.0.0.1:8765/clean.html"


def completed_result() -> RunResult:
    return RunResult(
        run_id="web-test",
        status="completed",
        stop_reason="relevant source found",
        question=QUESTION,
        answer=Answer(
            text="The project started on March 14, 2024.",
            citations=[Citation(
                evidence_id="E2",
                url=URL,
            )],
        ),
        evidence_count=3,
        pages_visited=2,
        steps_used=6,
        token_usage={"total_tokens": 4321},
    )


def install_fake_run(monkeypatch, observed):
    def fake_run(config, output, *, renderer):
        observed.update(config=config, output=output, renderer=renderer)
        directory = Path(output)
        directory.mkdir(parents=True)
        (directory / "result.json").write_text(completed_result().model_dump_json(), encoding="utf-8")
        return directory, completed_result()

    monkeypatch.setattr(
        "nandatown.web_evidence.run_web_evidence",
        fake_run,
    )


def test_custom_workflow_uses_required_inputs_and_pretty_output(
        tmp_path, monkeypatch, capsys):
    observed = {}
    install_fake_run(monkeypatch, observed)

    assert main([
        "web-evidence", "--url", URL, "--question", QUESTION,
        "--out", str(tmp_path / "run"),
    ]) == 0

    output = capsys.readouterr().out
    assert observed["config"].model == "gpt-4.1"
    assert observed["config"].provider == "openai"
    assert observed["config"].question == QUESTION
    assert observed["config"].url == URL
    assert observed["renderer"].enabled is True
    assert "Web evidence" in output
    assert "March 14, 2024" in output
    assert "[E2]" in output
    assert "2 pages | 6 steps | 3 evidence items" in output


def test_openai_provider_selects_openai_configuration(
        tmp_path, monkeypatch, capsys):
    observed = {}
    install_fake_run(monkeypatch, observed)

    assert main([
        "web-evidence", "--provider", "openai", "--model", "gpt-6-luna",
        "--url", URL, "--question", QUESTION,
        "--out", str(tmp_path / "run"),
    ]) == 0

    assert observed["config"].provider == "openai"
    assert observed["config"].model == "gpt-6-luna"
    assert observed["config"].base_url == "https://api.openai.com/v1"
    assert observed["config"].reasoning_effort == "none"
    assert observed["config"].temperature == 0


def test_json_output_is_clean_and_disables_live_renderer(
        tmp_path, monkeypatch, capsys):
    observed = {}
    install_fake_run(monkeypatch, observed)

    assert main(["web-evidence", "--url", URL, "--question", QUESTION,
                 "--json", "--out",
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


@pytest.mark.parametrize("missing", ["--url", "--question"])
def test_url_and_question_are_both_required(missing):
    provided = ["web-evidence"]
    if missing != "--url":
        provided.extend(["--url", URL])
    if missing != "--question":
        provided.extend(["--question", QUESTION])

    with pytest.raises(SystemExit) as exc:
        main(provided)

    assert exc.value.code == 2
