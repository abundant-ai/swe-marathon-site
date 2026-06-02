(function() {
  'use strict';
  const $app = document.getElementById('app');
  let state = { view: 'buckets', auth: null, buckets: [], keys: [] };

  function el(tag, attrs, ...children) {
    const e = document.createElement(tag);
    if (attrs) {
      for (const k in attrs) {
        if (k === 'class') e.className = attrs[k];
        else if (k === 'onClick') e.addEventListener('click', attrs[k]);
        else if (k === 'onSubmit') e.addEventListener('submit', attrs[k]);
        else if (k.startsWith('data-')) e.setAttribute(k, attrs[k]);
        else e[k] = attrs[k];
      }
    }
    for (const c of children) {
      if (c == null || c === false) continue;
      if (typeof c === 'string' || typeof c === 'number') e.appendChild(document.createTextNode(c));
      else e.appendChild(c);
    }
    return e;
  }

  async function api(path, opts) {
    opts = opts || {};
    opts.headers = Object.assign({'Content-Type': 'application/json'}, opts.headers || {});
    opts.credentials = 'same-origin';
    const r = await fetch(path, opts);
    let body = null;
    if (r.status !== 204) { try { body = await r.json(); } catch(e) {} }
    return { ok: r.ok, status: r.status, body: body };
  }

  function render() { renderApp(); }

  async function refreshBuckets() {
    const r = await api('/console/api/buckets');
    if (r.ok) state.buckets = r.body.buckets || [];
  }
  async function refreshKeys() {
    const r = await api('/console/api/access-keys');
    if (r.ok) state.keys = r.body.access_keys || [];
  }

  function renderApp() {
    $app.innerHTML = '';
    if (!state.auth) { $app.appendChild(renderLogin()); return; }
    $app.appendChild(renderShell());
  }

  function renderLogin() {
    const errorEl = el('div', {class:'error', 'data-testid':'login-error'});
    const akInput = el('input', {type:'text','data-testid':'login-access-key-id'});
    const skInput = el('input', {type:'password','data-testid':'login-secret-access-key'});
    const form = el('form', {'data-testid':'login-form', onSubmit: async (e) => {
      e.preventDefault();
      errorEl.textContent = '';
      const r = await api('/console/api/login', {method:'POST', body: JSON.stringify({access_key_id: akInput.value, secret_access_key: skInput.value})});
      if (r.ok) { state.auth = r.body; state.view = 'buckets'; await refreshBuckets(); render(); }
      else { errorEl.textContent = 'Invalid credentials'; }
    }},
      el('label', null, 'Access Key ID'), akInput,
      el('label', null, 'Secret Access Key'), skInput,
      el('div', {style:'margin-top:16px;display:flex;justify-content:flex-end;'},
        el('button', {type:'submit','data-testid':'login-submit'}, 'Sign in')),
      errorEl);
    return el('div', {class:'login-wrap'},
      el('div', {class:'login-card','data-testid':'login-panel'},
        el('h1', null, 'Halyard Console'), form));
  }

  function renderShell() {
    const sidebar = el('div', {class:'sidebar'},
      el('h2', null, 'Halyard'),
      el('nav', null,
        el('button', {'data-testid':'nav-buckets', class: state.view==='buckets'?'active':'',
          onClick: () => { state.view='buckets'; refreshBuckets().then(render); }}, 'Buckets'),
        el('button', {'data-testid':'nav-access-keys', class: state.view==='access-keys'?'active':'',
          onClick: () => { state.view='access-keys'; refreshKeys().then(render); }}, 'Access keys')),
      el('div', {class:'tenant-info'},
        el('div', null, 'Signed in as'),
        el('div', {class:'name','data-testid':'tenant-label'}, state.auth.tenant),
        el('div', {class:'code', style:'margin-top:6px;font-size:12px;'}, state.auth.access_key_id),
        el('button', {style:'margin-top:12px;width:100%;', class:'secondary','data-testid':'logout-btn',
          onClick: async () => { await api('/console/api/logout',{method:'POST'}); state.auth=null; render(); }}, 'Sign out')));
    const main = el('div', {class:'main'}, state.view==='buckets' ? renderBucketsView() : renderKeysView());
    return el('div', {class:'shell'}, sidebar, main);
  }

  function renderBucketsView() {
    const errorEl = el('div', {class:'error','data-testid':'new-bucket-error'});
    const nameInput = el('input', {type:'text','data-testid':'new-bucket-name', placeholder:'bucket-name'});
    const createBtn = el('button', {'data-testid':'new-bucket-btn', onClick: async () => {
      errorEl.textContent = '';
      const name = nameInput.value.trim();
      const r = await api('/console/api/buckets', {method:'POST', body: JSON.stringify({name})});
      if (r.ok) { nameInput.value=''; await refreshBuckets(); renderApp(); }
      else { errorEl.textContent = (r.body && r.body.message) || 'Error'; }
    }}, 'Create bucket');
    const panel = el('div', {'data-testid':'bucket-panel'},
      el('h1', null, 'Buckets'),
      el('div', {class:'toolbar'}, nameInput, createBtn),
      errorEl);
    if (!state.buckets.length) {
      panel.appendChild(el('div', {class:'empty-state','data-testid':'bucket-empty-state'}, 'No buckets yet. Create your first bucket above.'));
    } else {
      const list = el('table', {'data-testid':'bucket-list'},
        el('thead', null, el('tr', null, el('th',null,'Name'), el('th',null,'Created'))));
      const tbody = el('tbody');
      for (const b of state.buckets) {
        tbody.appendChild(el('tr', {'data-testid':'bucket-row','data-bucket-name': b.name},
          el('td', null, b.name), el('td', null, b.created_at)));
      }
      list.appendChild(tbody);
      panel.appendChild(list);
    }
    return panel;
  }

  function renderKeysView() {
    const panel = el('div', {'data-testid':'access-keys-panel'},
      el('h1', null, 'Access keys'),
      el('div', {class:'toolbar'},
        el('button', {'data-testid':'new-access-key-btn', onClick: async () => {
          const r = await api('/console/api/access-keys', {method:'POST'});
          if (r.ok) { await refreshKeys(); renderApp(); showNewKeyModal(r.body.access_key); }
        }}, 'Create access key')));
    if (!state.keys.length) {
      panel.appendChild(el('div', {class:'empty-state'}, 'No access keys.'));
    } else {
      const list = el('table', {'data-testid':'access-keys-list'},
        el('thead', null, el('tr', null, el('th',null,'Access Key ID'), el('th',null,'Created'), el('th',null,''))));
      const tbody = el('tbody');
      for (const k of state.keys) {
        const isCurrent = k.access_key_id === state.auth.access_key_id;
        tbody.appendChild(el('tr', {'data-testid':'access-key-row','data-access-key-id': k.access_key_id},
          el('td', null, el('span', {class:'code'}, k.access_key_id), isCurrent ? el('span', {class:'tag', style:'margin-left:8px'}, 'current') : null),
          el('td', null, k.created_at),
          el('td', null, el('button', {class:'danger', onClick: () => confirmRevoke(k.access_key_id, isCurrent)}, 'Revoke'))));
      }
      list.appendChild(tbody);
      panel.appendChild(list);
    }
    return panel;
  }

  function showNewKeyModal(key) {
    const backdrop = el('div', {class:'modal-backdrop','data-testid':'new-access-key-modal'},
      el('div', {class:'modal'},
        el('h3', null, 'New access key created'),
        el('div', {class:'warn'}, 'Save the secret now — it will not be shown again.'),
        el('label', null, 'Access Key ID'),
        el('div', {class:'code','data-testid':'new-access-key-id'}, key.access_key_id),
        el('label', null, 'Secret Access Key'),
        el('div', {class:'code','data-testid':'new-access-key-secret'}, key.secret_access_key),
        el('div', {class:'modal-actions'},
          el('button', {'data-testid':'new-access-key-close', onClick: () => backdrop.remove()}, 'Close'))));
    document.body.appendChild(backdrop);
  }

  function confirmRevoke(akid, isCurrent) {
    const message = isCurrent
      ? 'You cannot revoke the access key for your current session.'
      : 'Revoke access key ' + akid + '? This cannot be undone.';
    const backdrop = el('div', {class:'modal-backdrop','data-testid':'confirm-dialog'},
      el('div', {class:'modal'},
        el('h3', {'data-testid':'confirm-dialog-title'}, 'Confirm revocation'),
        el('div', {'data-testid':'confirm-dialog-message'}, message),
        el('div', {class:'modal-actions'},
          el('button', {class:'secondary','data-testid':'confirm-dialog-cancel', onClick: () => backdrop.remove()}, 'Cancel'),
          el('button', {class:'danger','data-testid':'confirm-dialog-confirm', onClick: async () => {
            backdrop.remove();
            if (isCurrent) return;
            await api('/console/api/access-keys/' + encodeURIComponent(akid), {method:'DELETE'});
            await refreshKeys();
            renderApp();
          }}, 'Revoke'))));
    document.body.appendChild(backdrop);
  }

  // Bootstrap: check session
  (async () => {
    const r = await api('/console/api/me');
    if (r.ok) { state.auth = r.body; await refreshBuckets(); }
    render();
  })();
})();
