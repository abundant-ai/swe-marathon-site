// Tabula vanilla SPA.
const ROWS = 40, COLS = 12;
let state = { wbId: null, sheet: null, active: 'A1', cells: {}, sheets: [] };

function colLetters(i) {
  let n = i + 1, s = '';
  while (n > 0) { n--; s = String.fromCharCode(65 + (n % 26)) + s; n = Math.floor(n / 26); }
  return s;
}

async function api(method, url, body, raw) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(url, opts);
  if (raw) return r;
  if (!r.ok) throw new Error('HTTP ' + r.status);
  return r.json();
}

async function loadWorkbookList() {
  const { workbooks } = await api('GET', '/api/workbooks');
  const ul = document.getElementById('workbook-list');
  ul.innerHTML = '';
  for (const wb of workbooks) {
    const li = document.createElement('li');
    li.textContent = wb.name + ' (#' + wb.id + ')';
    li.dataset.testid = 'workbook-row-' + wb.id;
    li.onclick = () => openWorkbook(wb.id);
    if (state.wbId === wb.id) li.classList.add('active');
    ul.appendChild(li);
  }
}

async function openWorkbook(id) {
  state.wbId = id;
  const wb = await api('GET', '/api/workbooks/' + id);
  state.sheets = wb.sheets;
  state.sheet = wb.sheets[0]?.name || 'Sheet1';
  state.cells = {};
  for (const s of wb.sheets) {
    state.cells[s.name] = {};
    for (const c of (s.cells || [])) {
      state.cells[s.name][c.ref] = c;
    }
  }
  renderTabs();
  renderGrid();
  await loadWorkbookList();
}

function renderTabs() {
  const tabs = document.getElementById('tabs');
  tabs.innerHTML = '';
  for (const s of state.sheets) {
    const b = document.createElement('button');
    b.textContent = s.name;
    b.dataset.testid = 'sheet-tab-' + s.name;
    if (s.name === state.sheet) b.classList.add('active');
    b.onclick = () => { state.sheet = s.name; renderGrid(); };
    tabs.appendChild(b);
  }
  const add = document.createElement('button');
  add.textContent = '+';
  add.className = 'secondary';
  add.onclick = async () => {
    const name = prompt('Sheet name?', 'Sheet' + (state.sheets.length + 1));
    if (!name) return;
    await api('POST', '/api/workbooks/' + state.wbId + '/sheets', { name });
    await openWorkbook(state.wbId);
  };
  tabs.appendChild(add);
}

function renderGrid() {
  const wrap = document.getElementById('grid-wrap');
  wrap.innerHTML = '';
  const tbl = document.createElement('table');
  tbl.className = 'grid';
  const headRow = document.createElement('tr');
  headRow.appendChild(document.createElement('th'));
  for (let c = 0; c < COLS; c++) {
    const th = document.createElement('th');
    th.textContent = colLetters(c);
    headRow.appendChild(th);
  }
  tbl.appendChild(headRow);
  for (let r = 0; r < ROWS; r++) {
    const tr = document.createElement('tr');
    const th = document.createElement('th');
    th.textContent = r + 1;
    tr.appendChild(th);
    for (let c = 0; c < COLS; c++) {
      const ref = colLetters(c) + (r + 1);
      const td = document.createElement('td');
      td.dataset.testid = 'cell-' + ref;
      td.dataset.ref = ref;
      const cell = state.cells[state.sheet]?.[ref];
      if (cell) {
        td.textContent = cell.display ?? cell.value ?? '';
        if (cell.kind === 'error') td.classList.add('error');
        else if (cell.kind === 'spill') td.classList.add('spill');
      }
      td.onclick = () => selectCell(ref);
      td.ondblclick = () => beginEdit(ref);
      if (ref === state.active) td.classList.add('active');
      tr.appendChild(td);
    }
    tbl.appendChild(tr);
  }
  wrap.appendChild(tbl);
  selectCell(state.active);
}

function selectCell(ref) {
  state.active = ref;
  document.querySelectorAll('table.grid td.active').forEach(t => t.classList.remove('active'));
  const td = document.querySelector(`[data-testid="cell-${ref}"]`);
  if (td) td.classList.add('active');
  document.getElementById('cell-ref').value = ref;
  const cell = state.cells[state.sheet]?.[ref];
  document.getElementById('formula-bar').value = cell?.input ?? '';
}

function beginEdit(ref) {
  selectCell(ref);
  document.getElementById('formula-bar').focus();
}

document.getElementById('formula-bar').addEventListener('keydown', async (e) => {
  if (e.key === 'Enter') {
    const ref = state.active;
    const value = e.target.value;
    await api('POST', '/api/workbooks/' + state.wbId + '/cells', {
      patches: [{ sheet: state.sheet, ref, input: value === '' ? null : value }]
    });
    const wb = await api('GET', '/api/workbooks/' + state.wbId);
    state.cells = {};
    for (const s of wb.sheets) {
      state.cells[s.name] = {};
      for (const c of (s.cells || [])) state.cells[s.name][c.ref] = c;
    }
    renderGrid();
  }
});

document.getElementById('new-workbook-btn').onclick = async () => {
  const name = prompt('Workbook name?', 'Untitled');
  if (!name) return;
  const wb = await api('POST', '/api/workbooks', { name });
  await openWorkbook(wb.id);
};
document.getElementById('save-btn').onclick = async () => {
  // Saves are implicit but show a status flicker.
  document.getElementById('status').textContent = 'saved';
  setTimeout(() => document.getElementById('status').textContent = '', 1000);
};

(async () => {
  const list = await api('GET', '/api/workbooks');
  if (list.workbooks.length === 0) {
    const wb = await api('POST', '/api/workbooks', { name: 'Untitled' });
    await openWorkbook(wb.id);
  } else {
    await openWorkbook(list.workbooks[0].id);
  }
})();
