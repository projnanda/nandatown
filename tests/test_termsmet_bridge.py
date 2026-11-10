"""Focused bridge checks against real SDK processes and a slow test server."""
import importlib.util
import os
import pathlib
import tempfile
import threading
import time
import unittest
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

    def bridge(self, project=PROJECT, **kwargs):
        bridge = TermsMetBridge(project, PYTHON, self.root / 'test.sqlite', **kwargs)
        self.bridges.append(bridge)
        return bridge

    def close_all(self):
        for bridge in self.bridges:
            bridge.close()

    def test_real_sessions_shared_database_permissions_and_snapshots(self):
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

    def test_protocol_errors_and_strict_response_consistency(self):
        make = lambda text, data, error=False: CallToolResult(
            content=[TextContent(type='text', text=text)], structuredContent=data, isError=error)
        self.assertEqual(module._business_result(make('{"n":1}', {'n': 1})), {'n': 1})
        for response in (make('{"n":1}', {'n': True}), make('{"n":1.0}', {'n': 1}),
                         make('not json', {'n': 1}), make('{}', {}, error=True)):
            with self.assertRaises(ValueError):
                module._business_result(response)

    def test_sdk_validation_error_is_transport_error(self):
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
        bridge = self.bridge(self.fake_server(), call_timeout=2, startup_timeout=10)
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
        bridge = self.bridge(self.fake_server(), call_timeout=3, startup_timeout=10)
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
        bridge = self.bridge(self.fake_server(), call_timeout=0.25, startup_timeout=10)
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
        bridge = self.bridge(self.fake_server(provider_delay=8), startup_timeout=2)
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
