"""Idle-only image release with source verification, online backup and pinned rollback."""
from __future__ import annotations

import hashlib
import inspect
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import traceback

os.umask(0o077)
SERVICE = "ak2api"
ROOT = None
M = None

class Runtime:
    SERVICES = {"ak2api": "AK"}

    def inspect(self, service):
        return json.loads(subprocess.check_output(["docker", "inspect", service], text=True))[0]

    def container_python(self, service, source):
        import base64
        encoded = base64.b64encode(source.encode()).decode()
        result = subprocess.run(["docker", "exec", service, "python", "-B", "-c",
                                 "import base64;exec(base64.b64decode(" + repr(encoded) + "))"],
                                capture_output=True, text=True, timeout=90)
        if result.returncode:
            raise RuntimeError("private container probe failed")
        return result.stdout

    def compose(self, info, override):
        result = ["docker", "compose", "--project-directory", info["directory"], "-p", info["project"]]
        for path in info["configs"]:
            result += ["-f", path]
        return result + ["-f", str(override)]

r = Runtime()
TERMINAL = {"succeeded", "success", "completed", "complete", "done", "failed", "error", "cancelled", "canceled", "expired", "deleted", "timeout", "submission_unknown", "polling_paused"}


def assert_environment_preserved(before, after):
    old = dict(item.split("=", 1) for item in before)
    new = dict(item.split("=", 1) for item in after)
    assert len(old) == len(before) and len(new) == len(after), "duplicate environment keys"
    assert old == new, "container environment changed"


def save(name, value):
    target = ROOT / name
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    tmp.chmod(0o600)
    tmp.replace(target)


def command(args, label, timeout=90):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    (ROOT / (label + ".private.log")).write_text(result.stdout + result.stderr)
    if result.returncode:
        raise RuntimeError(label + " failed")
    return result.stdout


def python(source):
    return json.loads(r.container_python(SERVICE, source))


TASK_ROW_SNAPSHOT = "def task_snapshot_row(row):\n    return dict(row)\n"

SNAPSHOT = TASK_ROW_SNAPSHOT + '''import hashlib,json,os,pathlib,sqlite3,time
service=SERVICE
digest=lambda v:hashlib.sha256(json.dumps(v,sort_keys=True,separators=(',',':'),ensure_ascii=False,default=str).encode()).hexdigest()
base=pathlib.Path('/app')
hashes={str(p.relative_to(base)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (base/'app').rglob('*') if p.is_file() and '__pycache__' not in p.parts}
directories={base/'data'}
directories.update(pathlib.Path(v) for k,v in os.environ.items() if k.endswith('_DATA_DIR') and v)
candidates={(directory/(service+extension)).resolve() for directory in directories for extension in ('.db','.sqlite','.sqlite3')}
for key,value in os.environ.items():
 if key.endswith(('_DATABASE_PATH','_DB_PATH')) and value:candidates.add(pathlib.Path(value).resolve())
databases=[]
for path in sorted(candidates):
 if not path.is_file():continue
 with sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True,timeout=5) as db:
  db.row_factory=sqlite3.Row
  tables={v[0] for v in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
  if 'tasks' not in tables:continue
  db.execute('BEGIN')
  tasks={v['id']:digest(task_snapshot_row(v)) for v in db.execute('SELECT * FROM tasks ORDER BY id')}
  statuses=dict(db.execute('SELECT status,count(*) FROM tasks GROUP BY status'))
  settings={}
  for table in ('service_settings','runtime_settings','settings'):
   if table not in tables:continue
   rows=[dict(v) for v in db.execute('SELECT * FROM '+table)]
   for row in rows:
    if 'key' in row:
     key=row['key'];value=row.get('value',row.get('body'))
     if key=='account_balance_alert_state_v1':
      try:
       value=json.loads(value)
       if isinstance(value.get('last_refill'),dict):value['last_refill']['started_at']=None
      except (ValueError,TypeError):pass
     settings[table+':'+str(key)]=value
    else:settings[table]=rows
  manual={}
  if 'costref_config' in tables:
   for row in db.execute('SELECT kind,id,body FROM costref_config ORDER BY kind,id'):
    value=json.loads(row['body'])
    if value.get('origin') not in {'workbook','purchase_default'}:
     manual[row['kind']+':'+row['id']]=digest(value)
  protected_tables={}
  if 'image_costs' in tables:
   protected_tables['image_costs']=digest([dict(v) for v in db.execute('SELECT * FROM image_costs ORDER BY id')])
  account_statuses={};account_config={};browser_profiles=[]
  account_activity={'active_tasks':0,'future_login_leases':0,'future_maintenance_leases':0}
  reward_statuses={}
  if 'account_rewards' in tables:
   for reward in db.execute('SELECT body FROM account_rewards'):
    state=json.loads(reward[0]).get('status','')
    if state in {'claiming','submitting'}:reward_statuses[state]=reward_statuses.get(state,0)+1
  if 'accounts' in tables:
   cols={v[1] for v in db.execute('PRAGMA table_info(accounts)')}
   if 'status' in cols:account_statuses=dict(db.execute('SELECT status,count(*) FROM accounts GROUP BY status'))
   protected={'name','email','enabled','max_concurrency','concurrency','weight','proxy_url','proxy_id','min_balance','purchase_unit_price','region','expected_user_id','username','priority','group_id','model_map','pool','password','auto_login','profile_dir','cdp_port'}
   for raw in db.execute('SELECT * FROM accounts ORDER BY id'):
    row=dict(raw)
    if 'body' in row:
     try:row={**json.loads(row['body']),**row}
     except (ValueError,TypeError):pass
    protected.update({'platform','token','access_token','authorization','user_id','uid','team_id','product_id','df_id','client_ip','user_agent','sec_ch_ua','sec_ch_ua_platform','cookies','cookies_json','cookie_header','weryai_cookies_json','enable_auto_maintenance'})
    config={key:row[key] for key in protected if key in row}
    config['daily_rewards_enabled']=bool(row.get('daily_rewards_enabled',0))
    # A default-only schema migration must not look like a changed account.
    # Lease/revision/timestamps are runtime activity rather than configuration.
    config['session_mode']=row.get('session_mode')or''
    config['session_source']=row.get('session_source')or''
    binding=row.get('browser_binding_json')or'{}'
    config['browser_binding_json']=json.loads(binding)if isinstance(binding,str)else binding
    account_config[str(row['id'])]=digest(config)
    account_activity['active_tasks']+=max(0,int(row.get('active_tasks')or 0))
    account_activity['future_login_leases']+=int(int(row.get('login_lease_until')or 0)>time.time())
    account_activity['future_maintenance_leases']+=int(int(row.get('maintenance_lease_until')or 0)>time.time())
    profile_root=settings.get('settings:chrome_user_data_root')or os.environ.get('AK_CHROME_USER_DATA_ROOT')or'data/ak-chrome-profiles'
    profile=pathlib.Path(str(row.get('profile_dir')or'').strip()or str(pathlib.Path(profile_root)/('account-'+str(int(row['id']))))).resolve()
    port=int(row.get('cdp_port')or(int(os.environ.get('AK_CHROME_CDP_BASE_PORT')or 19800)+int(row['id'])))
    assert 1024<=port<=65535,'account browser port invalid'
    browser_profiles.append({'account_id':int(row['id']),'profile':str(profile),'port':port})
  databases.append({'path':str(path),'task_hashes':tasks,'statuses':statuses,'settings':settings,'manual_cost_config':manual,'account_statuses':account_statuses,'account_config':account_config,'account_activity':account_activity,'browser_profiles':browser_profiles,'reward_statuses':reward_statuses,'protected_tables':protected_tables})
print(json.dumps({'app_hashes':hashes,'databases':databases}))
'''.replace("SERVICE", repr(SERVICE))


def snapshot():
    return python(SNAPSHOT)


def busy(snap):
    result = {}
    for db in snap["databases"]:
        result.update({"reward_" + key: value for key, value in db.get("reward_statuses", {}).items() if value})
        result.update({key: value for key, value in db["statuses"].items() if str(key).lower() not in TERMINAL and value})
        result.update({"account_" + str(key): value for key, value in db["account_statuses"].items()
                       if str(key).lower() in {"logging_in", "login_pending", "authorizing", "verifying",
                           "browser_login", "profile_resetting", "maintenance", "maintaining", "checking_in", "claiming"} and value})
        for key, value in db.get("account_activity", {}).items():
            if value:
                result["account_" + key] = result.get("account_" + key, 0) + value
    return result


CHROME_EXECUTABLES = {"chrome", "chromium", "chromium-browser", "google-chrome", "google-chrome-stable"}


def _process_record(path):
    try:
        arguments = [os.fsdecode(value) for value in (path / "cmdline").read_bytes().split(b"\0") if value]
        tail = (path / "stat").read_text().rpartition(")")[2].split()
        return {"pid": int(path.name), "start": tail[19], "state": tail[0], "arguments": arguments}
    except (FileNotFoundError, ProcessLookupError):
        return None


def _chrome_option(arguments, name):
    values = []
    for index, value in enumerate(arguments):
        if value.startswith(name + "="):
            values.append(value.split("=", 1)[1])
        elif value == name and index + 1 < len(arguments):
            values.append(arguments[index + 1])
    return values[0] if len(values) == 1 else None


def _account_browser_processes(proc_root, bindings):
    expected = {(str(Path(row["profile"]).resolve()), str(int(row["port"]))): int(row["account_id"])
                for row in bindings}
    assert len(expected) == len(bindings), "account browser bindings are not unique"
    owned, unknown = [], []
    for path in proc_root.iterdir():
        if not path.name.isdigit():
            continue
        item = _process_record(path)
        arguments = item["arguments"] if item else []
        if not arguments or Path(arguments[0]).name not in CHROME_EXECUTABLES:
            continue
        if any(value == "--type" or value.startswith("--type=") for value in arguments[1:]):
            continue  # Renderer/GPU/zygote children are closed by their browser parent.
        profile = _chrome_option(arguments, "--user-data-dir")
        port = _chrome_option(arguments, "--remote-debugging-port")
        key = (str(Path(profile).resolve()), str(int(port))) if profile and port and port.isdigit() else None
        identity = {key: item[key] for key in ("pid", "start")}
        if key in expected:
            owned.append({**identity, "account_id": expected[key]})
        else:
            unknown.append(identity)
    return owned, unknown


def _term_browser(proc_root, identity):
    # pidfd binds the signal to this exact process even if a PID is recycled.
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        raise RuntimeError("safe_browser_flush_requires_pidfd")
    try:
        descriptor = os.pidfd_open(identity["pid"])
    except ProcessLookupError:
        return
    try:
        current = _process_record(proc_root / str(identity["pid"]))
        if current and current["start"] == identity["start"] and current["state"] != "Z":
            signal.pidfd_send_signal(descriptor, signal.SIGTERM)
    finally:
        os.close(descriptor)


def _flush_browser_processes(proc_root, bindings, app_identity, timeout=10):
    application = _process_record(proc_root / str(app_identity["pid"]))
    assert application and application["start"] == app_identity["start"] and application["state"] in {"T", "t"}, "application must remain frozen during browser flush"
    owned, unknown = _account_browser_processes(proc_root, bindings)
    assert not unknown, "unrecognized browser prevents safe deployment"
    for identity in owned:
        _term_browser(proc_root, identity)
    deadline = time.monotonic() + min(10, max(0, timeout))
    while True:
        remaining = []
        for identity in owned:
            item = _process_record(proc_root / str(identity["pid"]))
            if item and item["start"] == identity["start"] and item["state"] != "Z":
                remaining.append(identity)
        if not remaining:
            return {"flushed_account_browsers": len(owned), "graceful_profile_flush": True}
        if time.monotonic() >= deadline:
            raise RuntimeError("account_browser_flush_timeout_no_force_kill")
        time.sleep(.1)


def flush_account_browsers(frozen, identity):
    fresh = snapshot()
    assert not busy(fresh), "account activity started before browser flush"
    assert len(fresh["databases"]) == len(frozen["databases"]) == 1
    current, expected = fresh["databases"][0], frozen["databases"][0]
    assert current["account_config"] == expected["account_config"], "account configuration changed before browser flush"
    assert current["browser_profiles"] == expected["browser_profiles"], "account browser bindings changed before flush"
    helpers = "\n\n".join(inspect.getsource(function) for function in (
        _process_record, _chrome_option, _account_browser_processes, _term_browser, _flush_browser_processes))
    source = ("import json,os,signal,time\nfrom pathlib import Path\nCHROME_EXECUTABLES=" + repr(CHROME_EXECUTABLES)
              + "\n" + helpers + "\nprint(json.dumps(_flush_browser_processes(Path('/proc'),"
              + repr(current["browser_profiles"]) + "," + repr(identity) + ",timeout=10)))")
    return python(source)


def sources_guard():
    current = r.inspect(SERVICE)
    assert current["State"]["Running"] and current["Image"] == M["image_id"], "runtime image changed"
    snap = snapshot()
    assert snap["app_hashes"] == M["app_hashes"], "runtime source changed"
    assert len(snap["databases"]) == 1, "one task database required"
    for name, expected in M["files"].items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == expected
    return snap


def signal_app(name, expected=None):
    # docker-init/xvfb-run may be PID 1; freeze the actual single ASGI process.
    source = '''import json,os,pathlib,signal
matches=[]
for path in pathlib.Path('/proc').iterdir():
 if not path.name.isdigit():continue
 try:
  args=(path/'cmdline').read_bytes().decode().split('\\0')
  executable=pathlib.Path(args[0]).name if args else ''
  if ((executable.startswith('python') or executable=='uvicorn') and any(v=='uvicorn' or v.endswith('/uvicorn') for v in args) and any(v in {'main:app','app.main:app'} for v in args)) or (executable.startswith('python') and len(args)>1 and args[1] in {'main.py','/app/main.py'}):matches.append(path)
 except (FileNotFoundError,PermissionError,ProcessLookupError):pass
assert len(matches)==1,'expected one application process'
root=matches[0]
identity={'pid':int(root.name),'start':root.joinpath('stat').read_text().split()[21]}
assert EXPECTED is None or identity==EXPECTED,'application process restarted'
os.kill(identity['pid'],getattr(signal,SIGNAL))
print(json.dumps(identity))
'''.replace("EXPECTED", repr(expected)).replace("SIGNAL", repr(name))
    return python(source)


def handoff_marker(identity, expected_image=None):
    image = expected_image or M["image_id"]
    return ROOT / ("stop-handoff-" + image.split(":")[-1][:16] + "-" + str(identity["pid"]) + "-" + str(identity["start"]) + ".json")


def commit_stop(identity, expected_image=None):
    # From this point onward a daemon stop may still be pending even when its
    # client times out. Never thaw this PID to accept new requests afterwards.
    marker = handoff_marker(identity, expected_image)
    assert not marker.exists(), "stop handoff already committed"
    marker.write_text(json.dumps({"identity": identity, "image": expected_image or M["image_id"]}))


def resume(identity, expected_image=None):
    if identity is None:
        return
    if handoff_marker(identity, expected_image).exists():
        return
    try:
        state = r.inspect(SERVICE)
        if state["State"]["Running"] and state["Image"] == (expected_image or M["image_id"]):
            signal_app("SIGCONT", identity)
    except Exception:
        pass


def watchdog(identity, expected_image=None):
    probe = "import os,pathlib,signal; pid=" + str(identity["pid"]) + "; assert (pathlib.Path('/proc')/str(pid)/'stat').read_text().split()[21]==" + repr(identity["start"]) + "; os.kill(pid,signal.SIGCONT)"
    code = "import time,subprocess,json,pathlib\ntime.sleep(45)\n" + "try:\n item=json.loads(subprocess.check_output(['docker','inspect'," + repr(SERVICE) + "]))[0]\n if not pathlib.Path(" + repr(str(handoff_marker(identity, expected_image))) + ").exists() and item['State']['Running'] and item['Image']==" + repr(expected_image or M["image_id"]) + ":\n  subprocess.run(['docker','exec'," + repr(SERVICE) + ",'python','-c'," + repr(probe) + "],capture_output=True,timeout=10)\nexcept Exception:pass\n"
    subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def probe_request(path, *, mode="admin"):
    """Read only localhost using the host's existing admin authentication."""
    assert mode in {"admin", "sync", "none"}
    assert path.startswith("/") and not path.startswith("//")
    ports = r.inspect(SERVICE)["NetworkSettings"]["Ports"]
    port = next(int(name.split("/")[0]) for name, bindings in ports.items() if bindings)
    source = '''import os,json,urllib.request,urllib.error,hmac,hashlib,time
prefix=PREFIX
token=os.environ.get(prefix+('_COST_SYNC_TOKEN' if MODE=='sync' else '_ADMIN_TOKEN'),'')
if MODE=='admin' and not token:token=os.environ.get(prefix+'_API_KEY','')
headers={'Authorization':'Bearer '+token,'X-Admin-Token':token,'X-Requested-With':'art2api','Accept':'application/json'} if MODE!='none' else {}
if MODE=='admin':
 if prefix in {'MAG','POL','AK'}:headers['Cookie']=prefix.lower()+'_admin='+token
 if prefix=='HIGG':headers['Cookie']='higg_admin='+hmac.new(token.encode(),b'higg2api-admin',hashlib.sha256).hexdigest()
 if prefix=='LINGYA':headers['Cookie']='lingya_admin_session='+hmac.new(token.encode(),b'lingya-admin-session-v1',hashlib.sha256).hexdigest()
 if prefix=='ART':
  expiry=str(int(time.time())+300)
  headers['Cookie']='art_session='+expiry+'.'+hmac.new(token.encode(),expiry.encode(),hashlib.sha256).hexdigest()
request=urllib.request.Request(URL,headers=headers,method='GET')
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
 with opener.open(request,timeout=10) as response:
  raw=response.read().decode()
  result={'status':response.status,'data':json.loads(raw) if 'application/json' in response.headers.get('Content-Type','') else {}}
except urllib.error.HTTPError as error:result={'status':error.code}
print(json.dumps(result))
'''
    for name, value in {"PREFIX": repr(r.SERVICES[SERVICE]), "MODE": repr(mode), "URL": repr("http://127.0.0.1:" + str(port) + path)}.items():
        source = source.replace(name, value)
    return json.loads(r.container_python(SERVICE, source))


def health(path="/health", mode="none"):
    last_status, last_error = None, None
    for _ in range(30):
        assert r.inspect(SERVICE)["State"]["Running"], "application stopped"
        try:
            response = probe_request(path, mode=mode)
            last_status = response["status"]
            if response["status"] == 200:
                return response
        except Exception as error:
            last_error = type(error).__name__
        time.sleep(2)
    raise RuntimeError("service health timeout: status=" + str(last_status) + ", error=" + str(last_error))


def rollback_if_idle(old, built, restore, stopped_identity=None):
    """Never replace a candidate which has accepted new work during recovery."""
    current = r.inspect(SERVICE)
    assert current["Image"] in {M["image_id"], built["image_id"]}, "unexpected replacement; refusing rollback"
    if not current["State"]["Running"]:
        command(restore, "rollback", timeout=120)
        health("/health", mode="none")
        assert r.inspect(SERVICE)["Image"] == M["image_id"], "rollback image mismatch"
        return True
    if current["Id"] == old["Id"] and current["Image"] == M["image_id"]:
        if stopped_identity and handoff_marker(stopped_identity).exists():
            command(restore, "rollback", timeout=120)
            health("/health", mode="none")
            assert r.inspect(SERVICE)["Image"] == M["image_id"], "rollback image mismatch"
            return True
        health("/health", mode="none")
        return False
    assert current["Image"] == built["image_id"], "unexpected replacement; refusing rollback"
    identity = None
    try:
        identity = signal_app("SIGSTOP")
        watchdog(identity, expected_image=current["Image"])
        frozen_at = time.monotonic()
        frozen = r.inspect(SERVICE)
        assert frozen["Id"] == current["Id"] and frozen["Image"] == current["Image"], "candidate changed before rollback"
        frozen_snapshot = snapshot()
        if busy(frozen_snapshot):
            return False
        flush_account_browsers(frozen_snapshot, identity)
        assert time.monotonic() - frozen_at < 25, "rollback freeze budget exceeded"
        commit_stop(identity, expected_image=current["Image"])
        command(["docker", "stop", "--time", "0", current["Id"]], "stop-idle-candidate", timeout=30)
        command(restore, "rollback", timeout=120)
        health("/health", mode="none")
        assert r.inspect(SERVICE)["Image"] == M["image_id"], "rollback image mismatch"
        return True
    finally:
        resume(identity, expected_image=current["Image"])


def deploy():
    initial = sources_guard()
    if busy(initial):
        save("status.json", {"phase": "deferred", "service": SERVICE, "busy": busy(initial)})
        return
    built = json.loads((ROOT / "build.json").read_text())
    old = r.inspect(SERVICE)
    info = {"project": M["project"], "directory": M["directory"], "configs": M["configs"]}
    backup = ROOT / "backups"
    backup.mkdir(exist_ok=True, mode=0o700)
    config_paths = list(dict.fromkeys([str(Path(M["directory"]) / ".env"), *M["configs"]]))
    config_hashes = {name: hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in config_paths}
    for index, name in enumerate(config_paths):
        (backup / (str(index) + "-" + Path(name).name)).write_bytes(Path(name).read_bytes())
    save("compose.json", {"services": {SERVICE: {"image": built["image"]}}})
    assert command(["docker", "image", "inspect", "--format", "{{.Id}}", built["base_image"]], "verify-rollback-image").strip() == M["image_id"]
    assert command(["docker", "image", "inspect", "--format", "{{.Id}}", built["image"]], "verify-candidate-image").strip() == built["image_id"]
    save("rollback.compose.json", {"services": {SERVICE: {"image": built["base_image"]}}})
    start = r.compose(info, ROOT / "compose.json") + ["up", "-d", "--no-deps", "--no-build", "--pull", "never", SERVICE]
    restore = r.compose(info, ROOT / "rollback.compose.json") + ["up", "-d", "--no-deps", "--no-build", "--pull", "never", "--force-recreate", SERVICE]
    identity, stopped = None, False
    try:
        # Online backup first. Large databases must never spend the brief
        # application freeze waiting on backup/check/copy work.
        path = initial["databases"][0]["path"]
        temporary = "/tmp/" + M["release"] + "-" + SERVICE + ".db"
        r.container_python(SERVICE, "import sqlite3,os\nwith sqlite3.connect('file:'+" + repr(path) + "+'?mode=ro',uri=True) as src,sqlite3.connect(" + repr(temporary) + ") as dst:\n src.backup(dst)\n assert dst.execute('PRAGMA quick_check').fetchone()[0]=='ok'\nos.chmod(" + repr(temporary) + ",0o600)")
        command(["docker", "cp", SERVICE + ":" + temporary, str(backup / "tasks.db")], "backup", timeout=120)
        r.container_python(SERVICE, "from pathlib import Path\nPath(" + repr(temporary) + ").unlink()")
        identity = signal_app("SIGSTOP")
        watchdog(identity)
        frozen_at = time.monotonic()
        cutover = snapshot()
        if busy(cutover):
            save("status.json", {"phase": "deferred", "service": SERVICE, "busy": busy(cutover)})
            return
        assert cutover["app_hashes"] == M["app_hashes"], "source changed before cutover"
        frozen_container = r.inspect(SERVICE)
        assert frozen_container["Id"] == old["Id"] and frozen_container["Image"] == M["image_id"], "container changed before cutover"
        save("cutover.private.json", cutover)
        profile_flush = flush_account_browsers(cutover, identity)
        assert time.monotonic() - frozen_at < 25, "freeze budget exceeded"
        assert config_hashes == {name: hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in config_paths}
        commit_stop(identity)
        stopped = True
        command(["docker", "stop", "--time", "0", old["Id"]], "stop-idle", timeout=30)
        command(start, "start", timeout=120)
        identity = None
        service_health = health()
        current = r.inspect(SERVICE)
        assert current["Image"] == built["image_id"], "running image differs from validated candidate"
        after = snapshot()
        before_db, after_db = cutover["databases"][0], after["databases"][0]
        assert all(after_db["task_hashes"].get(key) == value for key, value in before_db["task_hashes"].items()), "historical task modified"
        assert before_db["settings"] == after_db["settings"], "settings modified"
        assert before_db["protected_tables"] == after_db["protected_tables"], "image cost records modified"
        assert all(after_db["account_config"].get(key) == value for key, value in before_db["account_config"].items()), "account configuration modified"
        assert all(after_db["manual_cost_config"].get(key) == value for key, value in before_db["manual_cost_config"].items()), "manual cost settings modified"
        assert_environment_preserved(old["Config"]["Env"], current["Config"]["Env"])
        assert old["Mounts"] == current["Mounts"] and old["HostConfig"]["PortBindings"] == current["HostConfig"]["PortBindings"], "mounts/ports changed"
        assert set(old["NetworkSettings"]["Networks"]) == set(current["NetworkSettings"]["Networks"]), "networks changed"
        assert {key: value for key, value in M["app_hashes"].items() if key not in M["files"]} == {key: value for key, value in after["app_hashes"].items() if key not in M["files"]}, "unrelated source changed"
        assert all(after["app_hashes"].get(key) == value for key, value in M["files"].items()), "candidate source mismatch"
        assert config_hashes == {name: hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in config_paths}
        report = {"phase": "deployed", "service": SERVICE, "image": built["image"], "files": len(M["files"]),
                  "tasks_preserved": len(before_db["task_hashes"]), "manual_cost_config_preserved": len(before_db["manual_cost_config"]),
                  "settings_routes_unchanged": True, "account_configuration_unchanged": True,
                  "existing_environment_unchanged": True, "profile_flush": profile_flush,
                  "other_sources_unchanged": True,
                  "health_status": service_health["status"], "paid_requests": 0}
        save("deployment.json", report)
        save("status.json", report)
    except BaseException:
        resume(identity)
        if stopped:
            rollback_if_idle(old, built, restore, stopped_identity=identity)
        raise
    finally:
        resume(identity)


def main():
    import argparse
    import shutil
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--deploy-path", required=True)
    args = parser.parse_args()
    assert all(c in "0123456789abcdef" for c in args.revision) and len(args.revision) == 40
    assert args.image.endswith(":" + args.revision)
    old = r.inspect(SERVICE)
    assert old["State"]["Running"], "this guarded updater requires an existing running service"
    labels = old["Config"]["Labels"]
    directory = labels["com.docker.compose.project.working_dir"]
    assert Path(directory).resolve() == Path(args.deploy_path).resolve(), "unexpected compose directory"
    configs = labels["com.docker.compose.project.config_files"].split(",")
    assert configs and all(Path(name).is_file() for name in configs)
    global ROOT, M
    ROOT = Path(directory) / "releases" / ("online-accounts-" + args.revision[:16] + "-" + str(time.time_ns()))
    ROOT.mkdir(parents=True, mode=0o700)
    initial = snapshot()
    checkout = Path(__file__).resolve().parents[1]
    expected_baseline = json.loads((checkout / "docs/deploy-source-baseline.json").read_text())
    assert initial["app_hashes"] == expected_baseline, "runtime differs from the reviewed deployment baseline"
    candidate = {str(p.relative_to(checkout)): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in (checkout / "app").rglob("*") if p.is_file() and "__pycache__" not in p.parts}
    # Any inherited application source must remain byte-for-byte identical.
    # The isolated image probe verifies the entire expected source union.
    inherited = {name: value for name, value in initial["app_hashes"].items() if name not in candidate}
    expected_sources = {**inherited, **candidate}
    files = {name: value for name, value in candidate.items() if initial["app_hashes"].get(name) != value}
    for name in files:
        target = ROOT / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(checkout / name, target)
    M = {"service": SERVICE, "release": "online-accounts-" + args.revision[:16], "image_id": old["Image"],
         "app_hashes": initial["app_hashes"], "files": files,
         "project": labels["com.docker.compose.project"], "directory": directory, "configs": configs}
    save("manifest.json", M)
    candidate_probe = "import pathlib,hashlib,json; b=pathlib.Path('/app'); print(json.dumps({str(p.relative_to(b)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (b/'app').rglob('*') if p.is_file() and '__pycache__' not in p.parts}))"
    actual = json.loads(command(["docker", "run", "--rm", "--network", "none", "--entrypoint", "python",
                                 args.image, "-B", "-c", candidate_probe], "candidate-source", timeout=60))
    assert actual == expected_sources, "published image differs from reviewed checkout or inherited runtime source"
    base = "ak-online-accounts-rollback:" + old["Image"].split(":")[-1][:16]
    command(["docker", "tag", old["Image"], base], "pin-base")
    save("build.json", {"base_image": base, "image": args.image,
                        "image_id": command(["docker", "image", "inspect", "--format", "{{.Id}}", args.image], "candidate-image").strip()})
    deadline = time.monotonic() + 1200
    while busy(sources_guard()):
        if time.monotonic() >= deadline:
            save("status.json", {"phase": "deferred", "service": SERVICE, "reason": "active work"})
            raise RuntimeError("active tasks or account login prevented safe deployment")
        time.sleep(5)
    deploy()
    status = json.loads((ROOT / "status.json").read_text())
    assert status["phase"] == "deployed", "cutover deferred; the old service remains running"
    print(json.dumps({"phase": "deployed", "revision": args.revision, "receipt": str(ROOT / "deployment.json"),
                      "tasks_preserved": status["tasks_preserved"], "paid_requests": 0}))


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        if ROOT is not None:
            (ROOT / "failure.private.log").write_text(traceback.format_exc())
        raise
