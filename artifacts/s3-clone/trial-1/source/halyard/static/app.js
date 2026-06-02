'use strict';

// --------------------------------------------------------------------------
// API helpers
// --------------------------------------------------------------------------

const api = {
  async req(path, opts = {}) {
    const headers = opts.headers || {};
    if (opts.body && typeof opts.body !== 'string') {
      headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(opts.body);
    }
    const r = await fetch(path, {
      method: opts.method || 'GET',
      headers,
      body: opts.body,
      credentials: 'same-origin',
    });
    let data = null;
    if (r.status !== 204) {
      const ct = r.headers.get('content-type') || '';
      if (ct.includes('json')) {
        data = await r.json();
      } else {
        data = await r.text();
      }
    }
    if (!r.ok) {
      const err = new Error((data && data.error) || `HTTP ${r.status}`);
      err.status = r.status;
      err.body = data;
      throw err;
    }
    return data;
  },
  me() { return this.req('/console/api/me'); },
  login(ak, sk) {
    return this.req('/console/api/login', {
      method: 'POST',
      body: { access_key_id: ak, secret_access_key: sk },
    });
  },
  logout() { return this.req('/console/api/logout', { method: 'POST' }); },
  buckets() { return this.req('/console/api/buckets'); },
  createBucket(name) { return this.req('/console/api/buckets', { method: 'POST', body: { name } }); },
  deleteBucket(name) { return this.req('/console/api/buckets/' + encodeURIComponent(name), { method: 'DELETE' }); },
  accessKeys() { return this.req('/console/api/access-keys'); },
  createAccessKey() { return this.req('/console/api/access-keys', { method: 'POST' }); },
  deleteAccessKey(id) { return this.req('/console/api/access-keys/' + encodeURIComponent(id), { method: 'DELETE' }); },
};

// --------------------------------------------------------------------------
// State + render
// --------------------------------------------------------------------------

const state = {
  me: null,
  view: 'buckets',
  buckets: null,
  accessKeys: null,
  loading: false,
  loginError: null,
  bucketError: null,
  newBucket: '',
  modal: null,
  confirm: null,
};

const root = document.getElementById('app');

function el(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') e.className = v;
    else if (k === 'onClick') e.addEventListener('click', v);
    else if (k === 'onSubmit') e.addEventListener('submit', v);
    else if (k === 'onInput') e.addEventListener('input', v);
    else if (k === 'onChange') e.addEventListener('change', v);
    else if (k === 'onKeyDown') e.addEventListener('keydown', v);
    else if (k === 'innerHTML') e.innerHTML = v;
    else if (k.startsWith('data-')) e.setAttribute(k, v);
    else if (k === 'value') e.value = v;
    else if (v === true) e.setAttribute(k, '');
    else if (v === false || v === null || v === undefined) {/*skip*/}
    else e.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    if (typeof c === 'string' || typeof c === 'number') {
      e.appendChild(document.createTextNode(c));
    } else {
      e.appendChild(c);
    }
  }
  return e;
}

function render() {
  root.innerHTML = '';
  if (!state.me) {
    root.appendChild(renderLogin());
  } else {
    root.appendChild(renderShell());
  }
  if (state.modal) root.appendChild(state.modal);
  if (state.confirm) root.appendChild(renderConfirm());
}

// --------------------------------------------------------------------------
// Login
// --------------------------------------------------------------------------

function renderLogin() {
  const wrap = el('div', { class: 'login-wrap' });
  const card = el('div', { class: 'login-card', 'data-testid': 'login-panel' });
  card.appendChild(el('h1', {}, '⚓ Halyard'));
  card.appendChild(el('div', { class: 'subtitle' }, 'Sign in to your tenant'));

  const form = el('form', { 'data-testid': 'login-form', onSubmit: async (ev) => {
    ev.preventDefault();
    state.loading = true;
    state.loginError = null;
    render();
    try {
      const me = await api.login(akInput.value.trim(), skInput.value);
      state.me = me;
      state.view = 'buckets';
      state.loading = false;
      render();
      loadBuckets();
    } catch (err) {
      state.loginError = 'Invalid access key or secret key';
      state.loading = false;
      render();
    }
  }});

  const akInput = el('input', { type: 'text', 'data-testid': 'login-access-key-id', autocomplete: 'username', required: true, placeholder: 'AKIA...' });
  const skInput = el('input', { type: 'password', 'data-testid': 'login-secret-access-key', autocomplete: 'current-password', required: true, placeholder: 'Secret access key' });

  const akField = el('div', { class: 'field' },
    el('label', {}, 'Access Key ID'),
    akInput
  );
  const skField = el('div', { class: 'field' },
    el('label', {}, 'Secret Access Key'),
    skInput
  );

  form.appendChild(akField);
  form.appendChild(skField);

  if (state.loginError) {
    form.appendChild(el('div', { class: 'error-box', 'data-testid': 'login-error' }, state.loginError));
  } else {
    // empty placeholder to keep test stable
    form.appendChild(el('div', { 'data-testid': 'login-error', style: 'display:none' }));
  }

  const submit = el('button', { type: 'submit', class: 'primary', 'data-testid': 'login-submit', disabled: state.loading || null }, state.loading ? 'Signing in...' : 'Sign in');
  form.appendChild(submit);

  card.appendChild(form);
  wrap.appendChild(card);
  return wrap;
}

// --------------------------------------------------------------------------
// Shell
// --------------------------------------------------------------------------

function renderShell() {
  const shell = el('div', { class: 'shell' });
  const sidebar = el('div', { class: 'sidebar' });
  sidebar.appendChild(el('div', { class: 'brand' }, '⚓ Halyard'));
  const nav = el('div', { class: 'nav' },
    el('button', { 'data-testid': 'nav-buckets', class: state.view === 'buckets' ? 'active' : '', onClick: () => { state.view = 'buckets'; render(); loadBuckets(); } }, 'Buckets'),
    el('button', { 'data-testid': 'nav-access-keys', class: state.view === 'access-keys' ? 'active' : '', onClick: () => { state.view = 'access-keys'; render(); loadAccessKeys(); } }, 'Access keys'),
  );
  sidebar.appendChild(nav);

  const userArea = el('div', { class: 'user-area' });
  const tenantBox = el('div', { class: 'tenant' },
    el('span', { class: 'label' }, 'Tenant'),
    el('span', { class: 'tenant-name', 'data-testid': 'tenant-label' }, state.me.tenant),
  );
  userArea.appendChild(tenantBox);
  userArea.appendChild(el('button', {
    'data-testid': 'logout-btn',
    onClick: async () => { await api.logout(); state.me = null; state.view = 'buckets'; render(); }
  }, 'Sign out'));
  sidebar.appendChild(userArea);

  shell.appendChild(sidebar);

  const main = el('div', { class: 'main' });
  if (state.view === 'buckets') {
    main.appendChild(renderBuckets());
  } else {
    main.appendChild(renderAccessKeys());
  }
  shell.appendChild(main);
  return shell;
}

// --------------------------------------------------------------------------
// Buckets view
// --------------------------------------------------------------------------

function renderBuckets() {
  const panel = el('div', { 'data-testid': 'bucket-panel' });

  panel.appendChild(el('div', { class: 'page-header' },
    el('h1', { class: 'page-title' }, 'Buckets')
  ));

  // create form row
  const newRow = el('div', { class: 'flex-row', style: 'margin-bottom: 24px; max-width: 600px;' });
  const newInput = el('input', {
    type: 'text',
    'data-testid': 'new-bucket-name',
    placeholder: 'my-bucket-name',
    value: state.newBucket,
    onInput: (e) => { state.newBucket = e.target.value; },
    onKeyDown: (e) => { if (e.key === 'Enter') createBucket(); },
  });
  const newBtn = el('button', {
    class: 'primary grow-0',
    'data-testid': 'new-bucket-btn',
    onClick: createBucket,
  }, 'Create bucket');
  newRow.appendChild(newInput);
  newRow.appendChild(newBtn);
  panel.appendChild(newRow);

  if (state.bucketError) {
    panel.appendChild(el('div', { class: 'error-box', 'data-testid': 'new-bucket-error', style: 'margin-bottom: 16px; max-width: 600px;' }, state.bucketError));
  } else {
    panel.appendChild(el('div', { 'data-testid': 'new-bucket-error', style: 'display:none' }));
  }

  if (state.buckets === null) {
    panel.appendChild(el('div', { class: 'loading-bar', 'data-testid': 'loading-indicator' },
      el('span', { class: 'spinner' }), 'Loading buckets...'
    ));
    return panel;
  }

  if (state.buckets.length === 0) {
    panel.appendChild(el('div', { class: 'table-wrap empty', 'data-testid': 'bucket-empty-state' },
      el('h3', {}, 'No buckets yet'),
      el('p', { class: 'muted' }, 'Create your first bucket using the form above.')
    ));
    // hidden bucket-list to satisfy test ID lookups; emptiness is signified by empty-state visibility
    panel.appendChild(el('div', { 'data-testid': 'bucket-list', style: 'display:none' }));
    return panel;
  }

  // hidden empty state
  panel.appendChild(el('div', { 'data-testid': 'bucket-empty-state', style: 'display:none' }));

  const tbl = el('div', { class: 'table-wrap' });
  const table = el('table', { 'data-testid': 'bucket-list' });
  const thead = el('thead', {}, el('tr', {},
    el('th', {}, 'Name'), el('th', {}, 'Created'), el('th', { class: 'actions' }, '')
  ));
  const tbody = el('tbody', {});
  for (const b of state.buckets) {
    const row = el('tr', { 'data-testid': 'bucket-row', 'data-bucket-name': b.name },
      el('td', { class: 'code-mono' }, b.name),
      el('td', { class: 'muted' }, b.created_at || ''),
      el('td', { class: 'actions' },
        el('button', { class: 'danger', onClick: () => askDeleteBucket(b.name) }, 'Delete')
      )
    );
    tbody.appendChild(row);
  }
  table.appendChild(thead);
  table.appendChild(tbody);
  tbl.appendChild(table);
  panel.appendChild(tbl);
  return panel;
}

async function createBucket() {
  const name = (state.newBucket || '').trim();
  state.bucketError = null;
  if (!name) {
    state.bucketError = 'Bucket name cannot be empty.';
    render();
    return;
  }
  try {
    await api.createBucket(name);
    state.newBucket = '';
    state.bucketError = null;
    await loadBuckets();
  } catch (err) {
    if (err.body && err.body.error === 'InvalidBucketName') {
      state.bucketError = 'Invalid bucket name. Use lowercase letters, digits, and hyphens (3-63 chars).';
    } else if (err.body && err.body.error === 'BucketAlreadyExists') {
      state.bucketError = 'A bucket with that name already exists.';
    } else if (err.body && err.body.error === 'BucketAlreadyOwnedByYou') {
      state.bucketError = 'You already own a bucket with that name.';
    } else if (err.body && err.body.error === 'TooManyBuckets') {
      state.bucketError = 'Bucket quota reached for this tenant.';
    } else {
      state.bucketError = (err.body && (err.body.message || err.body.error)) || err.message || 'Could not create bucket.';
    }
    render();
  }
}

async function loadBuckets() {
  try {
    const r = await api.buckets();
    state.buckets = r.buckets || [];
    render();
  } catch (err) {
    state.buckets = [];
    render();
  }
}

function askDeleteBucket(name) {
  state.confirm = {
    title: 'Delete bucket',
    message: 'Permanently delete bucket "' + name + '"? The bucket must be empty.',
    confirmLabel: 'Delete',
    danger: true,
    onConfirm: async () => {
      state.confirm = null;
      try {
        await api.deleteBucket(name);
      } catch (err) {
        // ignore
      }
      await loadBuckets();
    },
  };
  render();
}

// --------------------------------------------------------------------------
// Access keys view
// --------------------------------------------------------------------------

function renderAccessKeys() {
  const panel = el('div', { 'data-testid': 'access-keys-panel' });
  panel.appendChild(el('div', { class: 'page-header' },
    el('h1', { class: 'page-title' }, 'Access keys'),
    el('button', { class: 'primary', 'data-testid': 'new-access-key-btn', onClick: createAccessKey }, 'Create access key')
  ));

  if (state.accessKeys === null) {
    panel.appendChild(el('div', { class: 'loading-bar', 'data-testid': 'loading-indicator' },
      el('span', { class: 'spinner' }), 'Loading access keys...'
    ));
    return panel;
  }

  const tbl = el('div', { class: 'table-wrap' });
  const table = el('table', { 'data-testid': 'access-keys-list' });
  const thead = el('thead', {}, el('tr', {},
    el('th', {}, 'Access Key ID'), el('th', {}, 'Created'), el('th', { class: 'actions' }, '')
  ));
  const tbody = el('tbody', {});
  if (state.accessKeys.length === 0) {
    tbody.appendChild(el('tr', {}, el('td', { colspan: '3' },
      el('div', { class: 'empty', style: 'padding:24px' }, 'No access keys yet.')
    )));
  } else {
    for (const k of state.accessKeys) {
      const isCurrent = state.me && k.access_key_id === state.me.access_key_id;
      const row = el('tr', { 'data-testid': 'access-key-row', 'data-access-key-id': k.access_key_id },
        el('td', { class: 'code-mono' }, k.access_key_id, isCurrent ? el('span', { class: 'muted', style: 'margin-left:8px' }, '(current)') : null),
        el('td', { class: 'muted' }, k.created_at || ''),
        el('td', { class: 'actions' },
          el('button', { class: 'danger', disabled: isCurrent || null, onClick: () => askDeleteAccessKey(k.access_key_id) }, 'Revoke')
        )
      );
      tbody.appendChild(row);
    }
  }
  table.appendChild(thead);
  table.appendChild(tbody);
  tbl.appendChild(table);
  panel.appendChild(tbl);
  return panel;
}

async function loadAccessKeys() {
  state.accessKeys = null;
  render();
  try {
    const r = await api.accessKeys();
    state.accessKeys = r.access_keys || [];
    render();
  } catch (err) {
    state.accessKeys = [];
    render();
  }
}

async function createAccessKey() {
  try {
    const r = await api.createAccessKey();
    const k = r.access_key;
    showNewAccessKeyModal(k.access_key_id, k.secret_access_key);
    await loadAccessKeys();
  } catch (err) { /*ignore*/ }
}

function showNewAccessKeyModal(id, secret) {
  const overlay = el('div', { class: 'modal-overlay' });
  const modal = el('div', { class: 'modal', 'data-testid': 'new-access-key-modal' });
  modal.appendChild(el('h2', {}, 'Access key created'));
  modal.appendChild(el('div', { class: 'warning-box' }, 'This is the only time the secret will be shown. Copy it before closing.'));
  modal.appendChild(el('label', {}, 'Access Key ID'));
  modal.appendChild(el('div', { class: 'code-box', 'data-testid': 'new-access-key-id' }, id));
  modal.appendChild(el('div', { style: 'height:14px' }));
  modal.appendChild(el('label', {}, 'Secret Access Key'));
  modal.appendChild(el('div', { class: 'code-box', 'data-testid': 'new-access-key-secret' }, secret));
  modal.appendChild(el('div', { class: 'modal-actions', style: 'margin-top:24px' },
    el('button', { class: 'primary', 'data-testid': 'new-access-key-close', onClick: () => { state.modal = null; render(); } }, 'I have copied my secret')
  ));
  overlay.appendChild(modal);
  state.modal = overlay;
  render();
}

function askDeleteAccessKey(id) {
  state.confirm = {
    title: 'Revoke access key',
    message: 'Permanently revoke access key "' + id + '"? Any clients using this key will stop working.',
    confirmLabel: 'Revoke',
    danger: true,
    onConfirm: async () => {
      state.confirm = null;
      try {
        await api.deleteAccessKey(id);
      } catch (err) { /*ignore*/ }
      await loadAccessKeys();
    },
  };
  render();
}

function renderConfirm() {
  const c = state.confirm;
  const overlay = el('div', { class: 'modal-overlay' });
  const modal = el('div', { class: 'modal', 'data-testid': 'confirm-dialog' });
  modal.appendChild(el('h2', { 'data-testid': 'confirm-dialog-title' }, c.title));
  modal.appendChild(el('p', { 'data-testid': 'confirm-dialog-message' }, c.message));
  modal.appendChild(el('div', { class: 'modal-actions' },
    el('button', { 'data-testid': 'confirm-dialog-cancel', onClick: () => { state.confirm = null; render(); } }, 'Cancel'),
    el('button', { class: c.danger ? 'danger' : 'primary', 'data-testid': 'confirm-dialog-confirm', onClick: c.onConfirm }, c.confirmLabel || 'Confirm'),
  ));
  overlay.appendChild(modal);
  return overlay;
}

// --------------------------------------------------------------------------
// Boot
// --------------------------------------------------------------------------

async function boot() {
  try {
    state.me = await api.me();
    state.view = 'buckets';
    render();
    loadBuckets();
  } catch (err) {
    state.me = null;
    render();
  }
}

boot();
