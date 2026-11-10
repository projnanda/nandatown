"""Test-only Lab plugin; never a TermsMet transaction or formal evidence."""
import importlib.util
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from nandatown.records import StageResult
from nandatown.sim.agents import SimAgent, role
from nandatown.sim.validators import validator

bridge = None
child = None


def close_termsmet():
    root = Path(__file__).parent / 'markers'
    root.mkdir(exist_ok=True)
    (root / 'cleanup').write_text('called')
    if bridge is not None:
        bridge.close()
    if child is not None:
        child.kill()
        child.wait(timeout=3)
    if (root / 'cleanup-error').exists():
        raise RuntimeError('controlled cleanup error')


@role('termsmet_smoke')
class Smoke(SimAgent):
    def on_start(self):
        global bridge, child
        root = Path(__file__).parent / 'markers'
        root.mkdir(exist_ok=True)
        assert os.environ['TERMSMET_REVIEW_SECONDS'] == '2'
        assert not Path(os.environ['TERMSMET_DB_PATH']).exists()
        assert os.environ['NANDATOWN_HOME'] != os.environ.get('HOME')
        assert 'TERMSMET_SMOKE_SECRET' not in os.environ
        (root / 'started').write_text('yes')
        behavior = self.config.get('behavior', 'pass')
        if behavior in ('connection-failure', 'bridge-smoke'):
            spec = importlib.util.spec_from_file_location('smoke_bridge', Path(__file__).with_name('mcp_bridge.py'))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            bridge = module.TermsMetBridge(os.environ['TERMSMET_PROJECT_DIR'], os.environ['TERMSMET_PROJECT_PYTHON'],
                                           os.environ['TERMSMET_DB_PATH'], startup_timeout=3)
            inventory = bridge.start()
            assert 'list_templates' in inventory['buyer'] and 'list_templates' in inventory['provider']
            assert bridge.call('buyer', 'list_templates', {}, request_id='smoke')['templates']
        if behavior == 'exception':
            raise RuntimeError('controlled failure')
        if behavior in ('hang', 'stubborn'):
            child = subprocess.Popen([sys.executable, '-c', 'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(120)'], start_new_session=True)
            (root / 'child.pid').write_text(str(child.pid))
            if behavior == 'stubborn':
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
            while True:
                time.sleep(.05)
        self.api.observe('smoke_complete', self.name, {})


def stages(spec, trace):
    names = ['agreement_funding', 'delivery_repair', 'release_gating', 'retrieved_artifact']
    if spec.validator == 'termsmet_lost_reply':
        names.append('lost_reply_recovery')
    return [StageResult(name=name, status='passed', evidence=[]) for name in names]


validator('termsmet_clean')(stages)
validator('termsmet_lost_reply')(stages)
