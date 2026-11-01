"""Independent E2E/replay orchestration; baseline Agent implementations stay intact."""
from __future__ import annotations
import json
import os
from pathlib import Path
import time
import uuid
from urllib.parse import urlsplit

import httpx
from .web_evidence import (RunConfig, EvidencePacket, EvidenceCollector, AnswerGenerator,
    PlaywrightBrowser, EventLog, TerminalRenderer, ModelReply, ToolCall, _write_json,
    COLLECTOR_SYSTEM_PROMPT, ANSWERER_SYSTEM_PROMPT)
from .promptarmor_guard import Guard, DefenseError, PROVENANCE



class Wire:
    """No automatic retries, secrets in headers only; serial transport."""
    def __init__(self, model, base_url, key_env, log, actor):
        self.model, self.log, self.actor = model, log, actor
        self.field = None
        key = os.environ.get(key_env)
        if not key:
            raise ValueError(f'Missing environment variable {key_env}')
        self.http = httpx.Client(base_url=base_url, headers={'Authorization': 'Bearer ' + key},
                                 trust_env=False, transport=httpx.HTTPTransport(retries=0))
        self.usage = {}

    def post(self, messages, timeout_seconds, tools=None, json_mode=False):
        payload = {'model': self.model, 'messages': messages, 'temperature': 0, 'stream': False}
        if getattr(self, 'reasoning_effort', None) is not None:
            payload['reasoning_effort'] = self.reasoning_effort
            if self.reasoning_effort != 'none':
                payload.pop('temperature', None)
        if tools:
            payload.update(tools=tools, tool_choice='auto')
        if json_mode:
            payload['response_format'] = {'type': 'json_object'}
        self.log.emit(self.actor, 'api_request', {'payload': payload, 'field': self.field,
                       'timeout_seconds': timeout_seconds, 'endpoint': '/chat/completions'})
        try:
            response = self.http.post('/chat/completions', json=payload, timeout=timeout_seconds)
            if response.status_code >= 400:
                try:
                    error = response.json().get('error', {})
                except ValueError:
                    error = {}
                safe_error = {k: v[:120] for k, v in error.items() if k in ('type', 'code', 'param') and isinstance(v, str)} if isinstance(error, dict) else {}
                self.log.emit(self.actor, 'api_error', {'status_code': response.status_code, **safe_error})
                raise RuntimeError('Model endpoint returned an error; response message omitted')
            body = response.json()
            usage = body.get('usage') or {}
            for key, value in usage.items():
                if type(value) is int:
                    self.usage[key] = self.usage.get(key, 0) + value
            self.log.emit(self.actor, 'api_response', {'body': body, 'field': self.field})
            return body
        except Exception as exc:
            self.log.emit(self.actor, 'transport_error', {'type': type(exc).__name__})
            raise

    def close(self):
        self.http.close()


class AgentClient(Wire):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, actor='agent-api', **kwargs)
        # Chat Completions tool calls require reasoning disabled for GPT-6.
        self.reasoning_effort = 'none' if self.model.startswith('gpt-6') else None

    def complete(self, messages, *, tools=None, json_mode=False, timeout_seconds=None):
        body = self.post(messages, timeout_seconds, tools, json_mode)
        choice = body['choices'][0]
        msg = choice['message']
        calls = [ToolCall(call_id=c['id'], name=c['function']['name'],
                          arguments=json.loads(c['function']['arguments'])) for c in msg.get('tool_calls') or []]
        return ModelReply(content=msg.get('content'), tool_calls=calls,
                          usage={k: v for k, v in (body.get('usage') or {}).items() if type(v) is int},
                          finish_reason=choice.get('finish_reason'))


class GuardClient(Wire):
    def __init__(self, *args, reasoning_effort=None, **kwargs):
        super().__init__(*args, actor='guard', **kwargs)
        self.reasoning_effort = reasoning_effort
        self.timeout_seconds = 120.0

    def complete(self, system, data):
        body = self.post([{'role': 'system', 'content': system}, {'role': 'user', 'content': data}],
                         self.timeout_seconds, json_mode=True)
        return body['choices'][0]['message'].get('content') or ''


def default_config(url, question, model='gpt-4.1', base_url='https://api.openai.com/v1'):
    return RunConfig(url=url, question=question, provider='openai', base_url=base_url, model=model, temperature=0)


def validate_endpoint(url):
    parts = urlsplit(url)
    if (parts.scheme not in ('http', 'https') or not parts.hostname
            or parts.username is not None or parts.password is not None
            or parts.query or parts.fragment):
        raise ValueError('Model endpoint must be HTTP(S) without credentials, query or fragment')


def run(config, out, *, packet_path=None, guard_model='gpt-6-luna',
        guard_base_url='https://api.openai.com/v1', guard_key_env='OPENAI_API_KEY',
        agent_key_env='OPENAI_API_KEY', agent=None, guard_client=None, browser=None, guard_reasoning_effort=None):
    validate_endpoint(config.base_url)
    validate_endpoint(guard_base_url)
    if config.provider != 'openai':
        raise ValueError('Protected workflow requires the OpenAI-compatible transport')
    if guard_model in ('gpt-4.1', 'gpt-4.1-mini'):
        if guard_reasoning_effort is not None:
            raise ValueError('GPT-4.1 guard models do not support reasoning_effort')
    elif guard_reasoning_effort is None:
        guard_reasoning_effort = 'medium'
    output = Path(out)
    if output.is_symlink() or (output.exists() and (not output.is_dir() or any(output.iterdir()))):
        raise ValueError('Output must be new or empty')
    output.mkdir(parents=True, exist_ok=True)
    log = EventLog(output / 'events.jsonl', TerminalRenderer(enabled=False))
    _write_json(output / 'config.json', {'agents': config.model_dump(), 'guard': {
        'model': guard_model, 'base_url': guard_base_url,
        'temperature': None if guard_reasoning_effort not in (None, 'none') else 0,
        'temperature_policy': 'omitted for active reasoning' if guard_reasoning_effort not in (None, 'none') else 'explicit zero',
        'reasoning_parameter': guard_reasoning_effort or 'omitted',
        'on_detect': 'remove', 'removal_length_guard': 3.0, 'include_definition': False,
        'per_text_timeout_seconds': 120, 'total_timeout_seconds': 600, 'automatic_retries': 0},
        'provenance': PROVENANCE})
    result = {'run_id': 'pa-' + uuid.uuid4().hex, 'status': 'error', 'answer': None,
              'question': config.question, 'provenance': PROVENANCE, 'factual_support_assessed': False,
              'guardrail': 'promptarmor', 'guardrail_version': 'promptarmor-handoff@1',
              'pages_visited': 0, 'steps_used': 0, 'evidence_count': 0,
              'token_usage': {'agent1': {}, 'guard': {}, 'agent2': {}}, 'timing_seconds': {},
              'limitations': ['Metadata is not screened.', 'Only detected spans are removed.',
                              'Sanitized text is not verbatim source evidence.']}
    owned_agent, owned_guard = agent is None, guard_client is None
    collector = generator = None
    phase = 'setup'
    started = time.monotonic()
    try:
        # Enforce exact baseline prompts; no security appendix enters either Agent.
        if config.collector_prompt != COLLECTOR_SYSTEM_PROMPT or config.answerer_prompt != ANSWERER_SYSTEM_PROMPT:
            raise ValueError('This workflow requires the unchanged baseline prompts')
        remaining = config.run_timeout_seconds
        if packet_path is not None:
            raw = Path(packet_path).read_bytes()
            packet = EvidencePacket.model_validate_json(raw)
            if packet.question != config.question:
                raise ValueError('Replay question differs from packet')
            (output / 'handoff.json').write_bytes(raw)
        else:
            phase = 'agent1'
            agent = agent or AgentClient(config.model, config.base_url, agent_key_env, log)
            browser = browser or PlaywrightBrowser(config, deadline=started + remaining)
            (output / 'pages').mkdir()
            browser.start()
            collector = EvidenceCollector(config, agent, browser, log,
                lambda page: _write_json(output / 'pages' / f'{page.page_id}.json', page), started + remaining)
            packet = collector.run()
            result['token_usage']['agent1'] = dict(collector.usage)
            _write_json(output / 'handoff.json', packet)
            remaining -= time.monotonic() - started
        result['evidence_count'] = len(packet.items)
        result['timing_seconds']['agent1'] = time.monotonic() - started if collector else 0
        log.emit('runner', 'original_handoff', {'packet': packet.model_dump(), 'provenance': PROVENANCE})
        if not packet.items:
            result.update(status='partial', stop_reason='No evidence; no Guard or Agent 2 call')
            return result
        if remaining <= 0:
            result.update(status='limit_reached', stop_reason='Original Agent time budget exhausted')
            return result
        phase = 'guard'
        guard_tick = time.monotonic()
        guard_client = guard_client or GuardClient(guard_model, guard_base_url, guard_key_env, log, reasoning_effort=guard_reasoning_effort)
        guard = Guard(guard_client, log, output / 'promptarmor.json')
        _write_json(output / 'manifest.json', {'upstream': guard.identity, 'replay': packet_path is not None,
                    'guard_usage_source': 'API usage, when available', 'provenance': PROVENANCE})
        cleaned = guard.packet(packet)
        result['timing_seconds']['guard'] = time.monotonic() - guard_tick
        result['token_usage']['guard'] = dict(getattr(guard_client, 'usage', {}))
        # Only an all-fields-successful guard may create the deliverable packet.
        _write_json(output / 'agent2_handoff.json', cleaned)
        log.emit('runner', 'sanitized_handoff', {'packet': cleaned.model_dump(), 'provenance': PROVENANCE})
        phase = 'agent2'
        agent = agent or AgentClient(config.model, config.base_url, agent_key_env, log)
        tick = time.monotonic()
        generator = AnswerGenerator(config, agent, log, tick + remaining)
        answer = generator.run(cleaned)
        result['timing_seconds']['agent2'] = time.monotonic() - tick
        result.update(status='completed' if packet.complete else 'partial', answer=answer.model_dump(),
                      stop_reason='Guard completed and Agent 2 returned structured output; factual accuracy not graded')
    except Exception as exc:
        result.update(status='defense_error' if phase == 'guard' else 'error',
                      stop_reason=f'{phase} failed ({type(exc).__name__}); no fallback used')
        log.emit('runner', 'run_error', {'phase': phase, 'type': type(exc).__name__})
    finally:
        if collector:
            result['pages_visited'] = len(collector.pages)
            result['steps_used'] = collector.steps
            result['token_usage']['agent1'] = dict(collector.usage)
        if guard_client:
            result['token_usage']['guard'] = dict(getattr(guard_client, 'usage', {}))
        if generator:
            result['token_usage']['agent2'] = dict(generator.usage)
        if browser:
            browser.close()
        if owned_agent and agent:
            agent.close()
        if owned_guard and guard_client:
            guard_client.close()
        result['timing_seconds']['total'] = time.monotonic() - started
        _write_json(output / 'result.json', result)
    return result
