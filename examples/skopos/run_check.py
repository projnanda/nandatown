"""Whole existing SKOPOS UI workflow through real Town mailbox and browser."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import httpx
import uvicorn
from nandatown.client import TownClient
from nandatown.coordinator import build_app
from nandatown.records import TestProfile

HERE=Path(__file__).resolve().parent


def run(html,out,expected_source):
    html=html.resolve();out=out.resolve();out.parent.mkdir(parents=True,exist_ok=True)
    digest=hashlib.sha256(html.read_bytes()).hexdigest()
    assert digest==expected_source,'source version differs: review and pin separately before testing'
    checks={};observed={}
    with tempfile.TemporaryDirectory(prefix='skopos-town-') as directory:
        directory=Path(directory);os.environ['NANDATOWN_HOME']=str(directory/'town-home')
        token=secrets.token_urlsafe(32);admin_token=secrets.token_urlsafe(32)
        env={'PATH':os.environ.get('PATH',''),'HOME':os.environ['HOME'],'SKOPOS_HTML':str(html),'SKOPOS_SOURCE_SHA256':digest,'SKOPOS_TEST_TOKEN':token,'SKOPOS_READY_FILE':str(directory/'ready.json'),'SKOPOS_SCREENSHOT':str(out.with_suffix('.png')),'SKOPOS_BROWSER_ERRORS':str(directory/'errors.json')}
        service=subprocess.Popen(['node',str(HERE/'service.mjs')],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,text=True)
        bridge=None;server=None;thread=None;sock=None
        try:
            until=time.monotonic()+30
            while not (directory/'ready.json').exists() and time.monotonic()<until:
                if service.poll() is not None:raise RuntimeError('browser bridge startup failed: '+service.stderr.read()[-1000:])
                time.sleep(.1)
            assert (directory/'ready.json').exists(),'browser startup timed out'
            service_url=json.loads((directory/'ready.json').read_text())['url']
            sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen(128)
            town_url=f'http://127.0.0.1:{sock.getsockname()[1]}'
            server=uvicorn.Server(uvicorn.Config(build_app(str(directory/'town.db'),admin_token=admin_token),log_level='error',access_log=False))
            thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True);thread.start()
            until=time.monotonic()+10
            while not server.started and time.monotonic()<until:time.sleep(.05)
            assert server.started
            admin=httpx.Client(base_url=town_url,headers={'X-Town-Admin':admin_token},timeout=30)
            profile=TestProfile(name='skopos-conjunction-triage',task={'kind':'quote','sku':'synthetic-conjunction-triage','quantity':1,'unit_price_cents':0,'expected_total_cents':0},roles={'operator':'buyer','skopos':'seller'},capabilities={'operator':[],'skopos':['skopos.triage','skopos.explain','skopos.refresh']},fault='none',lease_seconds=30,evaluator='skopos-browser-workflow-v1')
            created=admin.post('/runs',json={'profile':profile.model_dump()});created.raise_for_status();created=created.json();run_id=created['run_id']
            buyer=TownClient(town_url,run_id);buyer.join('operator',created['join_tokens']['operator'])
            bridge_env={'PATH':os.environ.get('PATH',''),'HOME':str(directory),'NANDATOWN_HOME':str(directory/'bridge-home'),'TOWN_URL':town_url,'RUN_ID':run_id,'NAME':'skopos','TOKEN':created['join_tokens']['skopos'],'SKOPOS_URL':service_url,'SKOPOS_TEST_TOKEN':token}
            bridge=subprocess.Popen([sys.executable,str(HERE/'adapter.py')],env=bridge_env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,text=True)
            until=time.monotonic()+10
            while time.monotonic()<until:
                if any(p['name']=='skopos' and 'skopos.triage' in p['capabilities'] for p in buyer.participants()):break
                assert bridge.poll() is None,'Town bridge exited';time.sleep(.05)
            else:raise AssertionError('discovery failed')
            checks['town_capability_discovery']=True
            def exchange(mid,operation,revision=1,**extra):
                body={'request_id':mid,'operation':operation,'expected_revision':revision,**extra}
                buyer.send(mid,'skopos','skopos_workflow_request',body)
                until=time.monotonic()+20
                while time.monotonic()<until:
                    claim=buyer.claim()
                    if claim:
                        assert claim['from']=='skopos' and claim['kind']=='skopos_workflow_result' and claim['body']['town_request_id']==mid
                        buyer.ack(claim['message_id'],claim['fence'],'processed',{'correct':True})
                        observed[mid]=claim['body'];return claim['body'],body
                    assert bridge.poll() is None,'bridge exited';time.sleep(.05)
                raise AssertionError('response timeout')
            initial,_=exchange('initial','snapshot')
            assert initial['status']=='COMPLETED' and initial['counts']['total']==20 and len(initial['rows'])==20
            assert 'FABRICATED DATA' in initial['synthetic_banner'] and 'Operators decide every maneuver' in initial['human_boundary']
            checks['whole_existing_html_queue_rendered']=True
            sorted_view,_=exchange('sort','sort')
            assert sorted_view['sort_mode']=='skopos'
            bins=[r['bin'] for r in sorted_view['rows']];order={'Urgent operator review':0,'Resolve uncertainty':1,'Monitor':2}
            assert [order[b] for b in bins]==sorted(order[b] for b in bins)
            checks['actual_ui_sort_and_counts']=True
            detail,_=exchange('explain','select',warning_id='CDM-1047')
            assert 'Resolve uncertainty' in detail['detail'] and 'request a new track' in detail['detail']
            checks['real_warning_selection_and_explanation']=True
            deadline,_=exchange('deadline','select',warning_id='CDM-1051')
            assert 'Deadline flag' in deadline['detail'] and 'Escalate now' in deadline['detail']
            checks['uncertain_urgent_deadline_not_dismissed']=True
            updated,body=exchange('tracking-update','update_tracking')
            assert updated['revision']==2 and 'Urgent operator review' in updated['detail'] and 'SKOPOS does not decide' in updated['detail']
            old={r['id']:r for r in sorted_view['rows']};new={r['id']:r for r in updated['rows']}
            assert old['CDM-1047']['bin']=='Resolve uncertainty' and new['CDM-1047']['bin']=='Urgent operator review'
            assert all(old[k]==new[k] for k in old if k!='CDM-1047')
            checks['new_tracking_reclassifies_only_changed_warning']=True
            # The same logical bridge request delivered under a new Town message ID.
            buyer.send('replay-envelope','skopos','skopos_workflow_request',body)
            until=time.monotonic()+10;claim=None
            while not claim and time.monotonic()<until:claim=buyer.claim();time.sleep(.05)
            assert claim and claim['body']['revision']==2 and claim['body']['replayed'] is True
            buyer.ack(claim['message_id'],claim['fence'],'processed',{'correct':True});observed['replay']=claim['body']
            checks['replayed_update_does_not_apply_twice']=True
            stale,_=exchange('stale-update','update_tracking',revision=1)
            assert stale['status']=='REJECTED' and stale['http_status']==409 and stale['reason']=='stale_revision'
            checks['stale_agent_revision_refused']=True
            malformed,_=exchange('unknown-warning','select',revision=2,warning_id='CDM-9999')
            assert malformed['http_status']==422 and malformed['status']=='REJECTED'
            checks['unknown_warning_rejected_without_ui_mutation']=True
            forbidden,_=exchange('no-maneuver','execute_maneuver',revision=2)
            assert forbidden['status']=='REJECTED' and forbidden['http_status']==422
            checks['no_maneuver_command_surface']=True
            after,_=exchange('after-refusals','snapshot',revision=2)
            assert after['rows']==updated['rows'] and after['revision']==2
            checks['failure_cases_preserve_displayed_state']=True
            replay=buyer.send('tracking-update','skopos','skopos_workflow_request',body);assert replay['replay']
            conflict={**body,'operation':'reset'}
            try:buyer.send('tracking-update','skopos','skopos_workflow_request',conflict)
            except httpx.HTTPStatusError as exc:assert exc.response.status_code==409
            else:raise AssertionError('changed message identity accepted')
            checks['town_replay_and_conflict_boundary']=True
            reset,_=exchange('reset','reset',revision=2)
            assert reset['revision']==3 and reset['rows']==initial['rows'] and reset['counts']==initial['counts']
            checks['actual_ui_reset_restores_fixture']=True
            # Leave the useful sorted/update screen for the artifact screenshot.
            exchange('final-sort','sort',revision=3);exchange('final-update','update_tracking',revision=3)
            events=admin.get(f'/runs/{run_id}/events').json()['events']
            captured=httpx.post(service_url+'/evidence',headers={'Authorization':'Bearer '+token},timeout=15);captured.raise_for_status()
            buyer.http.close();admin.close()
        finally:
            if bridge is not None:
                bridge.terminate()
                try:bridge.communicate(timeout=10)
                except subprocess.TimeoutExpired:bridge.kill();bridge.communicate()
            service.terminate()
            try:service.communicate(timeout=15)
            except subprocess.TimeoutExpired:service.kill();service.communicate()
            if server:server.should_exit=True
            if thread:thread.join(10);assert not thread.is_alive()
            if sock:sock.close()
        errors=json.loads((directory/'errors.json').read_text());assert errors==[],errors
        checks['browser_no_javascript_errors']=True
    assert hashlib.sha256(html.read_bytes()).hexdigest()==digest,'source modified during test'
    report={'project':'SKOPOS','result':'PASS','checks':checks,'source_sha256':digest,'source_version':'HW7 repaired HTML; source-hash pin, not a Git commit','nandatown_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=HERE,text=True).strip(),'observed':observed,'town_events':events,'paid_model_calls':0,'external_operations':0,'limitations':['Synthetic warnings and deterministic scripted agent; no LLM or live orbital feed','No maneuver authorization or execution','Browser bridge is new integration code; prototype unchanged','Custom assertion report, not native Town evaluator or TOWN-TESTED badge','Private source artifact needed for full reproduction','No public index, cross-host identity, crash-recovery or independent operator reproduction']}
    report['integration_sha256']={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in [HERE/'service.mjs',HERE/'adapter.py',HERE/'run_check.py']}
    out.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({k:report[k] for k in ['project','result','checks','source_sha256','nandatown_commit']},indent=2))
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--skopos-html',type=Path,required=True);parser.add_argument('--source-sha256',required=True);parser.add_argument('--out',type=Path,default=Path('/tmp/skopos-town-result.json'));args=parser.parse_args();run(args.skopos_html,args.out,args.source_sha256)
