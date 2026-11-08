"""Hosted-only real Chrome/Xvfb/VNC smoke; no network or paid generation."""
import argparse
import json
from pathlib import Path
import secrets
import subprocess
import time


def run(args, *, input=None, timeout=90):
    result=subprocess.run(args,input=input,capture_output=True,text=True,timeout=timeout)
    if result.returncode:raise RuntimeError('isolated_image_check_failed: '+args[1])
    return result.stdout


def verify(image):
    name='ak-online-login-smoke-'+secrets.token_hex(6)
    token=secrets.token_urlsafe(24)
    try:
        run(['docker','run','-d','--name',name,'--network','none','--shm-size','1g',
            '-e','AK_API_KEY='+token,'-e','AK_ADMIN_TOKEN='+token,
            '-e','AK_PROXY_POOL_ENABLED=false','-e','AK_PROXY_POOL=',
            '-e','AK_DAILY_CHECKIN_ENABLED=false','-e','AK_ACCOUNT_MAINTENANCE_INTERVAL_SECONDS=86400',
            '-e','AK_CHROME_EXECUTABLE=/usr/bin/chromium',
            '-e','AK_CHROME_USER_DATA_ROOT=/app/data/ak-chrome-profiles',image])
        ready=False
        for _ in range(45):
            result=subprocess.run(['docker','exec',name,'python','-B','-c',
                "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8795/health',timeout=2).read()"],
                capture_output=True,text=True,timeout=8)
            if result.returncode==0:ready=True;break
            time.sleep(1)
        assert ready,'isolated application did not become healthy'
        source=r'''
import http.cookies,json,urllib.request,urllib.error
import websocket
BASE='http://127.0.0.1:8795'
TOKEN=__TOKEN__
def request(method,path,body=None,cookie=None,origin=None):
 headers={'Cookie':cookie if cookie is not None else 'ak_admin='+TOKEN,'Accept':'application/json'}
 if origin is not None:headers['Origin']=origin
 if body is not None:headers['Content-Type']='application/json'
 req=urllib.request.Request(BASE+path,data=json.dumps(body).encode()if body is not None else None,headers=headers,method=method)
 opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
 try:
  with opener.open(req,timeout=60)as response:
   raw=response.read();kind=response.headers.get('content-type','')
   return response.status,json.loads(raw)if'application/json'in kind else raw,response.headers
 except urllib.error.HTTPError as error:return error.code,None,error.headers
status,info,_=request('GET','/api/account-login')
assert status==200 and info['available'] is True and info['active'] is None
assert request('POST','/api/accounts/pending',{'name':'unauthorized'},cookie='')[0]==401
assert request('POST','/api/accounts/pending',{'name':'cross-origin'},origin='https://invalid.example')[0]==403
status,account,_=request('POST','/api/accounts/pending',{'name':'isolated-smoke','proxy_url':'','use_proxy_pool':False,'max_concurrency':1})
assert status==200 and account['enabled'] is False
aid=account['id']
status,info,_=request('POST',f'/api/accounts/{aid}/login/start',{})
assert status==200 and info['active']['account_id']==aid and info['active']['phase']=='login'
sid=info['active']['session_id']
status,ticket,headers=request('POST',f'/api/account-login/{sid}/viewer',{})
assert status==200 and ticket['url']=='/account-login/viewer'
jar=http.cookies.SimpleCookie();jar.load(headers['Set-Cookie'])
assert jar['ak_login_viewer']['httponly']
cookie='ak_admin='+TOKEN+'; ak_login_viewer='+jar['ak_login_viewer'].value
assert request('GET','/account-login/viewer',cookie=cookie)[0]==200
status,credentials,_=request('GET','/account-login/credentials',cookie=cookie)
assert status==200 and len(credentials['password'])>=8
assert request('GET','/account-login/novnc/core/rfb.js',cookie=cookie)[0]==200
connection=websocket.create_connection('ws://127.0.0.1:8795/account-login/websockify',
 cookie=cookie,origin=BASE,timeout=10,subprotocols=['binary'],http_proxy_host=None)
try:
 banner=connection.recv()
 assert isinstance(banner,bytes)and banner.startswith(b'RFB ')
finally:connection.close()
assert request('POST',f'/api/account-login/{sid}/cancel',{})[0]==200
assert request('GET','/account-login/credentials',cookie=cookie)[0]in{401,403}
assert request('GET','/api/account-login')[1]['active']is None
print(json.dumps({'app_healthy':True,'desktop_available':True,'real_chrome_xvfb_vnc_started':True,
 'authenticated_same_origin_viewer':True,'live_websocket_rfb_handshake':True,'ticket_revoked_on_cancel':True,
 'unauthenticated_and_cross_site_rejected':True,'external_network':'disabled','generation_posts':0}))
'''.replace('__TOKEN__',repr(token))
        report=json.loads(run(['docker','exec','-i',name,'python','-B','-'],input=source,timeout=160))
        print(json.dumps(report))
    finally:
        subprocess.run(['docker','rm','-f',name],capture_output=True,timeout=35)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--image',required=True)
    verify(parser.parse_args().image)
