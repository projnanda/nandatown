"""TermsMet Town integration: validators over the recorded evidence bundles.

These tests need neither the TermsMet runtime nor the MCP SDK. They replay
the plugin's validators over the committed clean and lost-reply bundles and
check that mutated traces (missing events, wrong money or versions, no real
drop, early knowledge, tampering) do not pass.
"""
import copy
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

from nandatown.bundle import verify_bundle
from nandatown.records import TownEvent
from nandatown.sim.validators import VALIDATORS, Trace

ROOT = pathlib.Path(__file__).resolve().parents[1]
INTEGRATION = ROOT / 'examples/integrations/termsmet'
EVIDENCE = pathlib.Path(os.environ.get('TERMSMET_EVIDENCE', INTEGRATION / 'evidence'))
BASE = ['agreement_funding', 'delivery_repair', 'release_gating', 'retrieved_artifact']


def load_plugin():
    spec = importlib.util.spec_from_file_location('termsmet_plugin_under_test', INTEGRATION / 'termsmet_plugin.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_events(case):
    with open(EVIDENCE / case / 'events.jsonl', encoding='utf-8') as f:
        return [json.loads(line) for line in f]


def evaluate(validator, events):
    trace = Trace([TownEvent.model_validate(e) for e in events])
    return {stage.name: stage.status for stage in VALIDATORS[validator](None, trace)}


def calls(events, purpose=None, tool=None):
    requests = {e['detail']['body'].get('request_id'): e['detail']['body'] for e in events
                if e['kind'] == 'message_sent' and e['detail'].get('kind') == 'tm_tool_request'}
    return [e for e in events if e['kind'] == 'tm_server_call'
            and (purpose is None or requests[e['detail']['request_id']].get('purpose') == purpose)
            and (tool is None or e['detail']['tool'] == tool)]


def mutate_result(events, purpose, change):
    """Change a server result consistently in the adapter record and its Town reply."""
    events = copy.deepcopy(events)
    (call,) = calls(events, purpose=purpose)
    change(call['detail']['result'])
    for e in events:
        if (e['kind'] == 'message_sent' and e['observer'] == 'tm_adapter'
                and e['detail']['body'].get('request_id') == call['detail']['request_id']):
            change(e['detail']['body']['result'])
    return events


def mutate_receipts(events, change):
    """Change every terminal receipt consistently in adapter records and Town replies."""
    events = copy.deepcopy(events)
    for e in events:
        body = e['detail'].get('body') or {}
        result = e['detail'].get('result') if e['kind'] == 'tm_server_call' else body.get('result')
        if isinstance(result, dict) and isinstance(result.get('receipt'), dict):
            change(result['receipt'])
    return events


class TermsMetIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for case in ('clean', 'lost_reply'):
            if not (EVIDENCE / case / 'events.jsonl').is_file():
                raise unittest.SkipTest(f'no recorded {case} bundle at {EVIDENCE}')
        load_plugin()
        cls.clean = load_events('clean')
        cls.lost = load_events('lost_reply')

    def assert_not_passed(self, statuses, stage):
        self.assertIn(stage, statuses)
        self.assertNotEqual(statuses[stage], 'passed', statuses)

    def test_plugin_import_does_not_load_mcp_or_bridge(self):
        code = ('import importlib.util,sys; s=importlib.util.spec_from_file_location("p",sys.argv[1]);'
                'm=importlib.util.module_from_spec(s); s.loader.exec_module(m);'
                'print(any(k=="mcp" or k.startswith("mcp.") or "bridge" in k for k in sys.modules))')
        out = subprocess.run([sys.executable, '-c', code, str(INTEGRATION / 'termsmet_plugin.py')],
                             capture_output=True, text=True, check=True, timeout=60)
        self.assertEqual(out.stdout.strip(), 'False')

    def test_recorded_bundles_verify_and_all_stages_pass(self):
        for case, validator, stages in (('clean', 'termsmet_clean', BASE),
                                        ('lost_reply', 'termsmet_lost_reply', BASE + ['lost_reply_recovery'])):
            self.assertEqual(verify_bundle(str(EVIDENCE / case)), [], case)
            statuses = evaluate(validator, load_events(case))
            for stage in stages:
                self.assertEqual(statuses[stage], 'passed', (case, statuses))

    def test_runner_offline_verify(self):
        out = subprocess.run([sys.executable, str(INTEGRATION / 'run.py'), '--verify', str(EVIDENCE)],
                             capture_output=True, text=True, timeout=120, cwd=ROOT)
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_missing_required_event_does_not_pass(self):
        (fund,) = calls(self.clean, tool='fund_job')
        events = [e for e in self.clean if e is not fund]
        self.assert_not_passed(evaluate('termsmet_clean', events), 'agreement_funding')
        (retrieve,) = calls(self.clean, purpose='retrieve')
        events = [e for e in self.clean if e is not retrieve]
        statuses = evaluate('termsmet_clean', events)
        self.assert_not_passed(statuses, 'retrieved_artifact')
        self.assert_not_passed(statuses, 'release_gating')

    def test_wrong_money_or_type_does_not_pass(self):
        def wrong_total(result):
            result['delivery'][1]['total_cents'] = 2501

        def float_amount(result):
            result['delivery'][0]['total_cents'] = 700.0

        def dropped_row(result):
            del result['delivery'][1]

        for change in (wrong_total, float_amount, dropped_row):
            statuses = evaluate('termsmet_clean', mutate_result(self.clean, 'retrieve', change))
            self.assert_not_passed(statuses, 'retrieved_artifact')

    def test_wrong_version_does_not_pass(self):
        def v3(result):
            result['version'] = 3

        self.assert_not_passed(evaluate('termsmet_clean', mutate_result(self.clean, 'counter', v3)),
                               'agreement_funding')

        def accepted_v1(receipt):
            receipt['acceptances'][0]['version'] = 1

        self.assert_not_passed(evaluate('termsmet_clean', mutate_receipts(self.clean, accepted_v1)),
                               'retrieved_artifact')

    def test_early_release_or_refund_does_not_pass(self):
        def early(receipt):
            receipt['settlement_events'][1]['created_at'] = '2000-01-01T00:00:00.000000Z'

        def refund(receipt):
            receipt['settlement_events'].append(dict(receipt['settlement_events'][1], event_type='SIMULATED_REFUND'))

        for change in (early, refund):
            self.assert_not_passed(evaluate('termsmet_clean', mutate_receipts(self.clean, change)), 'release_gating')

    def test_clean_trace_is_not_a_lost_reply_pass(self):
        statuses = evaluate('termsmet_lost_reply', self.clean)
        self.assert_not_passed(statuses, 'lost_reply_recovery')

    def test_lost_reply_trace_is_not_a_clean_pass(self):
        statuses = evaluate('termsmet_clean', self.lost)
        for stage in BASE:
            self.assert_not_passed(statuses, stage)

    def test_lost_reply_without_real_drop_does_not_pass(self):
        events = copy.deepcopy(self.lost)
        (drop,) = [e for e in events if e['kind'] == 'message_dropped']
        drop['kind'] = 'message_delivered'
        drop['detail'] = {'to': 'tm_provider', 'kind': 'tm_repair_result'}
        self.assert_not_passed(evaluate('termsmet_lost_reply', events), 'lost_reply_recovery')
        events = [e for e in self.lost if e['kind'] != 'message_dropped']
        self.assert_not_passed(evaluate('termsmet_lost_reply', events), 'lost_reply_recovery')

    def test_dropped_message_must_be_the_real_pass(self):
        events = copy.deepcopy(self.lost)
        for e in events:
            if e['kind'] == 'message_sent' and e['detail'].get('kind') == 'tm_repair_result':
                e['detail']['body']['result']['verdict'] = 'FAIL'
        self.assert_not_passed(evaluate('termsmet_lost_reply', events), 'lost_reply_recovery')

    def test_provider_must_not_learn_pass_before_recovery(self):
        events = copy.deepcopy(self.lost)
        (drop,) = [e for e in events if e['kind'] == 'message_dropped']
        index = events.index(drop)
        leak = dict(drop, event_id='ev-leak', kind='message_delivered',
                    detail={'to': 'tm_provider', 'kind': 'tm_repair_result'})
        events.insert(index + 1, leak)
        self.assert_not_passed(evaluate('termsmet_lost_reply', events), 'lost_reply_recovery')

    def test_extra_retry_or_submission_does_not_pass(self):
        events = copy.deepcopy(self.lost)

        def accepted(result):
            result.clear()
            result.update({'job_id': 'x', 'submission_version': 3, 'verdict': 'PASS', 'release_allowed': False})

        events = mutate_result(events, 'repair_retry', accepted)
        self.assert_not_passed(evaluate('termsmet_lost_reply', events), 'lost_reply_recovery')

    def test_tampered_bundle_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            copy_dir = pathlib.Path(tmp) / 'clean'
            shutil.copytree(EVIDENCE / 'clean', copy_dir)
            path = copy_dir / 'events.jsonl'
            text = path.read_text(encoding='utf-8')
            self.assertIn('"total_cents":2500', text.replace(' ', ''))
            path.write_text(text.replace('"total_cents": 2500', '"total_cents": 2501')
                            .replace('"total_cents":2500', '"total_cents":2501'), encoding='utf-8')
            self.assertNotEqual(verify_bundle(str(copy_dir)), [])


if __name__ == '__main__':
    unittest.main()
