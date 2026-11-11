"""Independent buyer: submit, inspect outcome, retry and test invalid input."""
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import time
from http_mailbox import Mailbox
from fixture import ORDER


def main():
    client=Mailbox(os.environ['TOWN_URL'],os.environ['RUN_ID'],'buyer',os.environ['TOKEN'])
    deadline=time.monotonic()+float(os.environ.get('DEADLINE','180'))
    directory=client.call('GET','/participants')
    assert any(p['name']=='seller' for p in directory),'FactoryBridge participant missing'
    state=Path(os.environ['STATE_DIR']);state.mkdir(parents=True,exist_ok=True);received=[]
    def reply(mid,request):
        client.send(mid,request)
        while time.monotonic()<deadline:
            client.call('GET','/inbox/notify?wait=0.2');claim=client.claim()
            if claim is None:continue
            assert claim['message_id']=='response-'+mid and claim['kind']=='manufacturing.response','Response correlation mismatch'
            value=claim['body'];received.append(value);(state/'received.json').write_text(json.dumps(received,allow_nan=False));assert value['request_id']==request['request_id']
            client.ack(claim)
            return value
        raise RuntimeError('Response deadline reached')
    request={'request_id':'http-fulfillment','order':deepcopy(ORDER)}
    success=reply('http-success',request)
    assert success['status']=='completed' and success['verdict']['success']
    assert success['truth']['final_delivery_day']<=ORDER['deadline_day']
    assert success['truth']['total_cost']<=ORDER['budget_usd']
    assert any(e['status']=='delayed' for e in success['truth']['events'])
    assert any(e['status']=='cancelled' for e in success['truth']['events'])
    live=os.environ.get('FACTORYBRIDGE_LIVE')=='1'
    assert success['scripted_reviewer'] is (not live)
    if live:
        assert success['llm_usage']['llm_calls']>0 and success['llm_usage']['accounting_complete']
        assert success['llm_usage']['input_tokens']>0 and success['model']==os.environ['MODEL']
    replay=client.send('http-success',request);assert replay['replay'] is True
    changed=deepcopy(request);changed['order']['budget_usd']=99
    conflict,_=client.call('POST','/messages',{'message_id':'http-success','to':'seller','kind':'manufacturing.request','body':changed},expected=(409,))
    duplicate=reply('http-redelivery',request)
    assert duplicate['deduplicated'] and duplicate['execution_id']==success['execution_id']
    invalid=deepcopy(ORDER);invalid['parts'][0]['qty']=0
    rejected=reply('http-invalid',{'request_id':'http-invalid','order':invalid})
    assert rejected['status']=='rejected' and rejected['simulation_started'] is False
    assert rejected['llm_usage']['llm_calls']==0
    report={'success':success,'duplicate':{'deduplicated':True,'same_execution_id':True},
        'invalid_quantity':rejected,'same_message_replayed':replay['replay'],
        'changed_envelope_http_status':conflict,'http_exchanges':client.exchanges}
    Path(os.environ['STATE_DIR']).mkdir(parents=True,exist_ok=True)
    (Path(os.environ['STATE_DIR'])/'report.json').write_text(json.dumps(report,indent=2,allow_nan=False)+'\n')


if __name__=='__main__':
    try:main()
    except Exception as exc:
        print(type(exc).__name__,file=sys.stderr);raise SystemExit(1)
