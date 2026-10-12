import os
import json
import time
import numpy as np
import pytest
from starlette.testclient import TestClient

os.environ['TRUEPAGE_API_KEY'] = 'test-only-' + 'x' * 40
os.environ['PUBLIC_BASE_URL'] = 'https://testserver'
from truepage import server
from truepage.scanner import png


@pytest.fixture(scope='module')
def client():
    with TestClient(server.app, base_url='https://testserver') as client:
        yield client


def new(client):
    r = client.post('/api/jobs', headers={'Authorization':'Bearer '+server.API_KEY})
    assert r.status_code == 200
    return r.json()['session_id']


def upload(client, ident):
    rgb = np.full((300,400,3), 255, np.uint8)
    rgb[50:100,50:100] = [200,0,0]
    r = client.put('/api/jobs/'+ident, content=png(rgb))
    assert r.status_code == 200


def test_auth_and_health(client):
    assert client.get('/healthz').status_code == 200
    assert client.post('/api/jobs').status_code == 401
    assert client.post('/mcp').status_code == 401
    assert client.get('/api/jobs/not-a-session').status_code == 400
    assert client.post('/api/jobs',headers={'Origin':'https://evil.example'}).status_code == 403


def test_left_then_right_review_and_delete(client):
    ident = new(client); upload(client, ident)
    body = {'corners':[[0,0],[199,0],[199,299],[0,299]], 'page':'right','layout':'spread','reviewed':True}
    url = '/api/jobs/'+ident+'/export'
    assert client.post(url,json=body).status_code == 400
    body['page'] = 'left';body['reviewed'] = False
    assert client.post(url,json=body).status_code == 400
    body['reviewed'] = True
    result = client.post(url,json=body)
    assert result.status_code == 200
    download = client.get(result.json()['download_url'])
    assert download.status_code == 200 and download.content.startswith(b'\x89PNG')
    assert download.headers['cache-control'] == 'no-store'
    body['page']='right';body['corners']=[[200,0],[399,0],[399,299],[200,299]]
    assert client.post(url,json=body).status_code == 200
    body['rotation']=90
    assert client.post(url,json=body).status_code == 400
    assert client.delete('/api/jobs/'+ident).status_code == 200
    assert client.get(result.json()['download_url']).status_code == 404


def test_oauth_pkce_approval_and_code_replay(client):
    import base64
    import hashlib
    from urllib.parse import urlparse, parse_qs
    verifier = 'v' * 64
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    metadata = client.get('/.well-known/oauth-authorization-server')
    assert metadata.status_code == 200
    reg = client.post('/register', json={'client_name':'Test client','redirect_uris':['https://client.example/callback'],
        'grant_types':['authorization_code','refresh_token'],'response_types':['code'],'token_endpoint_auth_method':'none'})
    assert reg.status_code == 201, reg.text
    cid = reg.json()['client_id']
    params={'client_id':cid,'redirect_uri':'https://client.example/callback','response_type':'code',
        'code_challenge':challenge,'code_challenge_method':'S256','scope':'scan','state':'test-state',
        'resource':'https://testserver/mcp'}
    auth = client.get('/authorize', params=params, follow_redirects=False)
    assert auth.status_code in (302,303,307), auth.text
    login = auth.headers['location']
    assert client.get(login).status_code == 200
    assert client.post(login,data={'key':'wrong'}).status_code == 401
    approved = client.post(login,data={'key':server.API_KEY},follow_redirects=False)
    assert approved.status_code == 303
    query = parse_qs(urlparse(approved.headers['location']).query)
    assert query['state'] == ['test-state']
    form={'grant_type':'authorization_code','client_id':cid,'code':query['code'][0],
        'redirect_uri':'https://client.example/callback','code_verifier':verifier,'resource':'https://testserver/mcp'}
    bad=dict(form);bad['code_verifier']='z'*64
    assert client.post('/token',data=bad).status_code == 400
    token=client.post('/token',data=form)
    assert token.status_code == 200, token.text
    assert client.post('/token',data=form).status_code == 400
    headers={'Authorization':'Bearer '+token.json()['access_token'],'Accept':'application/json, text/event-stream'}
    result=client.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':1,'method':'tools/list','params':{}})
    assert result.status_code == 200, result.text


def test_limits_expiry_and_upload_replacement(client):
    ident = new(client)
    assert client.put('/api/jobs/'+ident,content=b'x'*(12*1024*1024+1)).status_code == 413
    assert client.put('/api/jobs/'+ident,content=b'invalid').status_code == 400
    upload(client, ident)
    assert client.put('/api/jobs/'+ident,content=b'replacement').status_code == 400
    server.JOBS[ident]['expires']=time.time()-1
    assert client.get('/api/jobs/'+ident).status_code == 400
    assert not (server.ROOT/ident).exists()


def test_mcp_discovery_and_real_tool_call(client):
    headers={'Authorization':'Bearer '+server.API_KEY,'Accept':'application/json, text/event-stream'}
    r=client.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-03-26','capabilities':{},'clientInfo':{'name':'test','version':'1'}}})
    assert r.status_code == 200, r.text
    r=client.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':2,'method':'tools/list','params':{}})
    names={t['name'] for t in r.json()['result']['tools']}
    assert names == {'create_upload_session','get_scan_status','scan_uploaded_page','delete_session'}
    r=client.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'create_upload_session','arguments':{}}})
    assert not r.json()['result'].get('isError'), r.text
    ident=json.loads(r.json()['result']['content'][0]['text'])['session_id']
    assert client.delete('/api/jobs/'+ident).status_code == 200
