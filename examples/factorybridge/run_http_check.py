"""Bounded real TCP/HTTP compatibility test, with separate buyer and worker."""
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
import urllib.error
import urllib.request
import uvicorn
from nandatown.coordinator import build_app
from nandatown.profiles import PROFILES

HERE=Path(__file__).resolve().parent
SENSITIVE={'token','session','join_tokens','authorization','api_key','admin_token','fence','x-town-session','x-town-admin'}


def public(value):
    if isinstance(value,dict):return {k:('[redacted ephemeral credential]' if k.lower() in SENSITIVE else public(v)) for k,v in value.items()}
    if isinstance(value,list):return [public(v) for v in value]
    return value


def revision(path):
    result=subprocess.run(['git','-C',str(path),'rev-parse','HEAD'],text=True,capture_output=True)
    return result.stdout.strip() if result.returncode==0 else 'source archive'


def verify_runtime(root):
    manifest=json.loads((root/'EXPORT_MANIFEST.json').read_text())
    if manifest['source_commit']!='55fd8d81d52cf554745b91e250e5fa74842fd49e':raise ValueError('Unexpected source runtime')
    for item in manifest['unchanged_source_files']:
        path=(root/item['path']).resolve()
        if not path.is_relative_to(root) or hashlib.sha256(path.read_bytes()).hexdigest()!=item['sha256']:
            raise ValueError('Runtime differs from its source manifest')
    return {'source_commit':manifest['source_commit'],'runtime_commit':revision(root),
            'manifest_sha256':hashlib.sha256((root/'EXPORT_MANIFEST.json').read_bytes()).hexdigest()}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--factory-root',type=Path,required=True);parser.add_argument('--out',type=Path,required=True)
    mode=parser.add_mutually_exclusive_group(required=True);mode.add_argument('--live-model',action='store_true');mode.add_argument('--scripted',action='store_true')
    args=parser.parse_args();factory=args.factory_root.resolve();provenance=verify_runtime(factory)
    if args.out.exists():parser.error('Choose a new output directory; never overwrite recorded evidence')
    if args.live_model and not os.environ.get('MODEL'):parser.error('Set MODEL explicitly; live mode never falls back to a scripted reviewer')
    args.out.mkdir(parents=True);report={'result':'ERROR','mode':'live_model' if args.live_model else 'scripted_smoke',
        'transport':'real loopback HTTP','independent_participant_processes':True,
        'town_base_commit':'6f9a7c064486c4a0ae9242f1ff341b8a4aad0244','adapter_commit':revision(HERE),
        'runtime':provenance,'python':sys.version.split()[0],'physical_execution':False}
    admin=secrets.token_urlsafe(32);server=None;thread=None;children=[];events=[];succeeded=False
    with tempfile.TemporaryDirectory(prefix='factorybridge-http-') as temporary:
        state=Path(temporary)
        sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        server=uvicorn.Server(uvicorn.Config(build_app(str(state/'town.db'),admin),log_level='critical',access_log=False))
        thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True);thread.start()
        base='http://127.0.0.1:'+str(port)
        def call(method,path,body=None,admin_access=True,expected=200):
            request=urllib.request.Request(base+path,data=None if body is None else json.dumps(body).encode(),method=method)
            request.add_header('Content-Type','application/json')
            if admin_access:request.add_header('X-Town-Admin',admin)
            try:
                with urllib.request.urlopen(request,timeout=10) as response:status=response.status;raw=response.read()
            except urllib.error.HTTPError as exc:status=exc.code;raw=exc.read()
            if status!=expected:raise RuntimeError('Unexpected coordinator HTTP status '+str(status))
            return json.loads(raw) if raw else None
        try:
            for _ in range(100):
                if server.started:break
                time.sleep(.05)
            if not server.started:raise RuntimeError('Coordinator did not start')
            profile=PROFILES['quote-clean'].model_dump()
            profile.update(name='factorybridge-http-fulfillment',lease_seconds=240,evaluator='factorybridge-http-v1',runtimes={'buyer':'factorybridge-http-requester','seller':'factorybridge-planner'})
            run=call('POST','/runs',{'profile':profile});run_id=run['run_id'];report['run_id']=run_id
            call('POST','/runs/'+run_id+'/join',{'name':'seller','token':'intentionally-wrong-fixture-token'},admin_access=False,expected=403)
            call('POST','/runs/'+run_id+'/messages',{'message_id':'unauthenticated','to':'seller','kind':'manufacturing.request','body':{}},admin_access=False,expected=401)
            report['auth_boundary']={'wrong_join_token_http_status':403,'missing_session_http_status':401}
            for role,script in [('seller','http_worker.py'),('buyer','http_requester.py')]:
                env=dict(os.environ);env['PYTHONPATH']=str(factory)
                if role=='buyer':
                    for key in list(env):
                        if key.startswith(('LLM_','OPENAI_','FACTORYBRIDGE_ALLOW_')):env.pop(key)
                    if args.live_model:env['MODEL']=os.environ['MODEL']
                env.update(TOWN_URL=base,RUN_ID=run_id,NAME=role,TOKEN=run['join_tokens'][role],
                           STATE_DIR=str(state/role),DEADLINE='180',FACTORYBRIDGE_LIVE='1' if args.live_model else '0')
                process=subprocess.Popen([sys.executable,str(HERE/script)],cwd=factory,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
                children.append((role,process))
            children[1][1].communicate(timeout=185)
            if children[1][1].returncode:raise RuntimeError('Requester failed; see received responses in evidence')
            children[0][1].communicate(timeout=10)
            if children[0][1].returncode:raise RuntimeError('FactoryBridge participant failed')
            buyer=json.loads((state/'buyer/report.json').read_text());seller=json.loads((state/'seller/report.json').read_text())
            assert seller['execution_counts']=={'http-fulfillment':1,'http-invalid':0},'Duplicate or invalid order re-executed'
            report.update(result='PASS',requester=buyer,worker=seller)
            succeeded=True
        except Exception as exc:
            report['failure']={'type':type(exc).__name__,'message':str(exc)}
            received=state/'buyer/received.json'
            if received.exists():report['received_before_failure']=json.loads(received.read_text())
        finally:
            for _,process in children:
                if process.poll() is None:
                    process.terminate()
                    try:process.wait(timeout=5)
                    except subprocess.TimeoutExpired:process.kill();process.wait()
            if report.get('run_id'):
                try:
                    call('POST','/runs/'+report['run_id']+'/finish');events=call('GET','/runs/'+report['run_id']+'/events')['events']
                except Exception as exc:report['evidence_export_error']=type(exc).__name__;succeeded=False;report['result']='ERROR'
            server.should_exit=True;thread.join(timeout=5);sock.close()
    for name,value in [('results-http.json',report),('town-events-http.json',events)]:
        (args.out/name).write_text(json.dumps(public(value),indent=2,allow_nan=False)+'\n')
    files=sorted(args.out.glob('*.json'));(args.out/'SHA256SUMS').write_text(''.join(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+p.name+'\n' for p in files))
    result=report.get('requester',{}).get('success',{})
    print(json.dumps({'result':report['result'],'mode':report['mode'],'llm_calls':result.get('llm_usage',{}).get('llm_calls'),
        'duplicate_executions':report.get('worker',{}).get('execution_counts',{}).get('http-fulfillment'),
        'evidence':str(args.out)},indent=2))
    return 0 if succeeded else 1


if __name__=='__main__':raise SystemExit(main())
