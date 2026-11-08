/* Same-origin interactive login; all credentials stay in the account browser. */
((root) => {
  function createOnlineAccountController(document, options = {}) {
    const element = (id) => document.getElementById(id);
    const dialog = element('onlineAccountDialog');
    if (!dialog) return null;
    const form = element('onlineAccountForm');
    const fields = element('onlineAccountFields');
    const frame = element('onlineAccountFrame');
    const placeholder = element('onlineAccountPlaceholder');
    const message = element('onlineAccountMessage');
    const finish = element('onlineAccountFinish');
    const create = element('onlineAccountCreate');
    const fetcher = options.fetch || root.fetch.bind(root);
    const location = options.location || root.location;
    const refresh = options.refresh || (() => typeof root.refresh === 'function' ? root.refresh() : undefined);
    const toast = options.toast || ((value) => typeof root.toast === 'function' && root.toast(value));
    const readForm = options.readForm || (() => Object.fromEntries(new root.FormData(form)));
    const cancelled = new Map();
    let current = null;
    let cleanup = Promise.resolve();

    async function request(path, method = 'GET', body, keepalive = false) {
      const response = await fetcher(path, {
        method, credentials: 'same-origin', cache: 'no-store', keepalive,
        headers: {'Content-Type': 'application/json'},
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      let value;
      try { value = await response.json(); } catch { value = {}; }
      if (response.status === 401) {
        location.replace('/login');
        const error = new Error('管理登录已失效，请重新登录');
        error.status = 401;
        throw error;
      }
      if (!response.ok) {
        const detail = typeof value.detail === 'string' ? value.detail : `请求未完成（HTTP ${response.status}）`;
        const error = new Error(detail);
        error.status = response.status;
        throw error;
      }
      return value;
    }

    const active = (attempt) => current === attempt && dialog.open;
    function busy(attempt, value) {
      if (!active(attempt)) return;
      attempt.busy = value;
      create.disabled = value;
      finish.disabled = value;
      dialog.setAttribute('aria-busy', String(value));
    }
    function failure(attempt, error) {
      if (!active(attempt)) return;
      message.textContent = error.status === 409
        ? '账号正在执行任务或另一个登录窗口尚未结束，请稍后重试。'
        : error.message || '网页登录暂未完成，请稍后重试。';
    }
    function blankFrame() {
      frame.src = 'about:blank';
      frame.hidden = true;
      placeholder.hidden = false;
    }
    function cancelSession(id, keepalive = false) {
      if (!id) return Promise.resolve();
      if (cancelled.has(id)) return cancelled.get(id);
      const operation = request(`/api/account-login/${encodeURIComponent(id)}/cancel`, 'POST', undefined, keepalive)
        .catch(() => { cancelled.delete(id); });
      cancelled.set(id, operation);
      return operation;
    }
    function sessionFrom(value, accountId) {
      const session = value && value.active;
      if (!session || typeof session.session_id !== 'string' || !session.session_id
          || Number(session.account_id) !== accountId) {
        throw new Error('登录窗口与账号不匹配，请刷新后重试。');
      }
      return session;
    }
    async function show(value, attempt) {
      const session = sessionFrom(value, attempt.accountId);
      if (session.phase !== 'login') throw new Error('账号登录窗口正在处理中，请稍后重新打开。');
      attempt.sessionId = session.session_id;
      if (!active(attempt)) { await cancelSession(attempt.sessionId); return; }
      fields.hidden = true;
      finish.hidden = false;
      element('onlineAccountIdentity').textContent = `账号 #${attempt.accountId} · 登录会话与任务保持同步`;
      message.textContent = '请在下方账号网页完成登录，再点击“完成登录并验证”。会话会自动同步。';
      const ticket = await request(`/api/account-login/${encodeURIComponent(attempt.sessionId)}/viewer`, 'POST');
      if (!active(attempt)) { await cancelSession(attempt.sessionId); return; }
      if (ticket.url !== '/account-login/viewer') throw new Error('登录画面地址无效，请关闭后重新打开。');
      frame.src = ticket.url;
      frame.hidden = false;
      placeholder.hidden = true;
    }
    async function start(attempt) {
      const operation = request(`/api/accounts/${attempt.accountId}/login/start`, 'POST');
      attempt.starting = operation;
      try { await show(await operation, attempt); }
      finally { attempt.starting = null; }
    }
    async function close(keepalive = false) {
      const attempt = current;
      current = null;
      blankFrame();
      if (dialog.open) dialog.close();
      if (!attempt || attempt.finished) return cleanup;
      const operations = [cancelSession(attempt.sessionId, keepalive)];
      if (attempt.starting) operations.push(attempt.starting.then((value) => {
        const session = sessionFrom(value, attempt.accountId);
        return cancelSession(session.session_id, keepalive);
      }).catch(() => {}));
      cleanup = Promise.all(operations).then(() => undefined);
      return cleanup;
    }
    async function open(accountId = 0) {
      accountId = Number(accountId);
      if (!Number.isInteger(accountId) || accountId < 0 || current) return;
      const attempt = {accountId, sessionId: '', busy: true, starting: null, finished: false, available: false};
      current = attempt;
      form.reset();
      blankFrame();
      fields.hidden = accountId > 0;
      finish.hidden = true;
      element('onlineAccountTitle').textContent = accountId ? '账号网页登录' : '在线添加账号';
      element('onlineAccountIdentity').textContent = accountId ? `账号 #${accountId}` : '登录会话与任务保持同步';
      message.textContent = '正在准备账号网页登录…';
      if (!dialog.open) dialog.showModal();
      busy(attempt, true);
      try {
        await cleanup;
        if (!active(attempt)) return;
        const status = await request('/api/account-login');
        if (!active(attempt)) return;
        if (!status.available) throw new Error('在线登录组件暂不可用，请稍后重试。');
        attempt.available = true;
        if (status.active) {
          if (!accountId || Number(status.active.account_id) !== accountId) {
            const error = new Error('另一个登录窗口尚未结束');
            error.status = 409;
            throw error;
          }
          await show(status, attempt);
        } else if (accountId) await start(attempt);
        else message.textContent = '填写账号名称及代理设置，创建后在浏览器中登录。新账号验证完成前不会接收任务。';
      } catch (error) { failure(attempt, error); }
      finally { busy(attempt, false); }
    }
    async function submit(event) {
      event.preventDefault();
      const attempt = current;
      if (!attempt || attempt.busy || !attempt.available || attempt.accountId) return;
      busy(attempt, true);
      message.textContent = '正在创建待登录账号…';
      try {
        const values = readForm();
        const concurrency = Number(values.max_concurrency);
        if (!Number.isInteger(concurrency) || concurrency < 1 || concurrency > 100) throw new Error('并发上限须为 1–100 的整数。');
        const name = String(values.name || '').trim();
        if (!name) throw new Error('请填写账号名称。');
        const account = await request('/api/accounts/pending', 'POST', {
          name, proxy_url: String(values.proxy_url || '').trim(), max_concurrency: concurrency,
          use_proxy_pool: Boolean(form.elements.namedItem('use_proxy_pool').checked),
        });
        if (!active(attempt)) return;
        const id = Number(account.id);
        if (!Number.isInteger(id) || id <= 0) throw new Error('待登录账号创建结果无效，请刷新后检查。');
        attempt.accountId = id;
        fields.hidden = true;
        message.textContent = '正在打开账号网页登录…';
        await refresh();
        if (active(attempt)) await start(attempt);
      } catch (error) { failure(attempt, error); }
      finally { busy(attempt, false); }
    }
    async function complete() {
      const attempt = current;
      if (!attempt || attempt.busy || !attempt.sessionId) return;
      busy(attempt, true);
      message.textContent = '正在验证账号身份并同步最新会话…';
      try {
        const account = await request(`/api/account-login/${encodeURIComponent(attempt.sessionId)}/finish`, 'POST');
        if (Number(account.id) !== attempt.accountId) throw new Error('账号验证结果不匹配，请刷新后检查。');
        attempt.finished = true;
        attempt.sessionId = '';
        if (active(attempt)) {
          current = null;
          blankFrame();
          dialog.close();
          toast(account.enabled ? `账号 #${account.id} 已验证，会话已同步` : `账号 #${account.id} 会话已同步，仍保持禁用`);
        }
        await refresh();
      } catch (error) { failure(attempt, error); }
      finally { busy(attempt, false); }
    }
    function onClick(event) {
      const button = event.target.closest('[data-action="web-login"]');
      if (button && !button.disabled) open(Number(button.dataset.accountId || button.dataset.id));
    }
    element('onlineAddAccountButton').addEventListener('click', () => open(0));
    element('onlineAccountCancel').addEventListener('click', () => close());
    element('onlineAccountClose').addEventListener('click', () => close());
    form.addEventListener('submit', submit);
    finish.addEventListener('click', complete);
    document.addEventListener('click', onClick);
    dialog.addEventListener('cancel', (event) => { event.preventDefault(); close(); });
    dialog.addEventListener('close', () => close());
    const onHide = () => close(true);
    if (root.addEventListener) root.addEventListener('pagehide', onHide);
    return {open, close, submit, complete, get sessionId() { return current ? current.sessionId : ''; }};
  }
  if (typeof module !== 'undefined' && module.exports) module.exports = {createOnlineAccountController};
  if (root.document) root.AkAccountLogin = createOnlineAccountController(root.document);
})(typeof globalThis === 'undefined' ? window : globalThis);
