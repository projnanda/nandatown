"""FactoryBridge runs in this participant process, not inside the requester."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from uuid import uuid4
from http_mailbox import Mailbox
from worker import execute


def main():
    client=Mailbox(os.environ['TOWN_URL'],os.environ['RUN_ID'],'seller',os.environ['TOKEN'])
    state=Path(os.environ['STATE_DIR']);state.mkdir(parents=True,exist_ok=True)
    journal=state/'processed.json';cache=json.loads(journal.read_text()) if journal.exists() else {}
    live=os.environ.get('FACTORYBRIDGE_LIVE')=='1'
    deadline=time.monotonic()+float(os.environ.get('DEADLINE','180'))
    handled=0
    while time.monotonic()<deadline and handled<3:
        client.call('GET','/inbox/notify?wait=0.2');claim=client.claim()
        if claim is None:continue
        request=claim['body'];key=request.get('request_id')
        digest=hashlib.sha256(json.dumps(request,sort_keys=True,allow_nan=False).encode()).hexdigest()
        if claim['kind']!='manufacturing.request' or not isinstance(key,str) or not key:
            client.ack(claim,'rejected',{'reason':'Invalid manufacturing envelope'});continue
        if key in cache:
            if cache[key]['fingerprint']!=digest:raise RuntimeError('Changed body reused a logical request ID')
            payload={**deepcopy(cache[key]['payload']),'deduplicated':True}
        else:
            payload=execute(request,live=live)
            payload.update(deduplicated=False,execution_id=str(uuid4()) if payload.get('simulation_started') else None)
            cache[key]={'fingerprint':digest,'payload':payload,'executions':int(payload.get('simulation_started',False))}
            temporary=journal.with_suffix('.tmp');temporary.write_text(json.dumps(cache,allow_nan=False));temporary.replace(journal)
        client.send('response-'+claim['message_id'],payload,to='buyer')
        client.ack(claim,note={'cached_result':payload['deduplicated'],'execution_id':payload.get('execution_id')})
        handled+=1
    if handled!=3:raise RuntimeError('Participant deadline reached')
    (state/'report.json').write_text(json.dumps({'handled_messages':handled,'execution_counts':{k:v['executions'] for k,v in cache.items()},'http_exchanges':client.exchanges},indent=2)+'\n')


if __name__=='__main__':
    try:main()
    except Exception as exc:
        print(type(exc).__name__,file=sys.stderr);raise SystemExit(1)
