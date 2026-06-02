const app = document.getElementById("app");

const state = {
  me: null,
  view: "buckets",
  buckets: [],
  keys: [],
  loading: false,
  newKey: null,
  confirm: null,
};

function h(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function api(path, options = {}) {
  setLoading(true);
  try {
    const res = await fetch(`/console/api/${path}`, {
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
      ...options,
    });
    const text = await res.text();
    const data = text ? JSON.parse(text) : {};
    if (!res.ok) {
      const err = new Error(data.message || data.error || "Request failed");
      err.code = data.error;
      err.status = res.status;
      throw err;
    }
    return data;
  } finally {
    setLoading(false);
  }
}

function setLoading(v) {
  state.loading = v;
  const el = document.querySelector("[data-testid=loading-indicator]");
  if (el) el.style.display = v ? "block" : "none";
}

async function boot() {
  try {
    state.me = await api("me");
    state.view = "buckets";
    await loadBuckets();
  } catch {
    state.me = null;
    render();
  }
}

async function loadBuckets() {
  const data = await api("buckets");
  state.buckets = data.buckets || [];
  render();
}

async function loadKeys() {
  const data = await api("access-keys");
  state.keys = data.access_keys || [];
  render();
}

function render() {
  if (!state.me) return renderLogin();
  app.innerHTML = `
    <div class="shell">
      <aside class="sidebar">
        <div class="brand">Halyard</div>
        <div class="tenant">Tenant <strong data-testid="tenant-label">${h(state.me.tenant)}</strong></div>
        <nav class="nav">
          <button data-testid="nav-buckets" class="${state.view === "buckets" ? "active" : ""}">Buckets</button>
          <button data-testid="nav-access-keys" class="${state.view === "keys" ? "active" : ""}">Access keys</button>
        </nav>
        <div class="spacer"></div>
        <button class="secondary" data-testid="logout-btn">Log out</button>
      </aside>
      <main class="main">
        ${state.view === "buckets" ? bucketsView() : keysView()}
      </main>
    </div>
    <div id="loading-indicator" data-testid="loading-indicator" style="display:${state.loading ? "block" : "none"}">Loading</div>
    ${state.newKey ? newKeyModal() : ""}
    ${state.confirm ? confirmDialog() : ""}
  `;
  bindShell();
}

function renderLogin() {
  app.innerHTML = `
    <div class="login-page">
      <section class="login-card" data-testid="login-panel">
        <h1>Halyard</h1>
        <p class="subtle">Sign in with an access key.</p>
        <form data-testid="login-form">
          <label>Access key ID
            <input data-testid="login-access-key-id" autocomplete="username" />
          </label>
          <label>Secret access key
            <input data-testid="login-secret-access-key" type="password" autocomplete="current-password" />
          </label>
          <button class="primary" data-testid="login-submit" type="submit">Sign in</button>
          <div class="error" data-testid="login-error"></div>
        </form>
      </section>
      <div id="loading-indicator" data-testid="loading-indicator" style="display:${state.loading ? "block" : "none"}">Loading</div>
    </div>
  `;
  document.querySelector("[data-testid=login-form]").addEventListener("submit", async e => {
    e.preventDefault();
    const error = document.querySelector("[data-testid=login-error]");
    error.textContent = "";
    try {
      state.me = await api("login", {
        method: "POST",
        body: JSON.stringify({
          access_key_id: document.querySelector("[data-testid=login-access-key-id]").value,
          secret_access_key: document.querySelector("[data-testid=login-secret-access-key]").value,
        }),
      });
      state.view = "buckets";
      await loadBuckets();
    } catch {
      error.textContent = "The access key ID or secret is incorrect.";
    }
  });
}

function bucketsView() {
  const rows = state.buckets.map(b => `
    <tr data-testid="bucket-row" data-bucket-name="${h(b.name)}">
      <td>${h(b.name)}</td>
      <td>${h(b.created_at || "")}</td>
    </tr>`).join("");
  return `
    <section class="panel" data-testid="bucket-panel">
      <div class="topline"><h2>Buckets</h2></div>
      <div class="toolbar">
        <input data-testid="new-bucket-name" placeholder="new-bucket-name" />
        <button class="primary" data-testid="new-bucket-btn">Create bucket</button>
      </div>
      <div class="inline-error" data-testid="new-bucket-error"></div>
      <div class="empty" data-testid="bucket-empty-state" style="display:${state.buckets.length ? "none" : "block"}">No buckets yet.</div>
      <table data-testid="bucket-list" style="display:${state.buckets.length ? "table" : "none"}">
        <thead><tr><th>Name</th><th>Created</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </section>
  `;
}

function keysView() {
  const rows = state.keys.map(k => `
    <tr data-testid="access-key-row" data-access-key-id="${h(k.access_key_id)}">
      <td>${h(k.access_key_id)}</td>
      <td>${h(k.created_at || "")}</td>
      <td><button class="danger" data-delete-key="${h(k.access_key_id)}">Delete</button></td>
    </tr>`).join("");
  return `
    <section class="panel" data-testid="access-keys-panel">
      <div class="topline">
        <h2>Access keys</h2>
        <button class="primary" data-testid="new-access-key-btn">New access key</button>
      </div>
      <table data-testid="access-keys-list">
        <thead><tr><th>Access key ID</th><th>Created</th><th></th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </section>
  `;
}

function newKeyModal() {
  return `
    <div class="modal-backdrop" data-testid="new-access-key-modal">
      <div class="modal-card">
        <h2>New access key</h2>
        <div class="secret-box">
          <div class="key-value"><strong>Access key ID</strong><span data-testid="new-access-key-id">${h(state.newKey.access_key_id)}</span></div>
          <div class="key-value"><strong>Secret</strong><span data-testid="new-access-key-secret">${h(state.newKey.secret_access_key)}</span></div>
        </div>
        <button class="primary" data-testid="new-access-key-close">Close</button>
      </div>
    </div>
  `;
}

function confirmDialog() {
  return `
    <div class="dialog-backdrop" data-testid="confirm-dialog">
      <div class="dialog-card">
        <h2 data-testid="confirm-dialog-title">${h(state.confirm.title)}</h2>
        <p data-testid="confirm-dialog-message">${h(state.confirm.message)}</p>
        <div class="dialog-actions">
          <button class="secondary" data-testid="confirm-dialog-cancel">Cancel</button>
          <button class="danger" data-testid="confirm-dialog-confirm">Delete</button>
        </div>
      </div>
    </div>
  `;
}

function bindShell() {
  document.querySelector("[data-testid=nav-buckets]").onclick = async () => { state.view = "buckets"; await loadBuckets(); };
  document.querySelector("[data-testid=nav-access-keys]").onclick = async () => { state.view = "keys"; await loadKeys(); };
  document.querySelector("[data-testid=logout-btn]").onclick = async () => { await api("logout", { method: "POST", body: "{}" }); state.me = null; render(); };
  if (state.view === "buckets") {
    document.querySelector("[data-testid=new-bucket-btn]").onclick = createBucket;
  } else {
    document.querySelector("[data-testid=new-access-key-btn]").onclick = createKey;
    document.querySelectorAll("[data-delete-key]").forEach(btn => {
      btn.onclick = () => {
        const key = btn.getAttribute("data-delete-key");
        state.confirm = { key, title: "Delete access key", message: `Delete access key ${key}?` };
        render();
      };
    });
  }
  const close = document.querySelector("[data-testid=new-access-key-close]");
  if (close) close.onclick = () => { state.newKey = null; render(); };
  const cancel = document.querySelector("[data-testid=confirm-dialog-cancel]");
  if (cancel) cancel.onclick = () => { state.confirm = null; render(); };
  const confirm = document.querySelector("[data-testid=confirm-dialog-confirm]");
  if (confirm) confirm.onclick = deleteConfirmedKey;
}

async function createBucket() {
  const input = document.querySelector("[data-testid=new-bucket-name]");
  const error = document.querySelector("[data-testid=new-bucket-error]");
  error.textContent = "";
  try {
    const data = await api("buckets", { method: "POST", body: JSON.stringify({ name: input.value.trim() }) });
    state.buckets.push({ name: data.bucket.name, created_at: "" });
    state.buckets.sort((a, b) => a.name.localeCompare(b.name));
    input.value = "";
    render();
  } catch (e) {
    error.textContent = e.message || "Could not create bucket.";
  }
}

async function createKey() {
  const data = await api("access-keys", { method: "POST", body: "{}" });
  state.newKey = data.access_key;
  await loadKeys();
  state.newKey = data.access_key;
  render();
}

async function deleteConfirmedKey() {
  const key = state.confirm.key;
  try {
    await api(`access-keys/${encodeURIComponent(key)}`, { method: "DELETE" });
    state.keys = state.keys.filter(k => k.access_key_id !== key);
    state.confirm = null;
    render();
  } catch (e) {
    state.confirm.message = e.code === "CannotDeleteCurrentAccessKey" ? "You cannot delete the access key used by this session." : e.message;
    render();
  }
}

boot();
