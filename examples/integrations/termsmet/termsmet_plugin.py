"""TermsMet Town Lab plugin: scripted buyer/provider, MCP adapter, validators.

Buyer and provider are deterministic, inspectable state machines. They act
only on Town messages they actually receive. ``tm_adapter`` is the only role
that calls the real TermsMet MCP service (through ``mcp_bridge.py``); it maps
the Town envelope sender to a fixed server identity and returns the unchanged
server result over Town.

Importing this module never imports the MCP SDK or starts a server, so the
validators can replay a bundle offline. The bridge is loaded lazily by the
adapter's ``on_start``.
"""
from __future__ import annotations

import csv
import hashlib
import importlib.util
import io
import json
import os
import time
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from nandatown.records import StageResult
from nandatown.sim.agents import SimAgent, role
from nandatown.sim.validators import validator

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / 'fixtures'
BUYER, PROVIDER, ADAPTER = 'tm_buyer', 'tm_provider', 'tm_adapter'
IDENTITIES = {BUYER: 'buyer', PROVIDER: 'provider'}
TEMPLATE_ID = 'order_extraction_v1'
REPAIR_WINDOW = 10
BASE_STAGES = ('agreement_funding', 'delivery_repair', 'release_gating', 'retrieved_artifact')

_BRIDGES: list[Any] = []


def close_termsmet() -> None:
    """Close every plugin-owned bridge; safe after partial initialization."""
    failures = []
    while _BRIDGES:
        bridge = _BRIDGES.pop()
        try:
            bridge.close()
        except Exception as exc:  # noqa: BLE001 - report category only
            failures.append(getattr(exc, 'category', type(exc).__name__))
    if failures:
        raise RuntimeError(f'TermsMet bridge cleanup failed: {failures}')


def _load_bridge_module():
    spec = importlib.util.spec_from_file_location('termsmet_mcp_bridge', HERE / 'mcp_bridge.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _error_code(result):
    if isinstance(result, dict) and isinstance(result.get('error'), dict):
        return result['error'].get('code')
    return None


# ---------------------------------------------------------------- roles


class _Participant(SimAgent):
    """Shared request bookkeeping for the scripted buyer and provider."""

    def on_start(self):
        self.adapter = self.config.get('adapter', ADAPTER)
        self.pending: dict[str, str] = {}
        self.sent_requests: dict[str, dict[str, Any]] = {}
        self.counter = 0
        self.stopped = False

    def _request(self, tool, arguments, purpose, **extra):
        self.counter += 1
        request_id = f"{self.name.removeprefix('tm_')}-{purpose.replace('_', '-')}-{self.counter}"
        body = {'request_id': request_id, 'tool': tool, 'arguments': arguments, 'purpose': purpose}
        body.update(extra)
        self.pending[request_id] = purpose
        self.sent_requests[request_id] = body
        self.api.send(self.adapter, 'tm_tool_request', body)
        return request_id

    def _state(self, state, **detail):
        self.api.observe('tm_role_state', self.name, dict(detail, state=state))

    def _fail(self, reason, **detail):
        self.stopped = True
        self.api.observe('tm_role_failed', self.name, dict(detail, reason=reason))

    def handle_tm_tool_result(self, msg):
        if self.stopped:
            return
        body = msg['body']
        purpose = self.pending.pop(body.get('request_id'), None)
        if purpose is None:
            self.api.observe('tm_unexpected_result', self.name, {'request_id': body.get('request_id')})
            return
        if 'transport_error' in body:
            self._fail('transport_error', request_id=body['request_id'], category=body['transport_error'])
            return
        getattr(self, f'after_{purpose}')(body['result'], body['request_id'])

    handle_tm_repair_result = handle_tm_tool_result


@role('termsmet_buyer')
class TermsMetBuyer(_Participant):
    def on_start(self):
        super().on_start()
        self.provider = self.config.get('provider', PROVIDER)
        self.source_csv = (FIXTURES / 'orders.csv').read_text(encoding='utf-8')
        self.poll_interval = float(self.config.get('poll_interval_seconds', 0.1))
        self.poll_limit = float(self.config.get('poll_limit_seconds', 30))
        self.job_id = None
        self.submission_version = None
        self.api.later(0.5, self.propose)

    def propose(self):
        self._request('propose_agreement', {'template_id': TEMPLATE_ID, 'source_csv': self.source_csv}, 'propose')

    def after_propose(self, result, _rid):
        if _error_code(result) or result.get('version') != 1:
            return self._fail('proposal_not_created')
        self.job_id = result['job_id']
        self._request('retrieve_delivery', {'job_id': self.job_id}, 'early_retrieve')

    def after_early_retrieve(self, result, _rid):
        if _error_code(result) != 'PERMISSION_DENIED':
            return self._fail('early_retrieval_not_denied')
        self._state('early_retrieval_denied', job_id=self.job_id)
        self.api.send(self.provider, 'tm_job_created', {'job_id': self.job_id, 'version': 1})

    def handle_tm_countered(self, msg):
        if self.stopped or msg['body'].get('job_id') != self.job_id:
            return
        # Verify the counter against the server rather than trusting the message.
        self._request('get_job', {'job_id': self.job_id}, 'review_terms')

    def after_review_terms(self, result, _rid):
        terms = result.get('terms') or {}
        if (result.get('version') != 2 or terms.get('repair_window_minutes') != REPAIR_WINDOW
                or terms.get('max_repairs') != 1 or result.get('source_csv') != self.source_csv):
            return self._fail('countered_terms_unacceptable')
        self._request('accept_agreement', {'job_id': self.job_id, 'version': 1}, 'stale_accept')

    def after_stale_accept(self, result, _rid):
        if _error_code(result) != 'VERSION_MISMATCH':
            return self._fail('stale_acceptance_not_rejected')
        self._request('accept_agreement', {'job_id': self.job_id, 'version': 2}, 'accept')

    def after_accept(self, result, _rid):
        if result.get('status') != 'ACCEPTED' or sorted(result.get('accepted_by', [])) != ['buyer', 'provider']:
            return self._fail('agreement_not_accepted')
        self._request('fund_job', {'job_id': self.job_id}, 'fund')

    def after_fund(self, result, _rid):
        if result.get('status') != 'FUNDED' or result.get('simulated') is not True:
            return self._fail('funding_not_confirmed')
        self._state('funded', job_id=self.job_id)
        self.api.send(self.provider, 'tm_funded', {'job_id': self.job_id, 'version': 2, 'status': 'FUNDED'})

    def handle_tm_delivery_ready(self, msg):
        body = msg['body']
        if self.stopped or body.get('job_id') != self.job_id:
            return
        self.submission_version = body.get('submission_version')
        self._request('get_job', {'job_id': self.job_id}, 'review_status')

    def after_review_status(self, result, _rid):
        status = result.get('status')
        self._state('review_observed', job_id=self.job_id, status=status,
                    review_deadline=(result.get('deadlines') or {}).get('review'))
        if status == 'IN_REVIEW':
            self._request('retrieve_delivery', {'job_id': self.job_id}, 'review_retrieve')
        elif status == 'RELEASED':
            self._start_polling()
        else:
            self._fail('unexpected_status_after_delivery', status=status)

    def after_review_retrieve(self, result, _rid):
        if _error_code(result) != 'PERMISSION_DENIED':
            return self._fail('retrieval_during_review_not_denied')
        self._start_polling()

    def _start_polling(self):
        self.poll_started = time.monotonic()
        self.polls = 0
        self.api.later(0.1, self._poll)

    def _poll(self):
        if self.stopped:
            return
        # Real wall-clock pause: Town's logical later() cannot age the server's review window.
        time.sleep(self.poll_interval)
        self.polls += 1
        self._request('get_result', {'job_id': self.job_id, 'submission_version': self.submission_version},
                      'release_poll')

    def after_release_poll(self, result, _rid):
        if _error_code(result):
            return self._fail('release_poll_error', code=_error_code(result))
        if result.get('release_allowed') is True and (result.get('settlement') or {}).get('status') == 'RELEASED':
            self._state('release_observed', job_id=self.job_id, polls=self.polls)
            self._request('retrieve_delivery', {'job_id': self.job_id}, 'retrieve')
            return
        if time.monotonic() - self.poll_started > self.poll_limit:
            return self._fail('release_poll_limit_exceeded', polls=self.polls)
        self.api.later(0.1, self._poll)

    def after_retrieve(self, result, _rid):
        delivery = result.get('delivery')
        if _error_code(result) or not isinstance(delivery, list):
            return self._fail('retrieval_failed')
        self._state('delivery_retrieved', job_id=self.job_id, rows=len(delivery))
        self._request('list_jobs', {}, 'final_jobs')

    def after_final_jobs(self, result, _rid):
        self._state('done', job_id=self.job_id, jobs=len(result.get('jobs', [])))
        self.api.send(self.provider, 'tm_done', {'job_id': self.job_id})


@role('termsmet_provider')
class TermsMetProvider(_Participant):
    def on_start(self):
        super().on_start()
        self.buyer = self.config.get('buyer', BUYER)
        self.reply_timeout = float(self.config.get('reply_timeout', 2.0))
        # Controlled fault injection: the first draft omits order A102.
        self.initial = json.loads((FIXTURES / 'initial_delivery.json').read_text(encoding='utf-8'))
        self.job_id = None
        self.source_csv = None
        self.repair_request = None
        self.retried = False
        self.confirmed = False

    def handle_tm_job_created(self, msg):
        if self.stopped or self.job_id is not None:
            return
        self.job_id = msg['body'].get('job_id')
        self._request('get_job', {'job_id': self.job_id}, 'inspect_proposal')

    def after_inspect_proposal(self, result, _rid):
        if result.get('status') != 'PROPOSED' or result.get('version') != 1:
            return self._fail('unexpected_proposal')
        self._request('counter_agreement', {'job_id': self.job_id, 'repair_window_minutes': REPAIR_WINDOW}, 'counter')

    def after_counter(self, result, _rid):
        if result.get('version') != 2:
            return self._fail('counter_not_recorded')
        self._request('accept_agreement', {'job_id': self.job_id, 'version': 2}, 'accept')

    def after_accept(self, result, _rid):
        if _error_code(result) or result.get('version') != 2:
            return self._fail('acceptance_failed')
        self.api.send(self.buyer, 'tm_countered', {'job_id': self.job_id, 'version': 2})

    def handle_tm_funded(self, msg):
        if self.stopped or msg['body'].get('job_id') != self.job_id:
            return
        self._request('counter_agreement', {'job_id': self.job_id, 'repair_window_minutes': 5}, 'locked_counter')

    def after_locked_counter(self, result, _rid):
        if _error_code(result) != 'INVALID_STATE':
            return self._fail('locked_counter_not_rejected')
        self._request('get_job', {'job_id': self.job_id}, 'check_funded')

    def after_check_funded(self, result, _rid):
        if result.get('status') != 'FUNDED' or result.get('version') != 2:
            return self._fail('job_not_funded')
        self.source_csv = result['source_csv']
        self._request('submit_delivery', {'job_id': self.job_id, 'delivery': self.initial}, 'initial')

    def after_initial(self, result, _rid):
        if result.get('submission_version') != 1 or result.get('verdict') != 'FAIL':
            return self._fail('unexpected_first_verdict', verdict=result.get('verdict'))
        self._request('get_result', {'job_id': self.job_id, 'submission_version': 1}, 'read_report')

    def after_read_report(self, result, _rid):
        errors = result.get('errors') or []
        missing = {e.get('order_id') for e in errors if e.get('code') == 'MISSING_ORDER'}
        if not missing or len(missing) != len(errors):
            return self._fail('report_not_repairable', errors=errors)
        repaired = self._repair(missing)
        self._state('repair_built_from_report', job_id=self.job_id, missing=sorted(missing))
        self.repair_request = self._request(
            'submit_delivery', {'job_id': self.job_id, 'delivery': repaired}, 'repair')
        self.api.later(self.reply_timeout, self._check_repair_reply)

    def _repair(self, missing):
        """Insert missing orders from the agreed CSV, using only the FAIL report."""
        repaired = list(self.initial)
        for position, row in enumerate(csv.DictReader(io.StringIO(self.source_csv))):
            if row['Order'] in missing:
                quantity = int(row['Units'])
                cents = int((Decimal(row['Price'].removeprefix('$')) * 100)
                            .quantize(Decimal('1'), rounding=ROUND_HALF_UP))
                repaired.insert(position, {'order_id': row['Order'], 'quantity': quantity,
                                           'unit_price_cents': cents, 'total_cents': quantity * cents})
        return repaired

    def after_repair(self, result, rid):
        self._state('repair_result_received', job_id=self.job_id, request_id=rid,
                    verdict=result.get('verdict'))
        if result.get('submission_version') != 2 or result.get('verdict') != 'PASS':
            return self._fail('repair_did_not_pass', verdict=result.get('verdict'))
        self._request('get_result', {'job_id': self.job_id, 'submission_version': 2}, 'confirm_report')

    def _check_repair_reply(self):
        if self.stopped or self.retried or self.repair_request not in self.pending:
            return
        # No reply arrived: the outcome is unconfirmed, not a checker UNKNOWN or a failure.
        self.retried = True
        self._state('unconfirmed', job_id=self.job_id, request_id=self.repair_request)
        original = self.sent_requests[self.repair_request]
        self._request('submit_delivery', original['arguments'], 'repair_retry', retry_of=self.repair_request)

    def after_repair_retry(self, result, rid):
        # A rejected retry says nothing about the original submission's verdict.
        self._state('retry_answered', job_id=self.job_id, request_id=rid, error_code=_error_code(result))
        self._request('get_job', {'job_id': self.job_id}, 'recovery_status')

    def after_recovery_status(self, result, _rid):
        status = result.get('status')
        if status not in ('IN_REVIEW', 'RELEASED'):
            return self._fail('unexpected_recovery_status', status=status)
        self._request('get_result', {'job_id': self.job_id, 'submission_version': 2}, 'confirm_report')

    def after_confirm_report(self, result, rid):
        if (result.get('submission_version') != 2 or result.get('verdict') != 'PASS'
                or result.get('agreement_version') != 2):
            return self._fail('submission_2_not_confirmed')
        self.confirmed = True
        self._state('confirmed_pass', job_id=self.job_id, submission_version=2, basis=rid)
        self.api.send(self.buyer, 'tm_delivery_ready', {'job_id': self.job_id, 'submission_version': 2})

    def handle_tm_done(self, msg):
        self._state('done', job_id=msg['body'].get('job_id'))


@role('termsmet_adapter')
class TermsMetAdapter(SimAgent):
    """The only role that calls TermsMet. Identity comes from the Town sender."""

    def on_start(self):
        self.identities = dict(IDENTITIES)
        self.repair_reply_sent = False
        module = _load_bridge_module()
        self.bridge_error = module.BridgeError
        bridge = module.TermsMetBridge(
            os.environ['TERMSMET_PROJECT_DIR'], os.environ['TERMSMET_PROJECT_PYTHON'],
            os.environ['TERMSMET_DB_PATH'], review_seconds=float(os.environ['TERMSMET_REVIEW_SECONDS']),
            startup_timeout=20, call_timeout=10)
        _BRIDGES.append(bridge)
        inventory = bridge.start()
        self.bridge = bridge
        self.api.observe('tm_adapter_ready', self.name,
                         {'buyer_tools': sorted(inventory['buyer']),
                          'provider_tools': sorted(inventory['provider'])})

    def handle_tm_tool_request(self, msg):
        body = msg['body']
        identity = self.identities.get(msg['sender'])
        request_id, tool, arguments = body.get('request_id'), body.get('tool'), body.get('arguments')
        if identity is None or not isinstance(request_id, str) or not isinstance(tool, str) \
                or not isinstance(arguments, dict):
            self.api.observe('tm_adapter_rejected', msg['message_id'], {'sender': msg['sender']})
            return
        detail = {'request_id': request_id, 'request_message_id': msg['message_id'],
                  'sender': msg['sender'], 'identity': identity, 'tool': tool, 'arguments': arguments}
        reply = {'request_id': request_id}
        try:
            result = self.bridge.call(identity, tool, arguments, request_id=request_id)
            detail['result'] = result
            reply['result'] = result
        except self.bridge_error as exc:
            detail['transport_error'] = exc.category
            reply['transport_error'] = exc.category
        record = next((r for r in reversed(self.bridge.records) if r['request_id'] == request_id), {})
        detail.update({k: record.get(k) for k in ('call_id', 'requested_at', 'returned_at')})
        self.api.observe('tm_server_call', request_id, detail)
        kind = 'tm_tool_result'
        if body.get('purpose') == 'repair' and not self.repair_reply_sent:
            kind = 'tm_repair_result'
            self.repair_reply_sent = True
        self.api.reply(msg, kind, reply)


# ------------------------------------------------------------ validators


def _strict_equal(a, b):
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_strict_equal(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(_strict_equal(x, y) for x, y in zip(a, b))
    return a == b


def _ts(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


class _Check(Exception):
    pass


def _need(ok, note):
    if not ok:
        raise _Check(note)


class _TermsMetTrace:
    """Index of the Town events a TermsMet run leaves behind."""

    def __init__(self, trace):
        self.events = trace.events
        self.pos = {e.event_id: i for i, e in enumerate(self.events)}
        self.sent = {e.subject: e for e in self.events if e.kind == 'message_sent'}
        self.delivered: dict[str, list] = {}
        for e in self.events:
            if e.kind == 'message_delivered':
                self.delivered.setdefault(e.subject, []).append(e)
        self.dropped = [e for e in self.events if e.kind == 'message_dropped']
        self.calls = [e for e in self.events if e.kind == 'tm_server_call' and e.observer == ADAPTER]
        self.requests = {}
        self.replies = {}
        for e in self.sent.values():
            body = e.detail.get('body') or {}
            if e.detail.get('kind') == 'tm_tool_request':
                self.requests.setdefault(body.get('request_id'), []).append(e)
            elif e.observer == ADAPTER and e.detail.get('kind') in ('tm_tool_result', 'tm_repair_result'):
                self.replies.setdefault(body.get('request_id'), []).append(e)
        self.failures = [e for e in self.events if e.kind in ('tm_role_failed', 'tm_adapter_rejected',
                                                              'tm_unexpected_result', 'message_unhandled')]

    def i(self, event):
        return self.pos[event.event_id]

    def purpose(self, call):
        return (self.requests[call.detail['request_id']][0].detail['body']).get('purpose')

    def find(self, tool=None, identity=None, purpose=None):
        return [c for c in self.calls
                if (tool is None or c.detail.get('tool') == tool)
                and (identity is None or c.detail.get('identity') == identity)
                and (purpose is None or self.purpose(c) == purpose)]

    def one(self, note, **conds):
        found = self.find(**conds)
        _need(len(found) == 1, f'{note}: expected exactly one call, found {len(found)}')
        return found[0]

    def delivered_to(self, call, recipient):
        """The reply to ``call`` delivered to ``recipient`` (first delivery event) or None."""
        for reply in self.replies.get(call.detail['request_id'], []):
            for d in self.delivered.get(reply.subject, []):
                if d.detail.get('to') == recipient:
                    return d
        return None

    def role_states(self, name, state):
        return [e for e in self.events
                if e.kind == 'tm_role_state' and e.observer == name and e.detail.get('state') == state]

    def global_problems(self):
        problems = []
        if not self.calls:
            problems.append('no TermsMet server calls recorded')
        if not [e for e in self.events if e.kind == 'run_finished']:
            problems.append('run did not finish')
        if self.failures:
            problems.append(f'role failure or rejection events: {[e.event_id for e in self.failures][:4]}')
        if not [e for e in self.events if e.kind == 'tm_adapter_ready' and e.observer == ADAPTER]:
            problems.append('adapter never reported a started bridge')
        for request_id, messages in self.requests.items():
            if len(messages) != 1:
                problems.append(f'request id {request_id!r} reused')
        for call in self.calls:
            d = call.detail
            requests = self.requests.get(d.get('request_id'), [])
            if len(requests) != 1:
                problems.append(f'{call.event_id} has no unique Town request')
                continue
            req = requests[0]
            body = req.detail['body']
            if (req.observer not in IDENTITIES or IDENTITIES[req.observer] != d.get('identity')
                    or d.get('sender') != req.observer or req.detail.get('to') != ADAPTER
                    or body.get('tool') != d.get('tool')
                    or not _strict_equal(body.get('arguments'), d.get('arguments'))):
                problems.append(f'{call.event_id} does not match its Town request')
            if not any(x.detail.get('to') == ADAPTER and self.i(x) < self.i(call)
                       for x in self.delivered.get(req.subject, [])):
                problems.append(f'{call.event_id} ran without a delivered Town request')
            if 'transport_error' in d or 'result' not in d:
                problems.append(f'{call.event_id} had a transport error')
                continue
            replies = self.replies.get(d['request_id'], [])
            if len(replies) != 1 or not _strict_equal((replies[0].detail['body']).get('result'), d['result']) \
                    or self.i(replies[0]) < self.i(call):
                problems.append(f'{call.event_id} result was not relayed unchanged')
        if len({c.detail.get('call_id') for c in self.calls}) != len(self.calls):
            problems.append('server call ids are not unique')
        return problems

    def job_id(self):
        propose = self.one('proposal', tool='propose_agreement')
        return propose.detail['result'].get('job_id')

    def final_receipt(self):
        receipts = [c for c in self.find(tool='get_result') if isinstance(c.detail['result'].get('receipt'), dict)]
        _need(receipts, 'no terminal receipt observed')
        return receipts[-1], receipts[-1].detail['result']['receipt']


def _stage(name, fn, tm, extra_problems=()):
    try:
        problems = list(extra_problems) + tm.global_problems()
        if not tm.calls:
            return StageResult(name=name, status='not_enough_evidence', note='no TermsMet calls in trace')
        if problems:
            return StageResult(name=name, status='failed', evidence=[c.event_id for c in tm.calls[:1]],
                               note='; '.join(problems)[:500])
        evidence, note = fn(tm)
        return StageResult(name=name, status='passed', evidence=evidence[:8], note=note)
    except _Check as exc:
        return StageResult(name=name, status='failed', evidence=[c.event_id for c in tm.calls[:1]],
                           note=str(exc))
    except (KeyError, TypeError, AttributeError, ValueError, IndexError) as exc:
        return StageResult(name=name, status='not_enough_evidence', note=f'malformed or missing evidence: {exc!r}')


def _fixture(name):
    return (FIXTURES / name).read_text(encoding='utf-8')


def _agreement_funding(tm):
    job = tm.job_id()
    _need(all(c.detail['arguments'].get('job_id', job) == job for c in tm.calls), 'calls reference another job')
    propose = tm.one('proposal', tool='propose_agreement', identity='buyer')
    p = propose.detail['result']
    _need(p.get('version') == 1 and p.get('status') == 'PROPOSED', 'proposal was not version 1')
    _need(propose.detail['arguments'].get('source_csv') == _fixture('orders.csv'), 'proposal source differs from fixture')
    _need(p.get('source_hash') == hashlib.sha256(_fixture('orders.csv').encode()).hexdigest(), 'source hash mismatch')
    counter = tm.one('counter', purpose='counter', identity='provider', tool='counter_agreement')
    _need(counter.detail['arguments'].get('repair_window_minutes') == REPAIR_WINDOW
          and counter.detail['result'].get('version') == 2, 'counter did not create v2 with 10 minutes')
    p_accept = tm.one('provider acceptance', purpose='accept', identity='provider', tool='accept_agreement')
    b_accept = tm.one('buyer acceptance', purpose='accept', identity='buyer', tool='accept_agreement')
    stale = tm.one('stale acceptance', purpose='stale_accept', identity='buyer')
    locked = tm.one('locked counter', purpose='locked_counter', identity='provider')
    fund = tm.one('funding', tool='fund_job', identity='buyer')
    _need(p_accept.detail['arguments'].get('version') == 2 and not _error_code(p_accept.detail['result']),
          'provider did not accept v2')
    _need(b_accept.detail['arguments'].get('version') == 2 and b_accept.detail['result'].get('status') == 'ACCEPTED'
          and sorted(b_accept.detail['result'].get('accepted_by', [])) == ['buyer', 'provider'],
          'buyer acceptance did not lock v2 for both parties')
    _need(stale.detail['arguments'].get('version') == 1 and tm.i(counter) < tm.i(stale)
          and _error_code(stale.detail['result']) == 'VERSION_MISMATCH', 'stale v1 acceptance not rejected')
    _need(tm.i(b_accept) < tm.i(locked) and _error_code(locked.detail['result']) == 'INVALID_STATE',
          'counter after lock not rejected')
    _need(tm.i(b_accept) < tm.i(fund) and fund.detail['result'].get('status') == 'FUNDED'
          and fund.detail['result'].get('simulated') is True, 'simulated funding not confirmed after acceptance')
    reviewed = tm.one('buyer review of counter', purpose='review_terms', identity='buyer')
    _need(tm.i(reviewed) < tm.i(b_accept) and reviewed.detail['result'].get('terms', {}).get('repair_window_minutes') == 10,
          'buyer did not verify the countered terms before accepting')
    ev = [propose, counter, stale, p_accept, b_accept, locked, fund]
    return [e.event_id for e in ev], f'job {job}: v2 (repair window 10) accepted by both; simulated funding'


def _delivery_repair(tm):
    submits = tm.find(tool='submit_delivery', identity='provider')
    first = tm.one('first submission', purpose='initial', tool='submit_delivery')
    repair = tm.one('repair submission', purpose='repair', tool='submit_delivery')
    report = tm.one('first report', purpose='read_report', tool='get_result')
    r1, r2 = first.detail['result'], repair.detail['result']
    _need(_strict_equal(first.detail['arguments']['delivery'], json.loads(_fixture('initial_delivery.json'))),
          'first draft is not the declared controlled draft')
    _need(r1.get('submission_version') == 1 and r1.get('verdict') == 'FAIL', 'first submission was not a computed FAIL')
    _need(report.detail['arguments'].get('submission_version') == 1
          and report.detail['result'].get('errors') == [{'code': 'MISSING_ORDER', 'order_id': 'A102'}],
          'report did not show MISSING_ORDER A102')
    seen = tm.delivered_to(report, PROVIDER)
    repair_req = tm.requests[repair.detail['request_id']][0]
    _need(seen is not None and tm.i(seen) < tm.i(repair_req), 'provider did not receive the report before repairing')
    _need(r2.get('submission_version') == 2 and r2.get('verdict') == 'PASS' and r2.get('release_allowed') is False,
          'repair was not a real submission-2 PASS')
    accepted = [c for c in submits if not _error_code(c.detail['result'])]
    _need(len(accepted) == 2, f'expected two accepted submissions, found {len(accepted)}')
    return ([first.event_id, report.event_id, seen.event_id, repair.event_id],
            'submission 1 FAIL (MISSING_ORDER A102) -> report read -> submission 2 PASS')


def _release_gating(tm):
    early = tm.one('early retrieval', purpose='early_retrieve', identity='buyer')
    fund = tm.one('funding', tool='fund_job')
    _need(tm.i(early) < tm.i(fund) and _error_code(early.detail['result']) == 'PERMISSION_DENIED',
          'retrieval before acceptance was not denied')
    status = tm.find(purpose='review_status', identity='buyer')
    _need(len(status) == 1, 'buyer did not read the review deadline')
    deadline = _ts(status[0].detail['result']['deadlines']['review'])
    in_review = tm.find(purpose='review_retrieve', identity='buyer')
    for c in in_review:
        _need(_error_code(c.detail['result']) == 'PERMISSION_DENIED', 'retrieval during review was not denied')
    retrieves = [c for c in tm.find(tool='retrieve_delivery') if not _error_code(c.detail['result'])]
    _need(len(retrieves) == 1, 'expected exactly one successful retrieval')
    polls = tm.find(purpose='release_poll')
    released_polls = [c for c in polls if (c.detail['result'].get('settlement') or {}).get('status') == 'RELEASED']
    _need(released_polls and tm.i(released_polls[0]) < tm.i(retrieves[0]), 'retrieval not preceded by observed release')
    for c in polls:
        if c is not released_polls[0] and tm.i(c) < tm.i(released_polls[0]):
            _need((c.detail['result'].get('settlement') or {}).get('status') == 'NONE', 'unexpected settlement during review')
    _receipt_call, receipt = tm.final_receipt()
    events = receipt['settlement_events']
    _need([e['event_type'] for e in events] == ['SIMULATED_FUNDING', 'SIMULATED_RELEASE']
          and all(e['simulated'] is True for e in events), 'settlement is not one funding and one release')
    _need(receipt['status'] == 'RELEASED' and receipt['cancellation_reason'] is None and receipt['challenges'] == [],
          'terminal receipt not cleanly RELEASED')
    released_at = _ts(events[1]['created_at'])
    _need(released_at >= deadline, 'release recorded before the real review deadline')
    real_wait = (released_at - _ts(repair_pass_time(tm))).total_seconds()
    return ([early.event_id, status[0].event_id] + [c.event_id for c in in_review]
            + [released_polls[0].event_id, retrieves[0].event_id],
            f'release at/after review deadline; {len(polls)} real-time polls; '
            f'release {real_wait:.2f}s after PASS; no refund')


def repair_pass_time(tm):
    _call, receipt = tm.final_receipt()
    return receipt['check_results'][1]['checked_at']


def _retrieved_artifact(tm):
    expected = json.loads(_fixture('expected_delivery.json'))
    retrieve = [c for c in tm.find(tool='retrieve_delivery') if not _error_code(c.detail['result'])]
    _need(len(retrieve) == 1, 'no single successful retrieval')
    delivery = retrieve[0].detail['result'].get('delivery')
    _need(tm.delivered_to(retrieve[0], BUYER) is not None, 'retrieved delivery never reached the buyer')
    _need(isinstance(delivery, list) and all(isinstance(r, dict) for r in delivery), 'delivery is not a list of records')
    ids = [r.get('order_id') for r in delivery]
    _need(len(ids) == len(set(ids)), 'duplicate order ids')
    _need(all(set(r) == {'order_id', 'quantity', 'unit_price_cents', 'total_cents'} for r in delivery),
          'unexpected or missing fields')
    _need(all(type(r[k]) is int for r in delivery for k in ('quantity', 'unit_price_cents', 'total_cents')),
          'non-integer amounts')
    _need(all(r['quantity'] * r['unit_price_cents'] == r['total_cents'] for r in delivery), 'line totals inconsistent')
    _need(_strict_equal(delivery, expected), 'retrieved delivery differs from the independent expected fixture')
    summary = (len(delivery), sum(r['quantity'] for r in delivery), sum(r['total_cents'] for r in delivery))
    _need(summary == (3, 6, 3560), f'summary {summary} differs from (3, 6, 3560)')
    propose = tm.one('proposal', tool='propose_agreement')
    source_hash, checker = propose.detail['result']['source_hash'], propose.detail['result']['checker_version']
    _receipt_call, receipt = tm.final_receipt()
    _need([a['version'] for a in receipt['agreement_versions']] == [1, 2]
          and receipt['agreement_versions'][1]['repair_window_minutes'] == 10
          and receipt['agreement_versions'][1]['max_repairs'] == 1
          and receipt['agreement_versions'][1]['template_id'] == TEMPLATE_ID, 'receipt agreement versions differ')
    _need([(a['version'], a['identity']) for a in receipt['acceptances']] == [(2, 'buyer'), (2, 'provider')],
          'receipt acceptances differ')
    _need([(s['submission_version'], s['agreement_version']) for s in receipt['submissions']] == [(1, 2), (2, 2)],
          'receipt does not show exactly two submissions on v2')
    _need([r['verdict'] for r in receipt['check_results']] == ['FAIL', 'PASS']
          and receipt['check_results'][0]['errors'] == [{'code': 'MISSING_ORDER', 'order_id': 'A102'}]
          and receipt['check_results'][1]['errors'] == [], 'receipt check results differ')
    _need(all(r['source_hash'] == source_hash and r['checker_version'] == checker for r in receipt['check_results'])
          and all(a['source_hash'] == source_hash for a in receipt['agreement_versions']),
          'source or checker version changed')
    jobs = tm.one('final job listing', purpose='final_jobs')
    _need(len(jobs.detail['result']['jobs']) == 1, 'database holds more than one job')
    _need(tm.role_states(BUYER, 'done') and tm.role_states(PROVIDER, 'done'), 'participants did not finish')
    return ([retrieve[0].event_id, _receipt_call.event_id, jobs.event_id],
            'buyer received 3 orders / 6 units / 3560 cents, equal to the independent fixture')


def _lost_reply_recovery(tm):
    repair = tm.one('repair submission', purpose='repair', tool='submit_delivery')
    rid = repair.detail['request_id']
    _need(len(tm.dropped) == 1, f'expected exactly one dropped message, found {len(tm.dropped)}')
    drop = tm.dropped[0]
    lost = tm.sent.get(drop.subject)
    _need(lost is not None and drop.detail.get('fault') == 'drop' and drop.detail.get('kind') == 'tm_repair_result'
          and lost.detail.get('kind') == 'tm_repair_result' and lost.detail.get('to') == PROVIDER,
          'dropped message is not the repair result to the provider')
    body = lost.detail['body']
    _need(body.get('request_id') == rid and _strict_equal(body.get('result'), repair.detail['result'])
          and body['result'].get('submission_version') == 2 and body['result'].get('verdict') == 'PASS',
          'dropped message was not the server-returned submission-2 PASS')
    _need(tm.i(repair) < tm.i(drop), 'drop does not follow the server PASS')
    _need(not tm.delivered.get(drop.subject), 'dropped message was delivered anyway')
    _need(tm.delivered_to(repair, PROVIDER) is None, 'provider received the repair result')
    _need(not tm.role_states(PROVIDER, 'repair_result_received'), 'provider claims to have received the repair result')
    unconfirmed = tm.role_states(PROVIDER, 'unconfirmed')
    _need(len(unconfirmed) == 1 and unconfirmed[0].detail.get('request_id') == rid
          and tm.i(drop) < tm.i(unconfirmed[0]), 'provider did not record the repair as unconfirmed')
    retries = tm.find(purpose='repair_retry')
    _need(len(retries) == 1, f'expected exactly one retry, found {len(retries)}')
    retry = retries[0]
    retry_req = tm.requests[retry.detail['request_id']][0]
    _need(retry_req.detail['body'].get('retry_of') == rid and retry.detail['request_id'] != rid
          and tm.i(unconfirmed[0]) < tm.i(retry_req), 'retry not linked to the unconfirmed request')
    _need(_strict_equal(retry.detail['arguments'], repair.detail['arguments']), 'retry changed the job or delivery')
    _need(_error_code(retry.detail['result']) == 'INVALID_STATE', 'retry was not rejected with INVALID_STATE')
    retry_seen = tm.delivered_to(retry, PROVIDER)
    _need(retry_seen is not None, 'retry rejection never reached the provider')
    status = tm.one('recovery status', purpose='recovery_status', identity='provider')
    confirm = tm.one('submission 2 report', purpose='confirm_report', identity='provider')
    _need(tm.i(retry_seen) < tm.i(tm.requests[status.detail['request_id']][0])
          and status.detail['result'].get('status') in ('IN_REVIEW', 'RELEASED'), 'state query missing or wrong')
    _need(confirm.detail['arguments'].get('submission_version') == 2 and confirm.detail['result'].get('verdict') == 'PASS',
          'submission 2 report did not confirm PASS')
    confirm_seen = tm.delivered_to(confirm, PROVIDER)
    confirmed = tm.role_states(PROVIDER, 'confirmed_pass')
    _need(confirm_seen is not None and len(confirmed) == 1 and tm.i(confirm_seen) < tm.i(confirmed[0]),
          'provider confirmed PASS before receiving the report')
    ready = [e for e in tm.sent.values() if e.detail.get('kind') == 'tm_delivery_ready']
    _need(len(ready) == 1 and tm.i(confirmed[0]) < tm.i(ready[0]), 'delivery announced before confirmation')
    _need(len(tm.find(tool='submit_delivery')) == 3, 'unexpected number of submit attempts')
    _call, receipt = tm.final_receipt()
    _need(len(receipt['submissions']) == 2 and
          [e['event_type'] for e in receipt['settlement_events']] == ['SIMULATED_FUNDING', 'SIMULATED_RELEASE'],
          'recovery created extra submissions or settlements')
    return ([repair.event_id, drop.event_id, unconfirmed[0].event_id, retry.event_id, status.event_id,
             confirm.event_id, confirmed[0].event_id, ready[0].event_id],
            f"PASS dropped in transit; provider unconfirmed -> 1 retry rejected INVALID_STATE -> "
            f"status {status.detail['result'].get('status')} + report -> confirmed")


def _clean_only(tm):
    problems = []
    if tm.dropped:
        problems.append('clean run contains dropped messages')
    if tm.find(purpose='repair_retry') or tm.role_states(PROVIDER, 'unconfirmed'):
        problems.append('clean run contains a retry')
    repair = tm.find(purpose='repair')
    if len(repair) == 1 and tm.delivered_to(repair[0], PROVIDER) is None:
        problems.append('clean run repair result not delivered to provider')
    return problems


@validator('termsmet_clean')
def termsmet_clean(spec, trace):
    tm = _TermsMetTrace(trace)
    extra = _clean_only(tm)
    return [_stage('agreement_funding', _agreement_funding, tm, extra),
            _stage('delivery_repair', _delivery_repair, tm, extra),
            _stage('release_gating', _release_gating, tm, extra),
            _stage('retrieved_artifact', _retrieved_artifact, tm, extra)]


@validator('termsmet_lost_reply')
def termsmet_lost_reply(spec, trace):
    tm = _TermsMetTrace(trace)
    return [_stage('agreement_funding', _agreement_funding, tm),
            _stage('delivery_repair', _delivery_repair, tm),
            _stage('release_gating', _release_gating, tm),
            _stage('retrieved_artifact', _retrieved_artifact, tm),
            _stage('lost_reply_recovery', _lost_reply_recovery, tm)]
