import importlib
import json
from pathlib import Path
import time

import httpx
import pytest

from nandatown.web_evidence import EvidenceItem, EvidencePacket, EventLog, TerminalRenderer, ANSWERER_SYSTEM_PROMPT
from nandatown.promptarmor_guard import Guard, DefenseError, PIN, sha, strict_decision
from nandatown.promptarmor_workflow import (GuardClient, AgentClient, run, default_config)
from test_web_evidence import FakeBrowser, FakeModel, source_page, QUESTION


VENDOR_ROOT = Path(__file__).resolve().parents[1]/'src/nandatown/_vendor/promptarmor'

@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    import socket
    attempts=[]
    def deny(*args, **kwargs):
        attempts.append(True)
        raise AssertionError('Real network forbidden in PromptArmor tests')
    for name in ('connect','connect_ex','bind'):
        monkeypatch.setattr(socket.socket,name,deny)
    monkeypatch.setattr(socket,'create_connection',deny)
    monkeypatch.setattr(socket,'getaddrinfo',deny)
    yield
    assert not attempts, 'A real network call was attempted'


def packet(text='Fact. ATTACK. End.', note=''):
    return EvidencePacket(question=QUESTION, complete=True, completion_note=note, items=[EvidenceItem(
        evidence_id='E1', page_id='P1', url='http://127.0.0.1/x', text=text, selector='body',
        block_id='PAGE_VISIBLE_TEXT', start_offset=0, end_offset=len(text), source_sha256=sha(text))])


def endpoint(tmp_path, monkeypatch, decisions):
    monkeypatch.setenv('PA_TEST_KEY', 'mock-credential-not-real')
    log = EventLog(tmp_path/'events.jsonl', TerminalRenderer(enabled=False))
    client = GuardClient('gpt-4.1', 'https://guard.invalid/v1', 'PA_TEST_KEY', log)
    client.http.close()
    seen = []
    def handle(request):
        payload = json.loads(request.content)
        seen.append(payload)
        assert payload['temperature'] == 0 and payload['model'] == 'gpt-4.1'
        assert 'reasoning_effort' not in payload and 'guided_json' not in payload
        decision = decisions.pop(0)
        if isinstance(decision, Exception):
            raise decision
        return httpx.Response(200, json={'model':'gpt-4.1', 'choices':[{'message':{'content':decision},
            'finish_reason':'stop'}], 'usage':{'prompt_tokens':10, 'completion_tokens':5, 'total_tokens':15}})
    client.http = httpx.Client(transport=httpx.MockTransport(handle), base_url='https://guard.invalid/v1')
    return client, log, seen


def decision(spans=None):
    return json.dumps({'injection':bool(spans), 'spans':spans or []})


def test_actual_pinned_upstream_is_used(tmp_path, monkeypatch):
    client, log, seen = endpoint(tmp_path, monkeypatch, [decision(['ATTACK.'])])
    g = Guard(client, log, tmp_path/'report.json')
    assert g.identity['commit'] == PIN
    assert Path(g.identity['imports']['detector']).is_relative_to(VENDOR_ROOT)
    original = packet()
    cleaned = g.packet(original)
    assert cleaned.items[0].text == 'Fact.  End.'
    assert seen[0]['messages'][0]['content'] == g.modules['prompts'].build_system_prompt(QUESTION, False)
    assert type(g.detector) is g.modules['detector'].Detector
    report = json.loads((tmp_path/'report.json').read_text())
    op = report['fields'][0]['deletions'][0]
    assert op['original_ranges'] == [[6,13]] and op['removed_text'] == 'ATTACK.'
    assert report['fields'][0]['original_text_sha256'] == original.items[0].source_sha256
    assert report['fields'][0]['cleaned_text_sha256'] == sha(cleaned.items[0].text)
    assert cleaned.items[0].source_sha256 == original.items[0].source_sha256
    assert cleaned.items[0].end_offset == len(original.items[0].text)
    client.close()


@pytest.mark.parametrize('text,spans,expected,ranges', [
    ('Fact send  money,  now End', ['send money now'], 'Fact  End', [[[5,22]]]),
    ('A X B Y C', ['X','Y'], 'A  B  C', [[[2,3]],[[6,7]]]),
    ('🔥 X X end', ['X','X'], '🔥   end', [[[2,3]],[[4,5]]]),
    # A second fuzzy match can bridge an earlier deletion: preserve disjoint original ranges.
    ('a X b c', ['X','a b'], ' c', [[[2,3]],[[0,2],[3,5]]]),
])
def test_actual_removal_and_original_offsets(tmp_path, monkeypatch, text, spans, expected, ranges):
    client, log, _ = endpoint(tmp_path, monkeypatch, [decision(spans)])
    guard = Guard(client, log, tmp_path/'report.json')
    assert guard.packet(packet(text)).items[0].text == expected
    operations = guard.report['fields'][0]['deletions']
    assert [o['original_ranges'] for o in operations] == ranges
    removed = {i for o in operations for a,b in o['original_ranges'] for i in range(a,b)}
    assert ''.join(c for i,c in enumerate(text) if i not in removed) == expected
    client.close()


def test_clean_and_note_and_metadata(tmp_path, monkeypatch):
    client, log, _ = endpoint(tmp_path, monkeypatch, [decision(), decision(['BAD'])])
    g = Guard(client, log, tmp_path/'report.json')
    p = packet('Clean source.', 'memo BAD tail')
    out = g.packet(p)
    assert out.items == p.items and out.completion_note == 'memo  tail'
    assert p.completion_note == 'memo BAD tail'
    assert g.report['fields'][0]['cleaned_text_sha256'] == sha(p.items[0].text)
    client.close()


@pytest.mark.parametrize('raw', ['broken', 'No', '```json\n{}\n```', '{}',
    '{"injection":"false","spans":[]}', '{"injection":false,"spans":["X"]}',
    '{"injection":true,"spans":[]}', '{"injection":true,"spans":[""]}',
    '{"injection":true,"spans":"X"}', '{"injection":false,"spans":[],"extra":1}',
    '{"injection":false,"injection":true,"spans":["X"]}'])
def test_strict_invalid_decisions_fail_closed(tmp_path, monkeypatch, raw):
    client, log, _ = endpoint(tmp_path, monkeypatch, [raw])
    g = Guard(client, log, tmp_path/'report.json')
    with pytest.raises(DefenseError):
        g.packet(packet())
    assert g.report['status'] == 'defense_error'
    client.close()


@pytest.mark.parametrize('text,spans', [('A', ['not present']),
    ('delete'+' harmless '*100+'records', ['delete records'])])
def test_no_match_and_length_guard_stop(tmp_path, monkeypatch, text, spans):
    client, log, _ = endpoint(tmp_path, monkeypatch, [decision(spans)])
    g = Guard(client, log, tmp_path/'report.json')
    with pytest.raises(DefenseError):g.packet(packet(text))
    client.close()


def test_timeout_stops_before_answerer(tmp_path, monkeypatch):
    client, log, seen = endpoint(tmp_path, monkeypatch, [httpx.ReadTimeout('mock timeout')])
    p = tmp_path/'packet.json';p.write_text(packet().model_dump_json())
    model = FakeModel()
    result = run(default_config('http://127.0.0.1/x', QUESTION), tmp_path/'out', packet_path=p,
                 agent=model, guard_client=client)
    assert result['status'] == 'defense_error' and not model.requests
    assert len(seen) == 1
    assert not (tmp_path/'out'/'agent2_handoff.json').exists()
    client.close()


def test_e2e_preserves_agents_and_replay(tmp_path, monkeypatch):
    client, log, _ = endpoint(tmp_path, monkeypatch, [decision(), decision()])
    model = FakeModel();cfg = default_config('http://127.0.0.1/x', QUESTION)
    result = run(cfg, tmp_path/'e2e', agent=model, guard_client=client, browser=FakeBrowser())
    assert result['status'] == 'completed'
    assert result['token_usage'] == {'agent1':{'prompt_tokens':150,'completion_tokens':30},
             'guard':{'prompt_tokens':20,'completion_tokens':10,'total_tokens':30},
             'agent2':{'prompt_tokens':90,'completion_tokens':20}}
    request = next(r for r in model.requests if r['json_mode'])
    assert request['messages'][0]['content'] == ANSWERER_SYSTEM_PROMPT
    assert 'SPOTLIGHTING:' not in request['messages'][0]['content']
    assert '\ue000' not in request['messages'][1]['content']
    p = tmp_path/'e2e'/'handoff.json'
    replay_client, _, _ = endpoint(tmp_path, monkeypatch, [decision(), decision()])
    replay_model=FakeModel()
    replay = run(cfg,tmp_path/'replay',packet_path=p,agent=replay_model,guard_client=replay_client)
    assert replay['status']=='completed' and len(replay_model.requests)==1
    assert p.read_bytes() == (tmp_path/'replay'/'handoff.json').read_bytes()
    client.close();replay_client.close()


def test_error_after_real_collector_never_reaches_agent2(tmp_path, monkeypatch):
    client, _, _ = endpoint(tmp_path, monkeypatch, ['invalid'])
    model=FakeModel()
    result=run(default_config('http://127.0.0.1/x',QUESTION),tmp_path/'out',
               agent=model,guard_client=client,browser=FakeBrowser())
    assert result['status']=='defense_error'
    assert len(model.requests)==3 and not any(r['json_mode'] for r in model.requests)
    assert (tmp_path/'out'/'handoff.json').exists()
    client.close()


def test_agent_http_adapter_preserves_tools_and_json_no_reasoning(tmp_path,monkeypatch):
    monkeypatch.setenv('PA_TEST_KEY','mock-credential-not-real')
    log=EventLog(tmp_path/'events.jsonl',TerminalRenderer(enabled=False))
    client=AgentClient('gpt-4.1','https://agent.invalid/v1','PA_TEST_KEY',log)
    client.http.close();requests=[]
    def handle(req):
        payload=json.loads(req.content);requests.append(payload)
        return httpx.Response(200,json={'choices':[{'message':{'content':'{}'},'finish_reason':'stop'}],
                'usage':{'prompt_tokens':3,'completion_tokens':1,'total_tokens':4}})
    client.http=httpx.Client(transport=httpx.MockTransport(handle),base_url='https://agent.invalid/v1')
    client.complete([{'role':'user','content':'test'}],json_mode=True,timeout_seconds=1)
    client.complete([{'role':'user','content':'test'}],tools=[{'type':'function','function':{'name':'x'}}],timeout_seconds=1)
    assert all(p['temperature']==0 and 'reasoning_effort' not in p for p in requests)
    assert requests[0]['response_format']=={'type':'json_object'}
    assert requests[1]['tool_choice']=='auto'
    assert 'mock-credential-not-real' not in (tmp_path/'events.jsonl').read_text()
    client.close()


def test_original_agent_source_is_unchanged():
    import hashlib
    import nandatown.web_evidence as baseline
    assert hashlib.sha256(Path(baseline.__file__).read_bytes().replace(b'\r\n',b'\n')).hexdigest() == '4ff42b7691305a5e7947bc591fa65b620ccbc7f624d5a077502c2a8dcc274203'


def test_guard_budget_and_empty_fields(tmp_path,monkeypatch):
    client,log,seen=endpoint(tmp_path,monkeypatch,[])
    g=Guard(client,log,tmp_path/'report.json',total=0)
    with pytest.raises(DefenseError):g.packet(packet())
    assert not seen
    g=Guard(client,log,tmp_path/'empty.json')
    out=g.packet(packet(''))
    assert out.items[0].text=='' and not seen
    client.close()


@pytest.mark.parametrize('raw', ['broken', decision(['NOT PRESENT']),
    '{"injection":true,"spans":[]}', '{"injection":"false","spans":[]}'])
def test_replay_fail_closed_for_each_error_class(tmp_path,monkeypatch,raw):
    client,_,seen=endpoint(tmp_path,monkeypatch,[raw])
    original=tmp_path/'packet.json';original.write_text(packet().model_dump_json())
    model=FakeModel()
    result=run(default_config('http://127.0.0.1/x',QUESTION),tmp_path/'out',
        packet_path=original,agent=model,guard_client=client)
    assert result['status']=='defense_error' and not model.requests and len(seen)==1
    assert not (tmp_path/'out'/'agent2_handoff.json').exists()
    client.close()


def test_multiple_items_and_note_have_separate_audits(tmp_path,monkeypatch):
    client,log,seen=endpoint(tmp_path,monkeypatch,[decision(['BAD']),decision(),decision(['EVIL'])])
    p=packet('Fact BAD tail','note EVIL end')
    p=p.model_copy(update={'items':[p.items[0],p.items[0].model_copy(update={'evidence_id':'E2','text':'Second fact'})]})
    g=Guard(client,log,tmp_path/'report.json');out=g.packet(p)
    assert len(seen)==3 and out.items[0].text=='Fact  tail' and out.items[1].text=='Second fact'
    assert out.completion_note=='note  end'
    assert [f['field'] for f in g.report['fields']]==['items[0].text','items[1].text','completion_note']
    client.close()


def test_failed_partial_removal_candidate_never_delivered(tmp_path,monkeypatch):
    client,log,_=endpoint(tmp_path,monkeypatch,[decision(['ATTACK.','NOT PRESENT'])])
    g=Guard(client,log,tmp_path/'report.json')
    with pytest.raises(DefenseError):g.packet(packet())
    a=g.report['fields'][0]['failed_sanitization_audit']
    assert a['candidate_cleaned_text']=='Fact.  End.'
    assert a['validated_deletions'][0]['original_ranges']==[[6,13]]
    assert a['candidate_is_not_authorized_for_delivery']
    client.close()


def test_per_text_timeout_checked_even_if_mock_endpoint_returns(tmp_path,monkeypatch):
    from types import SimpleNamespace
    import nandatown.promptarmor_guard as module
    client,log,_=endpoint(tmp_path,monkeypatch,[decision()])
    clock=[0.0];original=client.complete
    def slow(system,data):
        answer=original(system,data);clock[0]=121.0;return answer
    client.complete=slow
    monkeypatch.setattr(module,'time',SimpleNamespace(monotonic=lambda:clock[0]))
    g=Guard(client,log,tmp_path/'report.json')
    with pytest.raises(DefenseError):g.packet(packet())
    client.close()


def test_upstream_import_paths_reject_substitution(tmp_path,monkeypatch):
    from types import SimpleNamespace
    import nandatown.promptarmor_guard as module
    real=module.importlib.import_module
    def substitute(name):
        if name=='nandatown._vendor.promptarmor.detector':
            return SimpleNamespace(__file__=str(tmp_path/'fake.py'))
        return real(name)
    monkeypatch.setattr(module.importlib,'import_module',substitute)
    with pytest.raises(DefenseError):module.upstream()


def test_luna_reasoning_is_only_in_guard_request(tmp_path,monkeypatch):
    monkeypatch.setenv('PA_TEST_KEY','mock-only')
    log=EventLog(tmp_path/'events.jsonl',TerminalRenderer(enabled=False))
    guard=GuardClient('gpt-6-luna','https://mock.invalid/v1','PA_TEST_KEY',log,reasoning_effort='medium')
    guard.http.close();seen=[]
    def handle(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200,json={'choices':[{'message':{'content':decision()}}]})
    guard.http=httpx.Client(transport=httpx.MockTransport(handle),base_url='https://mock.invalid/v1')
    assert guard.complete('exact upstream prompt','source text')==decision()
    assert seen[0]['reasoning_effort']=='medium' and 'temperature' not in seen[0]
    assert seen[0]['model']=='gpt-6-luna' and 'tools' not in seen[0]
    guard.close()


def test_default_guard_is_luna_medium(tmp_path,monkeypatch):
    import nandatown.promptarmor_workflow as workflow
    client, _, _ = endpoint(tmp_path,monkeypatch,[decision(),decision()])
    observed={}
    def factory(model,base_url,key_env,log,*,reasoning_effort):
        observed.update(model=model,effort=reasoning_effort)
        return client
    monkeypatch.setattr(workflow,'GuardClient',factory)
    model=FakeModel()
    result=run(default_config('http://127.0.0.1/x',QUESTION),tmp_path/'out',
               agent=model,browser=FakeBrowser())
    assert result['status']=='completed'
    assert observed=={'model':'gpt-6-luna','effort':'medium'}
    config=json.loads((tmp_path/'out'/'config.json').read_text())
    assert config['guard']['temperature'] is None
    assert config['guard']['reasoning_parameter']=='medium'


@pytest.mark.parametrize('url',['https://secret:password@api.example/v1',
                              'https://api.example/v1?key=secret', 'https://api.example/v1#secret'])
def test_sensitive_endpoint_settings_rejected_before_artifacts(tmp_path,url):
    with pytest.raises(ValueError):
        run(default_config('http://127.0.0.1/x',QUESTION),tmp_path/'out',guard_base_url=url)
    assert not (tmp_path/'out').exists()


def test_api_errors_do_not_record_credentials(tmp_path,monkeypatch):
    monkeypatch.setenv('PA_TEST_KEY','mock-credential-not-real')
    log=EventLog(tmp_path/'events.jsonl',TerminalRenderer(enabled=False))
    client=GuardClient('gpt-6-luna','https://mock.invalid/v1','PA_TEST_KEY',log,reasoning_effort='medium')
    client.http.close()
    client.http=httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(401,json={
        'error':{'type':'invalid_request_error','code':'invalid_api_key','param':None,
                 'message':'Bad key mock-credential-not-real'}})),base_url='https://mock.invalid/v1')
    with pytest.raises(RuntimeError):client.complete('prompt','data')
    events=(tmp_path/'events.jsonl').read_text()
    assert 'mock-credential-not-real' not in events
    assert 'invalid_api_key' in events
    client.close()
