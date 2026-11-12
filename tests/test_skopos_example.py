"""Public bridge contract checks; no private prototype required."""
import importlib.util
from pathlib import Path
import httpx
import pytest

spec=importlib.util.spec_from_file_location('skopos_adapter',Path(__file__).parents[1]/'examples/skopos/adapter.py')
adapter=importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)

@pytest.mark.parametrize('url',['https://example.com','http://example.com','http://user:secret@localhost','http://localhost?q=x'])
def test_non_loopback_or_credential_url_rejected(url):
    assert not adapter.valid_url(url)

def test_loopback_url():
    assert adapter.valid_url('http://127.0.0.1:12345')

@pytest.mark.parametrize('code',[200,409,422])
def test_known_service_outcome(code):
    with httpx.Client(base_url='http://localhost',transport=httpx.MockTransport(lambda r:httpx.Response(code,json={'status':'COMPLETED' if code==200 else 'REJECTED'}))) as http:
        assert adapter.call_workflow(http,{})['http_status']==code

def test_transport_uncertainty_not_reported_as_rejection():
    def fail(request):raise httpx.ReadTimeout('timeout')
    with httpx.Client(base_url='http://localhost',transport=httpx.MockTransport(fail)) as http:
        assert adapter.call_workflow(http,{})['status']=='UNKNOWN'

def test_browser_failure_unknown():
    with httpx.Client(base_url='http://localhost',transport=httpx.MockTransport(lambda r:httpx.Response(500,json={'status':'UNKNOWN'}))) as http:
        assert adapter.call_workflow(http,{})['status']=='UNKNOWN'
