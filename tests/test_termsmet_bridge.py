"""Focused bridge checks against real SDK processes and a slow test server."""
import csv
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import sys
import tempfile
import threading
import time
import unittest
if importlib.util.find_spec("mcp") is None:
    raise unittest.SkipTest("Install examples/integrations/termsmet/requirements.lock to run TermsMet bridge tests")

from mcp.types import CallToolResult, TextContent

ROOT = pathlib.Path(__file__).resolve().parents[1]
PROJECT = pathlib.Path(os.environ.get('TERMSMET_PROJECT_DIR', ROOT.parent / 'termsmet-runtime')).resolve()
PYTHON = pathlib.Path(os.environ.get('TERMSMET_PROJECT_PYTHON', PROJECT / '.venv/bin/python')).absolute()
spec = importlib.util.spec_from_file_location('termsmet_bridge', ROOT / 'examples/integrations/termsmet/mcp_bridge.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
TermsMetBridge, BridgeError = module.TermsMetBridge, module.BridgeError
TOOLS = {'list_templates', 'propose_agreement', 'counter_agreement', 'accept_agreement', 'list_jobs', 'get_job', 'fund_job', 'submit_delivery', 'get_result', 'challenge_result', 'retrieve_delivery'}


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.bridges = []
        self.addCleanup(self.close_all)

    def bridge(self, project=PROJECT, project_python=PYTHON, **kwargs):
        bridge = TermsMetBridge(project, project_python, self.root / 'test.sqlite', **kwargs)
        self.bridges.append(bridge)
        return bridge

    def require_runtime(self):
        server = PROJECT / 'server/mcp_server.py'
        missing = []
        if not server.is_file():
            missing.append(f'TERMSMET_PROJECT_DIR: missing {server}')
        if not PYTHON.is_file() or not os.access(PYTHON, os.X_OK):
            missing.append(f'TERMSMET_PROJECT_PYTHON: missing or non-executable {PYTHON}')
        if missing:
            message = 'TermsMet runtime configuration invalid: ' + '; '.join(missing)
            if 'TERMSMET_PROJECT_DIR' in os.environ or 'TERMSMET_PROJECT_PYTHON' in os.environ:
                self.fail(message)
            self.skipTest(message)

    def close_all(self):
        for bridge in self.bridges:
            bridge.close()

    def test_real_sessions_shared_database_permissions_and_snapshots(self):
        self.require_runtime()
        bridge = self.bridge()
        inventory = bridge.start()
        self.assertEqual(set(inventory['buyer']), TOOLS)
        self.assertEqual(set(inventory['provider']), TOOLS)
        args = {'template_id': 'order_extraction_v1', 'source_csv': 'Order,Units,Price\nA1,1,$1.00\n'}
        proposed = bridge.call('buyer', 'propose_agreement', args, request_id='propose')
        args['source_csv'] = 'changed'
        job_id = proposed['job_id']
        jobs = bridge.call('provider', 'list_jobs', {}, request_id='list')
        self.assertIn(job_id, [item['job_id'] for item in jobs['jobs']])
        denied = bridge.call('provider', 'propose_agreement', {'template_id': 'order_extraction_v1', 'source_csv': 'x'}, request_id='denied')
        self.assertEqual(denied['error']['code'], 'PERMISSION_DENIED')
        proposed['job_id'] = 'changed'
        records = bridge.records
        self.assertNotEqual(records[0]['arguments']['source_csv'], 'changed')
        self.assertEqual(records[0]['result']['job_id'], job_id)
        records[0]['result']['job_id'] = 'changed again'
        self.assertEqual(bridge.records[0]['result']['job_id'], job_id)
        self.assertEqual([r['call_id'] for r in records], [1, 2, 3])
        self.assertTrue(all(r.get('requested_at') and r.get('returned_at') for r in records))
        bridge.close()
        bridge.close()
        with self.assertRaises(BridgeError):
            bridge.call('buyer', 'list_jobs', {}, request_id='closed')

    def test_real_bridge_service_boundary(self):
        self.require_runtime()
        fixtures = ROOT / 'examples/integrations/termsmet/fixtures'
        source = (fixtures / 'orders.csv').read_text(encoding='utf-8')
        initial = json.loads((fixtures / 'initial_delivery.json').read_text(encoding='utf-8'))
        self.assertTrue(source.endswith('\n'))
        bridge = self.bridge(review_seconds=2)
        bridge.start()
        sequence = 0

        def call(identity, tool, **arguments):
            nonlocal sequence
            sequence += 1
            return bridge.call(identity, tool, arguments, request_id=f'boundary-{sequence}')

        proposal = call('buyer', 'propose_agreement', template_id='order_extraction_v1', source_csv=source)
        job_id = proposal['job_id']
        self.assertEqual(proposal['version'], 1)
        self.assertEqual(proposal['source_hash'], hashlib.sha256(source.encode()).hexdigest())
        self.assertEqual(proposal['checker_version'], 'order_extraction_v1.0')
        self.assertEqual(call('buyer', 'retrieve_delivery', job_id=job_id)['error']['code'], 'PERMISSION_DENIED')
        self.assertEqual(call('provider', 'counter_agreement', job_id=job_id, repair_window_minutes=10)['version'], 2)
        self.assertEqual(call('buyer', 'accept_agreement', job_id=job_id, version=1)['error']['code'], 'VERSION_MISMATCH')
        call('provider', 'accept_agreement', job_id=job_id, version=2)
        accepted = call('buyer', 'accept_agreement', job_id=job_id, version=2)
        self.assertEqual(accepted['status'], 'ACCEPTED')
        self.assertEqual(set(accepted['accepted_by']), {'buyer', 'provider'})
        self.assertEqual(call('provider', 'counter_agreement', job_id=job_id, repair_window_minutes=5)['error']['code'], 'INVALID_STATE')
        funding = call('buyer', 'fund_job', job_id=job_id)
        self.assertEqual((funding['status'], funding['simulated']), ('FUNDED', True))
        first = call('provider', 'submit_delivery', job_id=job_id, delivery=initial)
        self.assertEqual((first['submission_version'], first['verdict'], first['release_allowed']), (1, 'FAIL', False))
        report = call('provider', 'get_result', job_id=job_id, submission_version=1)
        self.assertEqual(report['errors'], [{'code': 'MISSING_ORDER', 'order_id': 'A102'}])
        agreed = call('provider', 'get_job', job_id=job_id)
        self.assertEqual((agreed['version'], agreed['terms']['repair_window_minutes'], agreed['terms']['max_repairs']), (2, 10, 1))
        missing = {error['order_id'] for error in report['errors'] if error['code'] == 'MISSING_ORDER'}
        repaired = list(initial)
        for position, row in enumerate(csv.DictReader(io.StringIO(agreed['source_csv']))):
            if row['Order'] in missing:
                quantity = int(row['Units'])
                cents = int((Decimal(row['Price'].removeprefix('$')) * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
                repaired.insert(position, {'order_id': row['Order'], 'quantity': quantity,
                                           'unit_price_cents': cents, 'total_cents': quantity * cents})
        passing = call('provider', 'submit_delivery', job_id=job_id, delivery=repaired)
        self.assertEqual((passing['submission_version'], passing['verdict'], passing['release_allowed']), (2, 'PASS', False))
        duplicate = call('provider', 'submit_delivery', job_id=job_id, delivery=repaired)
        self.assertEqual(duplicate['error']['code'], 'INVALID_STATE')
        job = call('provider', 'get_job', job_id=job_id)
        self.assertIn(job['status'], ('IN_REVIEW', 'RELEASED'))
        review_deadline = datetime.fromisoformat(job['deadlines']['review'])
        confirmed = call('provider', 'get_result', job_id=job_id, submission_version=2)
        self.assertEqual((confirmed['submission_version'], confirmed['agreement_version'], confirmed['verdict']), (2, 2, 'PASS'))
        deadline = time.monotonic() + 15
        while True:
            remaining = deadline - time.monotonic()
            self.assertGreater(remaining, 0, 'release polling exceeded 15 seconds')
            bridge.call_timeout = min(10, remaining)
            final = call('buyer', 'get_result', job_id=job_id, submission_version=2)
            if final['release_allowed']:
                break
            self.assertEqual(final['settlement']['status'], 'NONE')
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        bridge.call_timeout = 10
        self.assertEqual(final['settlement'], {'status': 'RELEASED', 'simulated': True})
        receipt = final['receipt']
        self.assertEqual(receipt['status'], 'RELEASED')
        self.assertEqual([(a['version'], a['identity']) for a in receipt['acceptances']], [(2, 'buyer'), (2, 'provider')])
        self.assertEqual([item['submission_version'] for item in receipt['submissions']], [1, 2])
        self.assertEqual([item['verdict'] for item in receipt['check_results']], ['FAIL', 'PASS'])
        self.assertTrue(all(item['agreement_version'] == 2 for item in receipt['check_results']))
        self.assertTrue(all(item['source_hash'] == proposal['source_hash'] and item['checker_version'] == proposal['checker_version'] for item in receipt['check_results']))
        events = receipt['settlement_events']
        self.assertEqual([event['event_type'] for event in events], ['SIMULATED_FUNDING', 'SIMULATED_RELEASE'])
        self.assertTrue(all(event['simulated'] is True for event in events))
        self.assertGreaterEqual(datetime.fromisoformat(events[1]['created_at']), review_deadline)
        delivery = call('buyer', 'retrieve_delivery', job_id=job_id)['delivery']
        expected = json.loads((fixtures / 'expected_delivery.json').read_text(encoding='utf-8'))
        self.assertEqual(delivery, expected)
        self.assertTrue(all(type(row[key]) is int for row in delivery for key in ('quantity', 'unit_price_cents', 'total_cents')))
        self.assertEqual((len(delivery), sum(row['quantity'] for row in delivery), sum(row['total_cents'] for row in delivery)), (3, 6, 3560))
        self.assertEqual(len(call('buyer', 'list_jobs')['jobs']), 1)
        self.assertEqual(call('buyer', 'get_result', job_id=job_id, submission_version=2)['receipt']['settlement_events'], events)

    def test_protocol_errors_and_strict_response_consistency(self):
        make = lambda text, data, error=False: CallToolResult(
            content=[TextContent(type='text', text=text)], structuredContent=data, isError=error)
        self.assertEqual(module._business_result(make('{"n":1}', {'n': 1})), {'n': 1})
        for response in (make('{"n":1}', {'n': True}), make('{"n":1.0}', {'n': 1}),
                         make('not json', {'n': 1}), make('{}', {}, error=True)):
            with self.assertRaises(ValueError):
                module._business_result(response)

    def test_sdk_validation_error_is_transport_error(self):
        self.require_runtime()
        bridge = self.bridge()
        bridge.start()
        with self.assertRaises(BridgeError) as error:
            bridge.call('buyer', 'get_job', {'job_id': True}, request_id='bad-type')
        self.assertEqual(error.exception.category, 'transport_error')
        self.assertEqual(bridge.records[0]['arguments']['job_id'], True)
        with self.assertRaises(BridgeError):
            bridge.call('buyer', 'list_jobs', {}, request_id='closed')

    def fake_server(self, provider_delay=0):
        package = self.root / 'server'
        package.mkdir()
        (package / '__init__.py').write_text('')
        (package / 'mcp_server.py').write_text('''\
import asyncio, os, pathlib, time
from mcp.server import MCPServer
from mcp.server.mcpserver.tools import Tool
root = pathlib.Path(__file__).parent.parent
identity = os.environ['TERMSMET_IDENTITY']
(root / (identity + '.pid')).write_text(str(os.getpid()))
if identity == 'provider': time.sleep(PROVIDER_DELAY)
async def slow(delay: float, marker: str) -> dict:
    with (root / 'calls').open('a') as f: f.write(marker + "\\n")
    await asyncio.sleep(delay)
    return {'marker': marker}
MCPServer('fake', tools=[Tool.from_function(slow)]).run(transport='stdio')
'''.replace('PROVIDER_DELAY', repr(provider_delay)))
        return self.root

    def test_queue_deadline_prevents_late_execution(self):
        bridge = self.bridge(self.fake_server(), project_python=sys.executable, call_timeout=2, startup_timeout=10)
        bridge.start()
        results = []
        first = threading.Thread(target=lambda: results.append(bridge.call('buyer', 'slow', {'delay': 0.7, 'marker': 'first'}, request_id='first')))
        first.start()
        deadline = time.monotonic() + 3
        while not (self.root / 'calls').exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue((self.root / 'calls').exists())
        bridge.call_timeout = 0.1
        with self.assertRaises(BridgeError) as error:
            bridge.call('provider', 'slow', {'delay': 0.0, 'marker': 'second'}, request_id='second')
        self.assertEqual(error.exception.category, 'queue_timeout')
        first.join(3)
        self.assertFalse(first.is_alive())
        self.assertEqual(results, [{'marker': 'first'}])
        self.assertEqual((self.root / 'calls').read_text().splitlines(), ['first'])
        self.assertEqual(bridge.records[1]['transport_error'], 'queue_timeout')

    def test_queued_arguments_are_copied_before_dispatch(self):
        bridge = self.bridge(self.fake_server(), project_python=sys.executable, call_timeout=3, startup_timeout=10)
        bridge.start()
        first = threading.Thread(target=lambda: bridge.call('buyer', 'slow', {'delay': 0.5, 'marker': 'first'}, request_id='first'))
        first.start()
        deadline = time.monotonic() + 3
        while not (self.root / 'calls').exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue((self.root / 'calls').exists())
        args = {'delay': 0.0, 'marker': 'original'}
        output = []
        second = threading.Thread(target=lambda: output.append(bridge.call('provider', 'slow', args, request_id='second')))
        second.start()
        deadline = time.monotonic() + 2
        while len(bridge.records) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(len(bridge.records), 2)
        args['marker'] = 'changed'
        first.join(3)
        second.join(3)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(output, [{'marker': 'original'}])
        self.assertEqual(bridge.records[1]['arguments']['marker'], 'original')

    def test_started_timeout_closes_sessions_and_rejects_calls(self):
        bridge = self.bridge(self.fake_server(), project_python=sys.executable, call_timeout=0.25, startup_timeout=10)
        bridge.start()
        pids = [int((self.root / (identity + '.pid')).read_text()) for identity in ('buyer', 'provider')]
        with self.assertRaises(BridgeError) as error:
            bridge.call('buyer', 'slow', {'delay': 3.0, 'marker': 'slow'}, request_id='slow')
        self.assertEqual(error.exception.category, 'call_timeout_unknown')
        with self.assertRaises(BridgeError):
            bridge.call('buyer', 'slow', {'delay': 0.0, 'marker': 'retry'}, request_id='retry')
        bridge.close()
        for pid in pids:
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
        self.assertEqual((self.root / 'calls').read_text().splitlines(), ['slow'])

    def test_partial_start_timeout_cleans_up_both_processes(self):
        bridge = self.bridge(self.fake_server(provider_delay=8), project_python=sys.executable, startup_timeout=2)
        with self.assertRaises(BridgeError) as error:
            bridge.start()
        self.assertEqual(error.exception.category, 'startup_timeout')
        bridge.close()
        for identity in ('buyer', 'provider'):
            pid = int((self.root / (identity + '.pid')).read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)


if __name__ == '__main__':
    unittest.main()
