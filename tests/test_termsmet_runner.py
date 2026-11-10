"""Y4 runner smoke checks; no formal TermsMet scenario is implemented here."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / 'examples/integrations/termsmet/run.py'
SMOKE = ROOT / 'tests/termsmet_smoke_plugin.py'
spec = importlib.util.spec_from_file_location('termsmet_runner_test', RUNNER)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.integration = self.root / 'examples/integrations/termsmet'
        self.integration.mkdir(parents=True)
        self.project = self.root / 'project'
        self.project.mkdir()
        self.plugin = self.integration / 'termsmet_plugin.py'
        shutil.copy2(SMOKE, self.plugin)
        shutil.copy2(RUNNER, self.integration / 'run.py')
        shutil.copy2(ROOT / 'examples/integrations/termsmet/mcp_bridge.py', self.integration / 'mcp_bridge.py')
        for case in ('clean', 'lost-reply'):
            self.scenario(case)

    def scenario(self, case, behavior='pass'):
        path = self.integration / runner.SCENARIOS[case]
        path.write_text(f'''name: smoke-{case}
seed: 42
validator: {runner.VALIDATORS[case]}
agents:
  - name: smoke
    role: termsmet_smoke
    config: {{behavior: {behavior}}}
''')
        return path

    def live(self, case='clean', behavior='pass', timeout=20):
        return runner._run_live(self.project, sys.executable, case, self.root / case,
                                plugin=self.plugin, scenario=self.scenario(case, behavior),
                                cwd=self.root, timeout=timeout)

    def marker(self, name):
        return self.integration / 'markers' / name

    def test_smoke_bundles_verify_without_mcp_or_runtime(self):
        os.environ['TERMSMET_SMOKE_SECRET'] = 'do-not-inherit'
        self.addCleanup(os.environ.pop, 'TERMSMET_SMOKE_SECRET', None)
        evidence = self.root / 'evidence'
        evidence.mkdir()
        for case, folder in (('clean', 'clean'), ('lost-reply', 'lost_reply')):
            bundle = self.live(case)
            self.assertTrue((bundle / 'manifest.json').is_file())
            self.assertEqual(self.marker('cleanup').read_text(), 'called')
            self.marker('cleanup').unlink()
            self.marker('started').unlink()
            for name in ('profile.json', 'run.json', 'events.jsonl', 'intents.jsonl', 'result.json', 'manifest.json', 'attestation.json'):
                self.assertNotIn(str(self.root), (bundle / name).read_text())
            shutil.move(str(bundle), evidence / folder)
        shutil.rmtree(self.project)
        command = [sys.executable, str(self.integration / 'run.py'), '--verify', str(evidence)]
        result = subprocess.run(command, cwd=self.root, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Both bundles verified', result.stdout)
        with (evidence / 'clean' / 'events.jsonl').open('a') as f:
            f.write('{}\n')
        result = subprocess.run(command, cwd=self.root, capture_output=True, text=True, timeout=20)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('bundle verification failed', result.stderr)

    def test_missing_and_failed_stages_rejected(self):
        from nandatown.records import EvidenceResult, StageResult
        result = EvidenceResult(run_id='test', evaluator_version='lab-0.2.6', verdict='passed', evaluated_at=0,
                                stages=[StageResult(name=x, status='passed') for x in runner.BASE_STAGES - {'release_gating'}])
        with self.assertRaises(runner.RunnerError):
            runner._check_result(result, 'clean')
        result.stages.append(StageResult(name='release_gating', status='failed'))
        with self.assertRaises(runner.RunnerError):
            runner._check_result(result, 'clean')

    def test_missing_formal_files_and_nonempty_output(self):
        for missing in (self.plugin, self.integration / runner.SCENARIOS['clean']):
            with self.subTest(missing=missing.name):
                contents = missing.read_bytes()
                missing.unlink()
                try:
                    result = subprocess.run([sys.executable, str(self.integration / 'run.py'), '--project', str(self.project), '--case', 'clean', '--out', str(self.root / 'out')], capture_output=True, text=True, timeout=10)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(result.stderr.strip(), 'TermsMet runner: formal TermsMet plugin or scenario is missing')
                finally:
                    missing.write_bytes(contents)
        out = self.root / 'clean'
        out.mkdir()
        (out / 'existing').write_text('preserve')
        with self.assertRaisesRegex(runner.RunnerError, 'output directory must be empty'):
            self.live()
        self.assertEqual((out / 'existing').read_text(), 'preserve')

    def test_exception_and_cleanup_error_fail_after_hook(self):
        with self.assertRaises(runner.RunnerError):
            self.live(behavior='exception')
        self.assertTrue(self.marker('cleanup').exists())
        self.assertEqual(json.loads((self.root / 'clean/failure.json').read_text())['error'], 'run_failed')
        self.assertFalse((self.root / 'clean/manifest.json').exists())
        self.marker('cleanup').unlink()
        self.marker('cleanup-error').parent.mkdir(exist_ok=True)
        self.marker('cleanup-error').touch()
        with self.assertRaisesRegex(runner.RunnerError, 'cleanup_failed'):
            self.live(case='lost-reply')
        self.assertTrue(self.marker('cleanup').exists())
        self.assertFalse((self.root / 'lost-reply/manifest.json').exists())

    @unittest.skipUnless(importlib.util.find_spec('mcp') is not None, 'MCP SDK not installed')
    def test_real_bridge_connection_failure_runs_cleanup(self):
        with self.assertRaises(runner.RunnerError):
            self.live(behavior='connection-failure')
        self.assertTrue(self.marker('cleanup').exists())
        self.assertFalse((self.root / 'clean/manifest.json').exists())

    @unittest.skipUnless(importlib.util.find_spec('mcp') is not None, 'MCP SDK not installed')
    def test_real_bridge_smoke_in_runner(self):
        project = ROOT.parent / 'termsmet-runtime'
        python = project / '.venv/bin/python'
        if not (project / 'server/mcp_server.py').is_file() or not python.is_file():
            self.skipTest('pinned runtime unavailable')
        bundle = runner._run_live(project, python, 'clean', self.root / 'out', plugin=self.plugin,
                                  scenario=self.scenario('clean', 'bridge-smoke'), cwd=self.root, timeout=20)
        self.assertTrue((bundle / 'manifest.json').exists())
        self.assertTrue(self.marker('cleanup').exists())

    def test_missing_validator_runs_cleanup(self):
        text = self.plugin.read_text().replace("validator('termsmet_lost_reply')(stages)", '')
        self.plugin.write_text(text)
        with self.assertRaises(runner.RunnerError):
            self.live()
        self.assertTrue(self.marker('cleanup').exists())
        self.assertFalse((self.root / 'clean/manifest.json').exists())

    def test_missing_required_stage_fails_live_run(self):
        text = self.plugin.read_text().replace(", 'retrieved_artifact'", '')
        self.plugin.write_text(text)
        with self.assertRaises(runner.RunnerError):
            self.live()
        self.assertTrue(self.marker('cleanup').exists())
        self.assertFalse((self.root / 'clean/manifest.json').exists())
        self.assertTrue((self.root / 'clean/.pending').exists())

    def test_timeout_invokes_cleanup_and_reaps_detached_child(self):
        with self.assertRaisesRegex(runner.RunnerError, 'run timed out'):
            self.live(behavior='hang', timeout=2.5)
        self.assertTrue(self.marker('cleanup').exists())
        pid = int(self.marker('child.pid').read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.assertFalse((self.root / 'clean/manifest.json').exists())

    def test_inspection_failure_still_stops_worker_and_reports_cleanup_failure(self):
        real_observe = runner._observe
        child_seen = False
        def observe(pid, owned):
            nonlocal child_seen
            if child_seen:
                raise runner.RunnerError('cannot inspect trial-owned processes')
            table = real_observe(pid, owned)
            if self.marker('child.pid').exists():
                child = int(self.marker('child.pid').read_text())
                child_seen = child in owned
            return table
        with mock.patch.object(runner, '_observe', side_effect=observe):
            with self.assertRaisesRegex(runner.RunnerError, 'cleanup failed: cannot inspect'):
                self.live(behavior='hang', timeout=10)
        self.assertTrue(child_seen)
        self.assertTrue(self.marker('cleanup').exists())
        with self.assertRaises(ProcessLookupError):
            os.kill(int(self.marker('child.pid').read_text()), 0)

    def test_forced_termination_reports_cleanup_failure(self):
        prior = runner.GRACE_SECONDS
        runner.GRACE_SECONDS = 0.3
        self.addCleanup(setattr, runner, 'GRACE_SECONDS', prior)
        with self.assertRaisesRegex(runner.RunnerError, 'cleanup failed: forced termination required'):
            self.live(behavior='stubborn', timeout=2.5)
        pid = int(self.marker('child.pid').read_text())
        # Reparented processes can briefly remain zombies; they are no longer live.
        entry = runner._process_table().get(pid)
        self.assertTrue(entry is None or entry[2].startswith('Z'))
        self.assertFalse(self.marker('cleanup').exists())

    def test_interruption_invokes_cleanup(self):
        scenario = self.scenario('clean', 'hang')
        driver = '''import importlib.util,sys
s=importlib.util.spec_from_file_location('r',sys.argv[1]); r=importlib.util.module_from_spec(s); s.loader.exec_module(r)
try: r._run_live(sys.argv[2],sys.executable,'clean',sys.argv[3],plugin=sys.argv[4],scenario=sys.argv[5],cwd=sys.argv[6],timeout=30)
except r.RunnerError as e: print(e); sys.exit(1)
'''
        proc = subprocess.Popen([sys.executable, '-c', driver, str(RUNNER), str(self.project), str(self.root / 'out'), str(self.plugin), str(scenario), str(self.root)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 15
            while not self.marker('child.pid').exists() and time.monotonic() < deadline and proc.poll() is None:
                time.sleep(.05)
            self.assertTrue(self.marker('child.pid').exists())
            time.sleep(.2)
            proc.send_signal(signal.SIGINT)
            stdout, stderr = proc.communicate(timeout=15)
            self.assertNotEqual(proc.returncode, 0, stderr)
            self.assertIn('run interrupted', stdout)
            self.assertTrue(self.marker('cleanup').exists())
            with self.assertRaises(ProcessLookupError):
                os.kill(int(self.marker('child.pid').read_text()), 0)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


if __name__ == '__main__':
    unittest.main()
