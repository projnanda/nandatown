"""Small HTTP client shared by independent requester and worker processes."""
import json
import urllib.error
import urllib.request
from urllib.parse import urlsplit

class Mailbox:
    def __init__(self,url,run_id,name,token):
        parsed=urlsplit(url)
        if parsed.scheme!='http' or parsed.hostname not in ('127.0.0.1','localhost') or parsed.username or parsed.password:
            raise ValueError('This bounded example requires a loopback Town URL')
        self.base=url.rstrip('/')+'/runs/'+run_id;self.session=None;self.exchanges=[]
        self.session=self.call('POST','/join',{'name':name,'token':token})['session']

    def call(self,method,path,body=None,expected=(200,202,204)):
        request=urllib.request.Request(self.base+path,data=None if body is None else json.dumps(body,allow_nan=False).encode(),method=method)
        request.add_header('Content-Type','application/json')
        if self.session:request.add_header('X-Town-Session',self.session)
        try:
            with urllib.request.urlopen(request,timeout=10) as response:
                status=response.status;data=response.read()
        except urllib.error.HTTPError as exc:
            status=exc.code;data=exc.read()
        self.exchanges.append({'method':method,'path':path,'status':status})
        if status not in expected:raise RuntimeError('Town HTTP '+str(status)+' on '+path)
        value=json.loads(data) if data else None
        return (status,value) if len(expected)==1 and expected[0] not in (200,202,204) else value

    def send(self,mid,body,to='seller'):
        return self.call('POST','/messages',{'message_id':mid,'to':to,'kind':'manufacturing.request' if to=='seller' else 'manufacturing.response','body':body})

    def claim(self):return self.call('POST','/inbox/claim')
    def ack(self,claim,status='processed',note=None):
        return self.call('POST','/inbox/ack',{'message_id':claim['message_id'],'fence':claim['fence'],'status':status,'note':note or {}})
