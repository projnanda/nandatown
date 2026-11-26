"""Public command compatibility and defaults; never call model endpoints."""
import json
from pathlib import Path

import pytest

from nandatown.cli import main

URL = 'http://127.0.0.1:8765/clean.html'
QUESTION = 'When was Project Alpha launched?'


def fake_runner(monkeypatch, seen):
    def run(config, output, **kwargs):
        seen.update(config=config, output=output, kwargs=kwargs)
        return {'status': 'completed', 'question': config.question,
                'guardrail': 'promptarmor', 'stop_reason': 'mock',
                'answer': {'text': 'April 12, 2024', 'citations': [{'evidence_id': 'E1', 'url': URL}]},
                'pages_visited': 1, 'steps_used': 3, 'evidence_count': 1,
                'token_usage': {'agent1': {'total_tokens': 10}, 'guard': {'total_tokens': 5},
                                'agent2': {'total_tokens': 4}}}
    monkeypatch.setattr('nandatown.promptarmor_workflow.run', run)


def test_existing_protected_command_now_uses_promptarmor(tmp_path, monkeypatch, capsys):
    seen = {}
    fake_runner(monkeypatch, seen)
    assert main(['web-evidence-protected', '--url', URL, '--question', QUESTION,
                 '--out', str(tmp_path/'out'), '--json']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['guardrail'] == 'promptarmor'
    assert seen['config'].provider == 'openai' and seen['config'].model == 'gpt-4.1'
    assert seen['config'].temperature == 0
    assert seen['kwargs']['guard_model'] == 'gpt-6-luna'
    assert seen['kwargs']['guard_reasoning_effort'] is None  # resolved to medium in run()


def test_replay_command_uses_packet_question_without_browser(tmp_path, monkeypatch, capsys):
    seen = {}
    fake_runner(monkeypatch, seen)
    packet = tmp_path/'packet.json'
    packet.write_text(json.dumps({'question': QUESTION, 'items': [], 'complete': False}))
    assert main(['web-evidence-protected-replay', '--packet', str(packet),
                 '--out', str(tmp_path/'out')]) == 0
    assert seen['config'].question == QUESTION and seen['kwargs']['packet_path'] == str(packet)
    assert 'April 12, 2024' in capsys.readouterr().out


def test_protected_limits_are_validated(tmp_path, monkeypatch):
    seen = {}
    fake_runner(monkeypatch, seen)
    assert main(['web-evidence-protected', '--url', URL, '--question', QUESTION,
                 '--max-pages', '999', '--out', str(tmp_path/'out')]) == 2
    assert not seen


@pytest.mark.parametrize('command', ['web-evidence-protected', 'web-evidence-protected-replay'])
def test_help_does_not_need_credentials(command, capsys):
    with pytest.raises(SystemExit) as exc:
        main([command, '--help'])
    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert '--guard-model' in output and '--guard-reasoning-effort' in output
