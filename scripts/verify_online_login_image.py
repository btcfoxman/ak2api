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
        run(['docker','run','--rm','--network','none','--entrypoint','python',image,
            '-B','-c','import app.main; print("isolated_application_import_ok")'])
        # Match compose init:true: xvfb-run must have an init parent instead of
        # becoming PID 1 while waiting for the X server's readiness signal.
        run(['docker','run','-d','--init','--name',name,'--network','none','--shm-size','1g',
            '--hostname','ak-offline-fixture','--add-host','ak-offline-fixture:127.0.0.1',
            '-e','AK_API_KEY='+token,'-e','AK_ADMIN_TOKEN='+token,
            '-e','AK_PROXY_POOL_ENABLED=false','-e','AK_PROXY_POOL=',
            '-e','AK_DAILY_CHECKIN_ENABLED=false','-e','AK_ACCOUNT_MAINTENANCE_INTERVAL_SECONDS=86400',
            '-e','AK_CHROME_EXECUTABLE=/usr/bin/chromium',
            '-e','AK_CHROME_USER_DATA_ROOT=/app/data/ak-chrome-profiles',image])
        ready=False;last_probe=''
        for _ in range(45):
            result=subprocess.run(['docker','exec',name,'python','-B','-c',
                "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8795/health',timeout=2).read()"],
                capture_output=True,text=True,timeout=8)
            if result.returncode==0:ready=True;break
            last_probe=(result.stdout+result.stderr)[-3000:]
            time.sleep(1)
        if not ready:
            details=subprocess.run(['docker','logs','--tail','70',name],capture_output=True,text=True,timeout=15)
            state=subprocess.run(['docker','inspect','--format','{{json .State}}',name],capture_output=True,text=True,timeout=10)
            process_probe="import json,pathlib; out=[]\nfor p in pathlib.Path('/proc').iterdir():\n if not p.name.isdigit():continue\n try:\n  a=(p/'cmdline').read_bytes().split(b'\\0'); out.append({'pid':p.name,'executable':a[0].decode(errors='replace'),'wchan':(p/'wchan').read_text().strip()})\n except (OSError,IndexError):pass\nprint(json.dumps(out))"
            processes=subprocess.run(['docker','exec',name,'python','-B','-c',process_probe],capture_output=True,text=True,timeout=10)
            print(json.dumps({'fixture_container_state':state.stdout.strip(),
                'fixture_processes':processes.stdout.strip(),
                'fixture_startup_log':(details.stdout+details.stderr)[-12000:].replace(token,'[fixture-token]'),
                'last_readiness_probe':last_probe.replace(token,'[fixture-token]')}),flush=True)
            raise AssertionError('isolated application did not become healthy; see fixture-only startup diagnostics')
        source=r'''
import http.cookies,http.server,json,time,threading,urllib.request,urllib.error
import websocket
from app.browser_context import _CDP,_cdp_json
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
# Exercise the actual Debian noVNC JavaScript, not only its RFB transport.
# A disposable tab in this fixture's empty account browser connects to its own
# private desktop. No external network, real account or generation is involved.
browser=_CDP(_cdp_json(19800+aid,'/json/version')['webSocketDebuggerUrl'])
target=None;page=None
try:
 target=browser.call('Target.createTarget',{'url':'about:blank'})['targetId']
 tab=next(item for item in _cdp_json(19800+aid,'/json/list')if item['id']==target)
 page=_CDP(tab['webSocketDebuggerUrl'])
 page.call('Network.enable');page.call('Page.enable')
 for name,value,path in [('ak_admin',TOKEN,'/'),('ak_login_viewer',jar['ak_login_viewer'].value,'/account-login')]:
  result=page.call('Network.setCookie',{'name':name,'value':value,'url':BASE+path,'path':path,'httpOnly':True,'sameSite':'Strict'})
  assert result.get('success') is True
 page.call('Page.navigate',{'url':BASE+'/account-login/viewer'})
 connected=False;state={}
 for _ in range(60):
  state=page.evaluate("({connected:document.getElementById('send-text')?.disabled===false,status:document.getElementById('status')?.textContent||''})")or{}
  if state.get('connected'):connected=True;break
  time.sleep(.25)
 assert connected,'real browser noVNC client did not connect: '+str(state.get('status',''))
finally:
 if page:page.close()
 if target:browser.call('Target.closeTarget',{'targetId':target})
 browser.close()
# Verify the real localStorage primitive on an entirely synthetic, offline
# origin. Only this fixture process overrides the fixed production origin.
from app import browser_context as consent_context
consent_http={'posts':0}
class ConsentFixture(http.server.BaseHTTPRequestHandler):
 def do_GET(self):
  body=b'<!doctype html><html><body><button id="generate" data-generation-action="true">Synthetic generation</button></body></html>'
  self.send_response(200);self.send_header('Content-Type','text/html');self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(body)
 def do_POST(self):
  consent_http['posts']+=1;self.send_response(403);self.end_headers()
 def log_message(self,*args):pass
server=http.server.ThreadingHTTPServer(('127.0.0.1',0),ConsentFixture)
thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
fixture_origin='http://ak-offline-fixture:'+str(server.server_port)
real_origin=consent_context.GENERATION_CONSENT_ORIGIN
browser=_CDP(_cdp_json(19800+aid,'/json/version')['webSocketDebuggerUrl'])
target=None;page=None;consent_report={}
try:
 target=browser.call('Target.createTarget',{'url':'about:blank'})['targetId']
 tab=next(item for item in _cdp_json(19800+aid,'/json/list')if item['id']==target)
 page=_CDP(tab['webSocketDebuggerUrl']);page.call('Network.enable');page.call('Page.enable')
 page.call('Page.navigate',{'url':fixture_origin+'/'})
 for _ in range(50):
  ready=page.evaluate("({origin:location.origin,ready:document.readyState})")or{}
  if ready.get('origin')==fixture_origin and ready.get('ready')=='complete':break
  time.sleep(.1)
 assert ready.get('origin')==fixture_origin and ready.get('ready')=='complete'
 page.evaluate(r"""(() => {
   window.__consentWrites=0;window.__consentClicks=0;window.__consentResumes=0;
   window.__realConsentSet=Storage.prototype.setItem;window.__realConsentGet=Storage.prototype.getItem;
   window.__pendingSyntheticGeneration={resume:()=>{window.__consentResumes++}};
   document.querySelector('#generate').addEventListener('click',()=>{window.__consentClicks++});
   Storage.prototype.setItem=function(key,value){window.__consentWrites++;return window.__realConsentSet.call(this,key,value)};
   return true;
 })()""")
 consent_context.GENERATION_CONSENT_ORIGIN=fixture_origin
 key=json.dumps(consent_context.GENERATION_CONSENT_KEY)
 def set_marker(value):
  if value is None:page.evaluate("localStorage.removeItem("+key+");window.__consentWrites=0")
  else:page.evaluate("window.__realConsentSet.call(localStorage,"+key+","+json.dumps(value)+");window.__consentWrites=0")
 def counters():
  return page.evaluate("({writes:window.__consentWrites,clicks:window.__consentClicks,resumes:window.__consentResumes})")
 set_marker('true')
 assert consent_context.ensure_generation_consent(page)['written']is False
 assert consent_context.ensure_generation_consent(page)['written']is False
 assert counters()=={'writes':0,'clicks':0,'resumes':0}
 consent_report['already_true_marker_writes']=0
 for label,value in [('false','false'),('missing',None)]:
  set_marker(value)
  receipt=consent_context.ensure_generation_consent(page)
  assert receipt['written']is True and receipt['server_acknowledgement']is False
  assert page.evaluate('localStorage.getItem('+key+')')=='true'
  assert counters()=={'writes':1,'clicks':0,'resumes':0}
  consent_report[label+'_marker_writes']=1
 set_marker('false')
 page.evaluate("Storage.prototype.setItem=function(){throw new DOMException('fixture storage denied','SecurityError')}")
 try:
  consent_context.ensure_generation_consent(page)
  raise AssertionError('blocked localStorage unexpectedly prepared agreement')
 except consent_context.AkoolBrowserConsentError:pass
 assert page.evaluate('localStorage.getItem('+key+')')=='false'
 assert counters()=={'writes':0,'clicks':0,'resumes':0}
 consent_report['blocked_storage_rejected']=True
 page.evaluate("Storage.prototype.setItem=function(){window.__consentWrites++}")
 try:
  consent_context.ensure_generation_consent(page)
  raise AssertionError('failed marker readback unexpectedly prepared agreement')
 except consent_context.AkoolBrowserConsentError:pass
 assert counters()=={'writes':1,'clicks':0,'resumes':0}
 consent_report['failed_readback_rejected']=True
 page.evaluate("Storage.prototype.setItem=function(key,value){window.__consentWrites++;return window.__realConsentSet.call(this,key,value)};window.__consentWrites=0")
 consent_context.GENERATION_CONSENT_ORIGIN=real_origin
 try:
  consent_context.ensure_generation_consent(page)
  raise AssertionError('wrong origin unexpectedly prepared agreement')
 except consent_context.AkoolBrowserConsentError:pass
 assert counters()=={'writes':0,'clicks':0,'resumes':0}
 consent_report['wrong_origin_rejected']=True
 consent_context.GENERATION_CONSENT_ORIGIN=fixture_origin
 try:
  consent_context._verified_context(page,{'email':'expected@example.invalid'},None,
    {'data':{'user':{'email':'different@example.invalid','_id':'different-user'}}})
  raise AssertionError('wrong identity unexpectedly reached localStorage')
 except consent_context.AkoolBrowserError:pass
 assert counters()=={'writes':0,'clicks':0,'resumes':0}
 assert consent_http['posts']==0
 consent_report.update(identity_checked_before_storage=True,synthetic_button_clicks=0,pending_action_resumes=0,post_requests=0)
finally:
 consent_context.GENERATION_CONSENT_ORIGIN=real_origin
 if page:page.close()
 if target:browser.call('Target.closeTarget',{'targetId':target})
 browser.close();server.shutdown();server.server_close();thread.join(timeout=3)
assert request('POST',f'/api/account-login/{sid}/cancel',{})[0]==200
assert request('GET','/account-login/credentials',cookie=cookie)[0]in{401,403}
assert request('GET','/api/account-login')[1]['active']is None
print(json.dumps({'app_healthy':True,'desktop_available':True,'real_chrome_xvfb_vnc_started':True,
 'authenticated_same_origin_viewer':True,'live_websocket_rfb_handshake':True,'real_browser_novnc_connected':True,'ticket_revoked_on_cancel':True,
 'unauthenticated_and_cross_site_rejected':True,'generation_consent':consent_report,'external_network':'disabled','generation_posts':0}))
'''.replace('__TOKEN__',repr(token))
        report=json.loads(run(['docker','exec','-i',name,'python','-B','-'],input=source,timeout=160))
        print(json.dumps(report))
    finally:
        subprocess.run(['docker','rm','-f',name],capture_output=True,timeout=35)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--image',required=True)
    verify(parser.parse_args().image)
