"""Verify bridge tests remain optional and independent of neighboring checkouts."""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
FAKE_TESTS = (
    'test_partial_start_timeout_cleans_up_both_processes',
    'test_protocol_errors_and_strict_response_consistency',
    'test_queue_deadline_prevents_late_execution',
    'test_queued_arguments_are_copied_before_dispatch',
    'test_started_timeout_closes_sessions_and_rejects_calls',
)
REAL_TESTS = (
    'test_real_bridge_service_boundary',
    'test_real_sessions_shared_database_permissions_and_snapshots',
    'test_sdk_validation_error_is_transport_error',
)


class BridgePortabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        checkout = Path(self.temp.name) / 'town'
        (checkout / 'tests').mkdir(parents=True)
        (checkout / 'examples/integrations/termsmet').mkdir(parents=True)
        for path in ('tests/test_termsmet_bridge.py', 'examples/integrations/termsmet/mcp_bridge.py'):
            shutil.copy2(ROOT / path, checkout / path)
        self.checkout = checkout

    def run_tests(self, *flags, explicit=False):
        env = os.environ.copy()
        env.pop('TERMSMET_PROJECT_DIR', None)
        env.pop('TERMSMET_PROJECT_PYTHON', None)
        if explicit:
            env['TERMSMET_PROJECT_DIR'] = str(self.checkout / 'missing-project')
            env['TERMSMET_PROJECT_PYTHON'] = str(self.checkout / 'missing-python')
        return subprocess.run(
            [sys.executable, *flags, '-m', 'unittest', 'discover', '-s', 'tests',
             '-p', 'test_termsmet_bridge.py', '-v'],
            cwd=self.checkout, env=env, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, timeout=90, check=False)

    def test_absent_sdk_skips_during_discovery(self):
        result = self.run_tests('-I', '-S')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('Install examples/integrations/termsmet/requirements.lock', result.stdout)
        self.assertIn('skipped=1', result.stdout)

    @unittest.skipUnless(importlib.util.find_spec('mcp') is not None, 'MCP SDK not installed')
    def test_sdk_without_runtime_skips_only_real_tests(self):
        result = self.run_tests()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('Ran 8 tests', result.stdout)
        self.assertIn('skipped=3', result.stdout)
        for name in FAKE_TESTS:
            self.assertRegex(result.stdout, rf'{name} .* \.\.\. ok')
        for name in REAL_TESTS:
            self.assertRegex(result.stdout, rf'{name} .* \.\.\. skipped')

    @unittest.skipUnless(importlib.util.find_spec('mcp') is not None, 'MCP SDK not installed')
    def test_explicit_invalid_runtime_fails_without_breaking_fake_tests(self):
        result = self.run_tests(explicit=True)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn('TermsMet runtime configuration invalid:', result.stdout)
        self.assertIn('TERMSMET_PROJECT_DIR: missing', result.stdout)
        self.assertIn('TERMSMET_PROJECT_PYTHON: missing or non-executable', result.stdout)
        self.assertIn('failures=3', result.stdout)
        for name in FAKE_TESTS:
            self.assertRegex(result.stdout, rf'{name} .* \.\.\. ok')
        for name in REAL_TESTS:
            self.assertRegex(result.stdout, rf'{name} .* \.\.\. FAIL')


if __name__ == '__main__':
    unittest.main()
