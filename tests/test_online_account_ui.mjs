import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createRequire} from 'node:module';
import {test} from 'node:test';
import vm from 'node:vm';

const require = createRequire(import.meta.url);
const {createOnlineAccountController} = require('../app/static/account-login.js');

class Element {
  constructor() { this.listeners = new Map(); this.hidden = false; this.disabled = false; this.open = false; this.src = 'about:blank'; this.textContent = ''; this.attributes = {}; }
  addEventListener(type, callback) { const list = this.listeners.get(type) || []; list.push(callback); this.listeners.set(type, list); }
  async emit(type, event = {}) { for (const callback of this.listeners.get(type) || []) await callback(event); }
  setAttribute(key, value) { this.attributes[key] = value; }
  showModal() { this.open = true; }
  close() { if (!this.open) return; this.open = false; this.emit('close'); }
  reset() { this.resetCount = (this.resetCount || 0) + 1; }
}
function deferred() { let resolve; const promise = new Promise((complete) => { resolve = complete; }); return {promise, resolve}; }
const tick = () => new Promise((resolve) => setImmediate(resolve));
async function until(condition) { for (let count = 0; count < 15 && !condition(); count++) await tick(); assert.ok(condition(), 'expected asynchronous UI state'); }
const session = (id = 71, sessionId = 'owned-session') => ({available: true, active: {account_id: id, session_id: sessionId, phase: 'login', remaining_seconds: 300}});

function fixture(handler) {
  const nodes = new Map();
  const document = new Element();
  document.getElementById = (id) => { if (!nodes.has(id)) nodes.set(id, new Element()); return nodes.get(id); };
  const node = (id) => document.getElementById(id);
  node('onlineAccountForm').elements = {namedItem: () => ({checked: true})};
  const requests = [], redirects = [], toasts = [], events = new Map();
  let refreshes = 0;
  const originalListener = globalThis.addEventListener;
  globalThis.addEventListener = (name, callback) => events.set(name, callback);
  const controller = createOnlineAccountController(document, {
    fetch: async (path, init) => {
      requests.push({path, ...init, json: init.body ? JSON.parse(init.body) : undefined});
      const result = await handler(path, init, requests);
      return {status: result.status || 200, ok: !result.status || result.status < 400, json: async () => result.body || {}};
    },
    location: {replace: (path) => redirects.push(path)},
    readForm: () => ({name: 'Online fixture', proxy_url: '', max_concurrency: '8'}),
    refresh: async () => { refreshes++; }, toast: (value) => toasts.push(value),
  });
  if (originalListener === undefined) delete globalThis.addEventListener; else globalThis.addEventListener = originalListener;
  return {controller, document, node, requests, redirects, toasts, events, refreshes: () => refreshes};
}
function standard(path) {
  if (path === '/api/account-login') return {body: {available: true, active: null}};
  if (path === '/api/accounts/pending') return {body: {id: 71, enabled: false}};
  if (path.endsWith('/login/start')) return {body: session()};
  if (path.endsWith('/viewer')) return {body: {url: '/account-login/viewer'}};
  if (path.endsWith('/finish')) return {body: {id: 71, enabled: true}};
  if (path.endsWith('/cancel')) return {body: {ok: true}};
  assert.fail(`unexpected path ${path}`);
}

test('new account opens a same-origin viewer and finishes once without cancelling the verified session', async () => {
  const ui = fixture(standard);
  await ui.controller.open();
  await ui.controller.submit({preventDefault() {}});
  assert.equal(ui.node('onlineAccountFrame').src, '/account-login/viewer');
  assert.equal(ui.node('onlineAccountFinish').hidden, false);
  const pending = ui.requests.find((request) => request.path === '/api/accounts/pending');
  assert.deepEqual(pending.json, {name: 'Online fixture', proxy_url: '', max_concurrency: 8, use_proxy_pool: true});
  assert.ok(ui.requests.every((request) => request.credentials === 'same-origin' && request.cache === 'no-store'));
  await ui.controller.complete();
  assert.equal(ui.node('onlineAccountDialog').open, false);
  assert.equal(ui.node('onlineAccountFrame').src, 'about:blank');
  assert.equal(ui.controller.sessionId, '');
  assert.equal(ui.requests.filter((request) => request.path.endsWith('/finish')).length, 1);
  assert.equal(ui.requests.filter((request) => request.path.endsWith('/cancel')).length, 0);
  assert.equal(ui.refreshes(), 2);
  assert.match(ui.toasts[0], /会话已同步/);
});

test('existing-account opening does not create another account', async () => {
  const ui = fixture(standard);
  await ui.controller.open(71);
  assert.equal(ui.requests.some((request) => request.path === '/api/accounts/pending'), false);
  assert.equal(ui.node('onlineAccountFields').hidden, true);
  assert.equal(ui.node('onlineAccountTitle').textContent, '账号网页登录');
  await ui.controller.close();
  assert.equal(ui.requests.filter((request) => request.path.endsWith('/cancel')).length, 1);
});

test('closing during a late start cancels that owned lease once without opening a late iframe', async () => {
  const gate = deferred();
  const ui = fixture((path) => path.endsWith('/login/start') ? gate.promise : standard(path));
  const opened = ui.controller.open(71);
  await until(() => ui.requests.some((request) => request.path.endsWith('/login/start')));
  const closed = ui.controller.close();
  gate.resolve({body: session()});
  await Promise.all([opened, closed]);
  assert.equal(ui.node('onlineAccountDialog').open, false);
  assert.equal(ui.node('onlineAccountFrame').src, 'about:blank');
  assert.equal(ui.requests.filter((request) => request.path.endsWith('/viewer')).length, 0);
  assert.equal(ui.requests.filter((request) => request.path.endsWith('/cancel')).length, 1);
});

test('late viewer response cannot revive a closed login window', async () => {
  const gate = deferred();
  const ui = fixture((path) => path.endsWith('/viewer') ? gate.promise : standard(path));
  const opened = ui.controller.open(71);
  await until(() => ui.requests.some((request) => request.path.endsWith('/viewer')));
  const closed = ui.controller.close();
  gate.resolve({body: {url: '/account-login/viewer'}});
  await Promise.all([opened, closed]);
  assert.equal(ui.node('onlineAccountFrame').src, 'about:blank');
  assert.equal(ui.requests.filter((request) => request.path.endsWith('/cancel')).length, 1);
});

test('Escape and the dialog close event revoke the viewer without double cancellation', async () => {
  const ui = fixture(standard);
  await ui.controller.open(71);
  let prevented = false;
  await ui.node('onlineAccountDialog').emit('cancel', {preventDefault() { prevented = true; }});
  await tick();
  assert.ok(prevented);
  assert.equal(ui.node('onlineAccountDialog').open, false);
  assert.equal(ui.requests.filter((request) => request.path.endsWith('/cancel')).length, 1);
});

test('pagehide sends bounded keepalive cancellation and clears the iframe', async () => {
  const ui = fixture(standard);
  await ui.controller.open(71);
  await ui.events.get('pagehide')();
  const cancel = ui.requests.find((request) => request.path.endsWith('/cancel'));
  assert.equal(cancel.keepalive, true);
  assert.equal(cancel.body, undefined);
  assert.equal(ui.node('onlineAccountFrame').src, 'about:blank');
});

test('a busy account gives an explicit waiting message without browser or retry endpoints', async () => {
  const ui = fixture((path) => path.endsWith('/login/start') ? {status: 409, body: {detail: 'account has active work'}} : standard(path));
  await ui.controller.open(71);
  assert.match(ui.node('onlineAccountMessage').textContent, /正在执行任务/);
  assert.equal(ui.node('onlineAccountFrame').src, 'about:blank');
  assert.equal(ui.requests.some((request) => /\/viewer|retry|generate/.test(request.path)), false);
});

test('401 redirects to management login and cannot create pending accounts', async () => {
  const ui = fixture(() => ({status: 401, body: {detail: 'login required'}}));
  await ui.controller.open();
  await ui.controller.submit({preventDefault() {}});
  assert.deepEqual(ui.redirects, ['/login']);
  assert.equal(ui.requests.length, 1);
});

test('wrong-account or non-login-phase sessions are never viewed or cancelled', async () => {
  for (const value of [session(99), {...session(), active: {...session().active, phase: 'syncing'}}]) {
    const ui = fixture((path) => path === '/api/account-login' ? {body: value} : standard(path));
    await ui.controller.open(71);
    await ui.controller.close();
    assert.equal(ui.requests.length, 1);
    assert.equal(ui.node('onlineAccountFrame').src, 'about:blank');
  }
});

test('a viewer URL cannot switch the iframe to another origin', async () => {
  const ui = fixture((path) => path.endsWith('/viewer') ? {body: {url: 'https://untrusted.example.test/?ticket=secret'}} : standard(path));
  await ui.controller.open(71);
  assert.equal(ui.node('onlineAccountFrame').src, 'about:blank');
  assert.match(ui.node('onlineAccountMessage').textContent, /地址无效/);
  await ui.controller.close();
  assert.equal(ui.requests.filter((request) => request.path.endsWith('/cancel')).length, 1);
});

test('task login uses the assigned existing account and disables absent or deleted accounts', () => {
  const source = readFileSync(new URL('../app/static/app.js', import.meta.url), 'utf8');
  const helper = source.slice(source.indexOf('function taskAccountLogin('), source.indexOf('function renderTasks('));
  const context = vm.createContext({state: {accounts: [{id: 37}]}, escapeHtml: (value) => String(value)});
  vm.runInContext(helper, context);
  const ready = context.taskAccountLogin({account_id: 37});
  assert.match(ready, /data-account-id="37"/);
  assert.doesNotMatch(ready, / disabled/);
  for (const id of [undefined, 0, 99, 'bad-id']) assert.match(context.taskAccountLogin({account_id: id}), / disabled/);
  assert.match(context.taskAccountLogin({account_id: 99}), /关联账号已删除/);
});

test('viewer credentials stay in same-origin requests and clipboard secrets are cleared', () => {
  const viewer = readFileSync(new URL('../app/static/account-viewer.html', import.meta.url), 'utf8');
  assert.match(viewer, /fetch\('\/account-login\/credentials'/);
  assert.match(viewer, /location\.host}\/account-login\/websockify/);
  assert.doesNotMatch(viewer, /(?:password|ticket|token)=/);
  assert.match(viewer, /text\.value = ''/);
  assert.match(viewer, /window\.addEventListener\('pagehide'/);
  assert.doesNotMatch(viewer, /console\.(?:log|debug)/);
});
