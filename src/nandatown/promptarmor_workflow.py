"""Pinned PromptArmor adapter and compatible E2E/replay entry points."""
from __future__ import annotations
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from .web_evidence import (RunConfig, EventLog, ModelReply, ToolCall, _write_json,
    COLLECTOR_SYSTEM_PROMPT, ANSWERER_SYSTEM_PROMPT)
from .promptarmor_guard import Guard, PROVENANCE



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


class PromptArmorAdapter:
    """Pinned LLM sanitization with fail-closed handoff and original-source audit."""

    name = 'llm'
    log_type = EventLog

    def __init__(self, config, *, guard_model, guard_base_url, guard_key_env,
                 agent_key_env, guard_reasoning_effort, agent=None, guard_client=None,
                 replay=False):
        self.config = config
        self.guard_model, self.guard_base_url = guard_model, guard_base_url
        self.guard_key_env, self.agent_key_env = guard_key_env, agent_key_env
        self.effort, self.client = guard_reasoning_effort, guard_client
        self.owned_guard = guard_client is None
        self.replay = replay

    @property
    def usage(self):
        return getattr(self.client, 'usage', {})

    def configuration(self, config):
        return {'guard': {
            'model': self.guard_model, 'base_url': self.guard_base_url,
            'temperature': None if self.effort not in (None, 'none') else 0,
            'reasoning_parameter': self.effort or 'omitted',
            'on_detect': 'remove', 'removal_length_guard': 3.0, 'include_definition': False,
            'per_text_timeout_seconds': 120, 'total_timeout_seconds': 600,
            'automatic_retries': 0}, 'provenance': PROVENANCE,
            'effective_agent_parameters': {'model': config.model, 'temperature': 0,
                'reasoning_effort': 'none' if config.model.startswith('gpt-6') else None},
            'agent_transport_retries': 0, 'guard_time_excluded': True}

    def open_collector(self, config, log):
        service = AgentClient(config.model, config.base_url, self.agent_key_env, log)
        return service, service

    def defend(self, packet, output, log):
        from .evidence_workflow import DefenseOutcome
        self.client = self.client or GuardClient(self.guard_model, self.guard_base_url,
            self.guard_key_env, log, reasoning_effort=self.effort)
        guard = Guard(self.client, log, output / 'promptarmor.json')
        _write_json(output / 'manifest.json', {'upstream': guard.identity, 'replay': self.replay,
                    'guard_usage_source': 'API usage, when available', 'provenance': PROVENANCE})
        cleaned = guard.packet(packet)
        log.emit('runner', 'sanitized_handoff', {'packet': cleaned.model_dump(), 'provenance': PROVENANCE})
        return DefenseOutcome(cleaned, 'complete', {
            'provenance': PROVENANCE, 'guardrail': 'promptarmor',
            'guardrail_version': 'promptarmor-handoff@1'},
            {'model': self.config.model, 'reason': 'fixed_agent2',
             'reasoning_effort': 'none' if self.config.model.startswith('gpt-6') else None,
             'temperature': 0})

    def open_answerer(self, config, service, outcome, output, log):
        if service is not None:
            return service, None, config
        client, handle = self.open_collector(config, log)
        return client, handle, config

    def close(self):
        if self.owned_guard and self.client is not None:
            self.client.close()


def run(config, out, *, packet_path=None, guard_model='gpt-6-luna',
        guard_base_url='https://api.openai.com/v1', guard_key_env='OPENAI_API_KEY',
        agent_key_env='OPENAI_API_KEY', agent=None, guard_client=None, browser=None,
        guard_reasoning_effort=None):
    """Compatibility entry point backed by the shared experiment runner."""
    from .evidence_workflow import run_workflow
    validate_endpoint(config.base_url)
    validate_endpoint(guard_base_url)
    if config.provider != 'openai':
        raise ValueError('Protected workflow requires the OpenAI-compatible transport')
    if config.collector_prompt != COLLECTOR_SYSTEM_PROMPT or config.answerer_prompt != ANSWERER_SYSTEM_PROMPT:
        raise ValueError('This workflow requires the unchanged baseline prompts')
    if guard_model in ('gpt-4.1', 'gpt-4.1-mini'):
        if guard_reasoning_effort is not None:
            raise ValueError('GPT-4.1 guard models do not support reasoning_effort')
    elif guard_reasoning_effort is None:
        guard_reasoning_effort = 'medium'
    adapter = PromptArmorAdapter(config, guard_model=guard_model, guard_base_url=guard_base_url,
        guard_key_env=guard_key_env, agent_key_env=agent_key_env,
        guard_reasoning_effort=guard_reasoning_effort, agent=agent, guard_client=guard_client,
        replay=packet_path is not None)
    return run_workflow(config, out, adapter, model=agent, browser=browser, packet_path=packet_path)
