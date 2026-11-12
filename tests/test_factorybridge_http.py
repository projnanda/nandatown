"""No paid calls in pytest. The opt-in live command has separate evidence."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import pytest

HERE=Path(__file__).resolve().parents[1]/'examples/factorybridge'


def module(name):
    spec=importlib.util.spec_from_file_location(name,HERE/(name+'.py'))
    value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value);return value


def runtime():
    path=os.environ.get('FACTORYBRIDGE_RUNTIME_ROOT')
    if not path:pytest.skip('Set FACTORYBRIDGE_RUNTIME_ROOT to the pinned public runtime checkout')
    return path


def test_mailbox_rejects_remote_urls_before_credentials_are_sent():
    with pytest.raises(ValueError):module('http_mailbox').Mailbox('https://example.com','unused','seller','not-a-real-token')


def test_evidence_redaction_preserves_usage_but_removes_credentials():
    clean=module('run_http_check').public({'token':'private-value','nested':{'session':'private-value','fence':'private-value','api_key':'private-value'},'llm_usage':{'input_tokens':123,'output_tokens':45}})
    assert 'private-value' not in json.dumps(clean)
    assert clean['llm_usage']=={'input_tokens':123,'output_tokens':45}


def test_http_workflow_without_paid_model(tmp_path):
    out=tmp_path/'http'
    env=dict(os.environ);env['FACTORYBRIDGE_ALLOW_REMOTE']='0'
    result=subprocess.run([sys.executable,str(HERE/'run_http_check.py'),'--factory-root',runtime(),'--out',str(out),'--scripted'],env=env,capture_output=True,text=True,timeout=35)
    assert result.returncode==0,result.stdout+result.stderr
    report=json.loads((out/'results-http.json').read_text())
    assert report['result']=='PASS' and report['mode']=='scripted_smoke'
    assert report['requester']['success']['llm_usage']['llm_calls']==0
    assert report['worker']['execution_counts']=={'http-fulfillment':1,'http-invalid':0}
    assert report['requester']['duplicate']['same_execution_id']
    assert report['requester']['changed_envelope_http_status']==409
    assert report['auth_boundary']['missing_session_http_status']==401
    assert report['auth_boundary']['wrong_join_token_http_status']==403


def test_live_mode_never_silently_falls_back(tmp_path):
    env=dict(os.environ);env.pop('MODEL',None)
    result=subprocess.run([sys.executable,str(HERE/'run_http_check.py'),'--factory-root',runtime(),'--out',str(tmp_path/'no-live'),'--live-model'],env=env,capture_output=True,text=True,timeout=15)
    assert result.returncode==2
    assert 'Set MODEL explicitly' in result.stderr
    assert not (tmp_path/'no-live').exists()
