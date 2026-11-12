"""Nanda Town mailbox bridge to SKOPOS's loopback browser workflow adapter."""
import os
import time
from urllib.parse import urlsplit
import httpx
from nandatown.client import TownClient


def valid_url(url):
    value = urlsplit(url)
    return value.scheme=='http' and value.hostname in {'127.0.0.1','localhost'} and not value.username and not value.password and not value.query


def call_workflow(http, body):
    try:
        response = http.post('/workflow', json=body)
        value = response.json()
        if response.status_code not in {200,409,422}:
            return {'status':'UNKNOWN','reason':'unexpected_service_response'}
        return {**value,'http_status':response.status_code}
    except Exception:
        return {'status':'UNKNOWN','reason':'service_unreachable_or_protocol_error'}


def main():
    assert valid_url(os.environ['SKOPOS_URL']), 'loopback synthetic test only'
    town = TownClient(os.environ['TOWN_URL'], os.environ['RUN_ID'])
    town.join(os.environ['NAME'],os.environ['TOKEN'])
    with httpx.Client(base_url=os.environ['SKOPOS_URL'],headers={'Authorization':'Bearer '+os.environ['SKOPOS_TEST_TOKEN']},timeout=20) as http:
        deadline = time.monotonic()+120
        while time.monotonic()<deadline:
            claim = town.claim()
            if claim is None:time.sleep(.05);continue
            if claim['kind']!='skopos_workflow_request':
                town.ack(claim['message_id'],claim['fence'],'rejected',{'reason':'unsupported_kind'});continue
            result=call_workflow(http,claim['body'])
            town.send('result-'+claim['message_id'],claim['from'],'skopos_workflow_result',{'town_request_id':claim['message_id'],**result})
            town.ack(claim['message_id'],claim['fence'],'processed',{'skopos_status':result['status']})
    town.http.close()


if __name__=='__main__':main()
