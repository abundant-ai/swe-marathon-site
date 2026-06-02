"""FastAPI app exposing the spreadsheet engine."""
import os, json, asyncio, threading, time, io, csv, re, math
from typing import Optional, Dict, Any, List
from fastapi import FastAPI, Request, HTTPException, UploadFile, File, Form, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, PlainTextResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

import engine
from engine import (Workbook, Sheet, Cell, EvalCtx, evaluate, set_cell_input, recompute,
                    parse_literal, _clear_spill_ghosts, _assign_cell_value)
from parser import parse_formula, ParseError
from refs import parse_ref, parse_range, expand_range, idx_to_col, col_to_idx, make_ref
from values import err, is_err, coerce_num, coerce_str, value_kind, Err
from formatx import apply_format
from locales import translate_formula
import store, xlsxio

DATA_DIR = '/app/data'
os.makedirs(DATA_DIR, exist_ok=True)

app = FastAPI()

class State:
    def __init__(self):
        self.workbooks: Dict[int, Workbook] = {}
        self.next_id = 1
        self.lock = threading.RLock()
        self.collab_clients: Dict[int, list] = {}  # wb_id -> [WSConn]
        self.collab_seq: Dict[int, int] = {}

STATE = State()

def _err_response(status, name, msg=None):
    return JSONResponse(status_code=status, content={"error": name, "message": msg or name})

@app.exception_handler(404)
async def _h404(request, exc):
    if request.url.path.startswith('/api'):
        return JSONResponse(status_code=404, content={"error": "NotFound"})
    return JSONResponse(status_code=404, content={"error": "NotFound"})

@app.get('/api/health')
async def health():
    return {"status": "ok"}

@app.get('/')
async def root():
    p = '/app/static/index.html'
    if os.path.exists(p):
        return HTMLResponse(open(p).read())
    return HTMLResponse('<html><body>tabula</body></html>')

def _wb_summary(wb: Workbook, include_cells=True):
    sheets = []
    for name in wb.sheet_order:
        sh = wb.sheets[name]
        cells = []
        if include_cells:
            for (c, r), cell in sh.cells.items():
                if cell.value is None and cell.input is None and not cell.format and not cell.style: continue
                cells.append(_cell_dict(wb, name, c, r, cell))
        sheets.append({"name": name, "cells": cells})
    return {"id": wb.id, "name": wb.name, "sheets": [s["name"] if False else s for s in sheets]}

def _wb_summary_compact(wb: Workbook):
    return {"id": wb.id, "name": wb.name, "sheets": list(wb.sheet_order)}

def _cell_dict(wb, sheet, c, r, cell):
    ref = make_ref(c, r)
    d = {"ref": ref, "value": _serialize_value(cell.value), "kind": cell.kind}
    if cell.input is not None:
        d["input"] = cell.input
    if cell.format:
        d["format"] = cell.format
    if cell.style:
        d["style"] = cell.style
    if cell.spill_anchor:
        d["spill"] = True
        d["spill_range"] = {"rows": cell.spill_anchor[0], "cols": cell.spill_anchor[1]}
    if cell.spill_ghost_of:
        d["kind"] = "spill"
    # display
    fmt = cell.format
    if fmt:
        try: d["display"] = apply_format(cell.value, fmt)
        except: d["display"] = _default_display(cell.value)
    else:
        d["display"] = _default_display(cell.value)
    # CF
    cf = _cf_for_cell(wb, sheet, c, r, cell.value)
    if cf:
        d["cf"] = cf
    return d

def _default_display(v):
    if v is None: return ""
    if isinstance(v, bool): return "TRUE" if v else "FALSE"
    if is_err(v): return str(v)
    if isinstance(v, float):
        if v.is_integer(): return str(int(v))
        # Excel-style: 15 sig digits, strip trailing zeros
        s = f"{v:.15g}"
        return s
    return str(v)

def _serialize_value(v):
    if v is None: return None
    if isinstance(v, Err): return str(v)
    if isinstance(v, bool): return v
    if isinstance(v, float):
        if v.is_integer(): return int(v)
        return v
    if isinstance(v, list):
        return [[_serialize_value(x) for x in r] for r in v]
    return v

def _cf_for_cell(wb, sheet, c, r, value):
    out = []
    for rule in wb.cf_rules:
        if rule['sheet'] != sheet: continue
        if not _ref_in_range(c, r, rule['range']): continue
        if _cf_match(rule['rule'], value):
            out.append({"style": rule['rule'].get('style', {})})
    return out

def _ref_in_range(c, r, rngstr):
    rng = parse_range(rngstr)
    if not rng: return False
    (c1, r1, _, _), (c2, r2, _, _) = rng
    if c1 > c2: c1, c2 = c2, c1
    if r1 > r2: r1, r2 = r2, r1
    return c1 <= c <= c2 and r1 <= r <= r2

def _cf_match(rule, v):
    if rule.get('kind') != 'cell_value': return False
    op = rule.get('op'); val = rule.get('value')
    if v is None or is_err(v): return False
    if op == 'between':
        lo, hi = rule.get('value', [None, None])
        try: return lo <= v <= hi
        except: return False
    try:
        if op == '=': return v == val
        if op == '<>': return v != val
        if op == '<': return v < val
        if op == '>': return v > val
        if op == '<=': return v <= val
        if op == '>=': return v >= val
    except: return False
    return False

# ---------- workbook listing ----------
@app.get('/api/workbooks')
async def list_workbooks():
    with STATE.lock:
        return {"workbooks": [_wb_summary_compact(w) for w in STATE.workbooks.values()]}

@app.post('/api/workbooks')
async def create_workbook(request: Request):
    body = await request.json()
    name = body.get('name', 'Untitled')
    with STATE.lock:
        wid = STATE.next_id; STATE.next_id += 1
        wb = Workbook(wid, name)
        STATE.workbooks[wid] = wb
        store.save_workbook(DATA_DIR, wb)
    return _wb_summary_compact(wb)

@app.get('/api/workbooks/{wid}')
async def get_workbook(wid: int):
    wb = STATE.workbooks.get(wid)
    if wb is None: return _err_response(404, 'NotFound')
    return _wb_full(wb)

def _wb_full(wb):
    sheets = []
    for name in wb.sheet_order:
        sh = wb.sheets[name]
        cells = []
        for (c, r), cell in sh.cells.items():
            if cell.value is None and cell.input is None and not cell.format and not cell.style and not cell.spill_anchor: continue
            cells.append(_cell_dict(wb, name, c, r, cell))
        sheets.append({"name": name, "cells": cells})
    return {"id": wb.id, "name": wb.name, "sheets": sheets}

@app.delete('/api/workbooks/{wid}')
async def delete_workbook(wid: int):
    with STATE.lock:
        if wid not in STATE.workbooks: return _err_response(404, 'NotFound')
        del STATE.workbooks[wid]
        store.delete_workbook(DATA_DIR, wid)
    return Response(status_code=204)

@app.post('/api/workbooks/{wid}/sheets')
async def add_sheet(wid: int, request: Request):
    wb = STATE.workbooks.get(wid)
    if wb is None: return _err_response(404, 'NotFound')
    body = await request.json()
    name = body.get('name')
    if not name: return _err_response(400, 'ValidationError', 'name required')
    if name in wb.sheets: return _err_response(400, 'ValidationError', 'duplicate sheet')
    wb.add_sheet(name)
    store.save_workbook(DATA_DIR, wb)
    return {"name": name}

@app.delete('/api/workbooks/{wid}/sheets/{name}')
async def del_sheet(wid: int, name: str):
    wb = STATE.workbooks.get(wid)
    if wb is None: return _err_response(404, 'NotFound')
    if name not in wb.sheets: return _err_response(404, 'NotFound')
    wb.remove_sheet(name)
    store.save_workbook(DATA_DIR, wb)
    return Response(status_code=204)

@app.get('/api/workbooks/{wid}/cells/{sheet}/{ref}')
async def get_cell(wid: int, sheet: str, ref: str):
    wb = STATE.workbooks.get(wid)
    if wb is None: return _err_response(404, 'NotFound')
    if sheet not in wb.sheets: return _err_response(404, 'NotFound')
    p = parse_ref(ref)
    if p is None: return _err_response(400, 'ValidationError', 'bad ref')
    c, r, _, _ = p
    cell = wb.sheets[sheet].get(c, r)
    if cell is None:
        return {"ref": ref, "value": None, "kind": "empty", "display": ""}
    return _cell_dict(wb, sheet, c, r, cell)

# ---------- patch ----------
def _check_dv(wb, sheet, c, r, raw_input):
    if raw_input is None or raw_input == '': return None
    if isinstance(raw_input, str) and raw_input.startswith('='): return None  # skip formulas
    val = parse_literal(raw_input)
    for d in wb.dv_rules:
        if d['sheet'] != sheet: continue
        if not _ref_in_range(c, r, d['range']): continue
        rule = d['rule']
        kind = rule.get('kind')
        if kind == 'list':
            vals = rule.get('values', [])
            if val not in vals: return 'ValidationError'
        elif kind == 'integer':
            if not isinstance(val, int) or isinstance(val, bool): return 'ValidationError'
            if not _check_op_range(rule, val): return 'ValidationError'
        elif kind == 'decimal':
            if not isinstance(val, (int, float)) or isinstance(val, bool): return 'ValidationError'
            if not _check_op_range(rule, val): return 'ValidationError'
        elif kind == 'text_length':
            if not isinstance(val, str): return 'ValidationError'
            if not _check_op_range(rule, len(val)): return 'ValidationError'
    return None

def _check_op_range(rule, val):
    op = rule.get('op', 'between')
    if op == 'between':
        return rule.get('min', val) <= val <= rule.get('max', val)
    if op == '=': return val == rule.get('value')
    if op == '<>': return val != rule.get('value')
    if op == '<': return val < rule.get('value')
    if op == '>': return val > rule.get('value')
    if op == '<=': return val <= rule.get('value')
    if op == '>=': return val >= rule.get('value')
    return True

@app.post('/api/workbooks/{wid}/cells')
async def patch_cells(wid: int, request: Request, locale: Optional[str] = Query(None)):
    wb = STATE.workbooks.get(wid)
    if wb is None: return _err_response(404, 'NotFound')
    body = await request.json()
    patches = body.get('patches', [])
    use_locale = locale or wb.settings.get('locale')
    # Validation pass: check sheets, refs, ghosts, DV, AND pre-parse formulas
    for p in patches:
        sheet = p['sheet']
        if sheet not in wb.sheets: return _err_response(404, 'NotFound')
        ref = p['ref']
        rp = parse_ref(ref)
        if rp is None: return _err_response(400, 'ValidationError', 'bad ref')
        c, r, _, _ = rp
        cell = wb.sheets[sheet].get(c, r)
        if cell and cell.spill_ghost_of and p.get('input') is not None:
            return _err_response(400, 'SpillTargetWriteError')
        dv = _check_dv(wb, sheet, c, r, p.get('input'))
        if dv: return _err_response(400, dv)
        # pre-parse
        if 'input' in p:
            inp = p.get('input')
            if isinstance(inp, str) and inp.startswith('='):
                try:
                    parse_formula('=' + translate_formula(inp[1:], use_locale))
                except ParseError as e:
                    return _err_response(400, 'ParseError', str(e))
    dirty = set()
    try:
        for p in patches:
            sheet = p['sheet']
            rp = parse_ref(p['ref']); c, r, _, _ = rp
            if 'input' in p:
                inp = p.get('input')
                if isinstance(inp, str) and inp.startswith('='):
                    inp = '=' + translate_formula(inp[1:], use_locale)
                d = set_cell_input(wb, sheet, c, r, inp)
                dirty.update(d)
            cell = wb.sheets[sheet].get_or_create(c, r)
            if 'format' in p:
                cell.format = p['format']
                dirty.add((sheet, c, r))
            if 'style' in p:
                cell.style = p['style']
                dirty.add((sheet, c, r))
    except ParseError as e:
        return _err_response(400, 'ParseError', str(e))
    changed = recompute(wb, dirty)
    # all changed cells
    out = []
    seen = set()
    for a in changed:
        if a in seen: continue
        seen.add(a)
        sh, cc, rr = a
        cell = wb.sheets[sh].get(cc, rr)
        if cell is None:
            out.append({"ref": make_ref(cc, rr), "value": None, "kind": "empty", "display": ""})
        else:
            out.append(_cell_dict(wb, sh, cc, rr, cell))
    # Re-evaluate CF for cells in CF ranges that share the changed values? For now we recompute on each cell read.
    store.save_workbook(DATA_DIR, wb)
    # Broadcast collab events for each patch
    for p in patches:
        sheet = p['sheet']
        rp = parse_ref(p['ref']); c, r, _, _ = rp
        # leave to /collab path
    return {"cells": out}

# ---------- fill ----------
def _rewrite_formula_ast(ast, dc, dr):
    if ast is None: return ast
    t = ast[0]
    if t in ('num','str','err'): return ast
    if t == 'ref':
        sh, refs = ast[1], ast[2]
        p = parse_ref(refs)
        if not p: return ast
        c, r, ca, ra = p
        if not ca: c += dc
        if not ra: r += dr
        return ('ref', sh, make_ref(c, r, ca, ra))
    if t == 'range':
        sh, refs = ast[1], ast[2]
        rng = parse_range(refs)
        if not rng: return ast
        (c1, r1, ca1, ra1), (c2, r2, ca2, ra2) = rng
        if not ca1: c1 += dc
        if not ra1: r1 += dr
        if not ca2: c2 += dc
        if not ra2: r2 += dr
        return ('range', sh, f"{make_ref(c1,r1,ca1,ra1)}:{make_ref(c2,r2,ca2,ra2)}")
    if t == 'name': return ast
    if t == 'unary': return ('unary', ast[1], _rewrite_formula_ast(ast[2], dc, dr))
    if t == 'binop': return ('binop', ast[1], _rewrite_formula_ast(ast[2], dc, dr), _rewrite_formula_ast(ast[3], dc, dr))
    if t == 'call': return ('call', ast[1], [_rewrite_formula_ast(a, dc, dr) for a in ast[2]])
    if t == 'apply': return ('apply', _rewrite_formula_ast(ast[1], dc, dr), [_rewrite_formula_ast(a, dc, dr) for a in ast[2]])
    if t == 'lambda': return ('lambda', ast[1], _rewrite_formula_ast(ast[2], dc, dr))
    if t == 'let': return ('let', [(n, _rewrite_formula_ast(e, dc, dr)) for n, e in ast[1]], _rewrite_formula_ast(ast[2], dc, dr))
    if t == 'array': return ('array', [[_rewrite_formula_ast(e, dc, dr) for e in row] for row in ast[1]])
    return ast

def _ast_to_text(ast):
    if ast is None: return ''
    t = ast[0]
    if t == 'num':
        v = ast[1]
        if isinstance(v, float) and v.is_integer(): return str(int(v))
        return repr(v) if isinstance(v, float) else str(v)
    if t == 'str': return '"' + str(ast[1]).replace('"','""') + '"'
    if t == 'err': return ast[1]
    if t == 'ref':
        sh = ast[1]
        return (sh + '!' if sh else '') + ast[2]
    if t == 'range':
        sh = ast[1]
        return (sh + '!' if sh else '') + ast[2]
    if t == 'name': return ast[1]
    if t == 'unary':
        if ast[1] == '%': return f"({_ast_to_text(ast[2])})%"
        return f"({ast[1]}{_ast_to_text(ast[2])})"
    if t == 'binop':
        return f"({_ast_to_text(ast[2])}{ast[1]}{_ast_to_text(ast[3])})"
    if t == 'call':
        return f"{ast[1]}({','.join(_ast_to_text(a) for a in ast[2])})"
    if t == 'apply':
        return f"{_ast_to_text(ast[1])}({','.join(_ast_to_text(a) for a in ast[2])})"
    if t == 'lambda':
        return f"LAMBDA({','.join(ast[1])},{_ast_to_text(ast[2])})"
    if t == 'let':
        parts = []
        for n, e in ast[1]: parts.extend([n, _ast_to_text(e)])
        parts.append(_ast_to_text(ast[2]))
        return f"LET({','.join(parts)})"
    if t == 'array':
        return '{' + ';'.join(','.join(_ast_to_text(e) for e in row) for row in ast[1]) + '}'
    return ''

@app.post('/api/workbooks/{wid}/fill')
async def fill(wid: int, request: Request):
    wb = STATE.workbooks.get(wid)
    if wb is None: return _err_response(404, 'NotFound')
    body = await request.json()
    sheet = body['sheet']
    if sheet not in wb.sheets: return _err_response(404, 'NotFound')
    src_ref = body['source']; tgt_range = body['targets']
    sp = parse_ref(src_ref)
    if not sp: return _err_response(400, 'ValidationError')
    sc, sr, _, _ = sp
    sh = wb.sheets[sheet]
    src = sh.get(sc, sr)
    src_input = src.input if src else None
    src_format = src.format if src else None
    src_style = src.style if src else None
    dirty = set()
    for c, r in expand_range(tgt_range):
        dc = c - sc; dr = r - sr
        if src_input is None:
            dirty.update(set_cell_input(wb, sheet, c, r, None))
        elif isinstance(src_input, str) and src_input.startswith('='):
            ast = parse_formula(src_input)
            new_ast = _rewrite_formula_ast(ast, dc, dr)
            new_text = '=' + _ast_to_text(new_ast)
            dirty.update(set_cell_input(wb, sheet, c, r, new_text))
        else:
            dirty.update(set_cell_input(wb, sheet, c, r, src_input))
        cell = sh.get_or_create(c, r)
        if src_format: cell.format = src_format
        if src_style: cell.style = src_style
    changed = recompute(wb, dirty)
    out = []
    for a in changed:
        sh2, cc, rr = a
        cell = wb.sheets[sh2].get(cc, rr)
        if cell: out.append(_cell_dict(wb, sh2, cc, rr, cell))
    store.save_workbook(DATA_DIR, wb)
    return {"cells": out}

# ---------- sort / filter ----------
@app.post('/api/workbooks/{wid}/sort')
async def sort_range(wid: int, request: Request):
    wb = STATE.workbooks.get(wid)
    if wb is None: return _err_response(404, 'NotFound')
    body = await request.json()
    sheet = body['sheet']; rng = body['range']
    if sheet not in wb.sheets: return _err_response(404, 'NotFound')
    by = body.get('by', [])
    rg = parse_range(rng)
    if not rg: return _err_response(400, 'ValidationError')
    (c1, r1, _, _), (c2, r2, _, _) = rg
    if c1 > c2: c1, c2 = c2, c1
    if r1 > r2: r1, r2 = r2, r1
    sh = wb.sheets[sheet]
    rows = []
    for r in range(r1, r2+1):
        row = []
        for c in range(c1, c2+1):
            cell = sh.get(c, r)
            row.append(cell.value if cell else None)
        rows.append((r, row))
    def key(item):
        _, row = item
        out = []
        for spec in by:
            col = spec.get('column', 0)
            v = row[col] if col < len(row) else None
            asc = spec.get('asc', True)
            # None last
            if v is None: out.append((1 if asc else 0, 0))
            elif isinstance(v, (int, float)) and not isinstance(v, bool):
                out.append((0, v if asc else -v))
            else:
                s = str(v)
                # use codepoints to compare; for desc, negate per-char
                if asc:
                    out.append((0, s))
                else:
                    out.append((0, tuple(-ord(ch) for ch in s)))
        return tuple(out)
    rows_sorted = sorted(rows, key=key)
    # write back
    dirty = set()
    for new_r_idx, (_, row) in enumerate(rows_sorted):
        target_r = r1 + new_r_idx
        for ci, val in enumerate(row):
            target_c = c1 + ci
            # Use literal value (not formula re-eval)
            inp = None
            if val is not None:
                if isinstance(val, bool): inp = 'TRUE' if val else 'FALSE'
                elif isinstance(val, float) and val.is_integer(): inp = str(int(val))
                else: inp = str(val) if not isinstance(val, str) else val
            dirty.update(set_cell_input(wb, sheet, target_c, target_r, inp))
    changed = recompute(wb, dirty)
    out = [_cell_dict(wb, a[0], a[1], a[2], wb.sheets[a[0]].get(a[1], a[2])) for a in changed if wb.sheets[a[0]].get(a[1], a[2])]
    store.save_workbook(DATA_DIR, wb)
    return {"cells": out}

@app.post('/api/workbooks/{wid}/filter')
async def filter_range(wid: int, request: Request):
    wb = STATE.workbooks.get(wid)
    if wb is None: return _err_response(404, 'NotFound')
    body = await request.json()
    sheet = body['sheet']; rng = body['range']; crit = body['criteria']
    if sheet not in wb.sheets: return _err_response(404, 'NotFound')
    rg = parse_range(rng)
    if not rg: return _err_response(400, 'ValidationError')
    (c1, r1, _, _), (c2, r2, _, _) = rg
    if c1 > c2: c1, c2 = c2, c1
    if r1 > r2: r1, r2 = r2, r1
    sh = wb.sheets[sheet]
    col_off = crit.get('column', 0)
    expr = crit.get('expr', '')
    out = []
    import functions as F
    for r in range(r1, r2+1):
        row_cells = []
        for c in range(c1, c2+1):
            cell = sh.get(c, r)
            row_cells.append({"ref": make_ref(c, r), "value": _serialize_value(cell.value if cell else None)})
        v = sh.get(c1 + col_off, r)
        vv = v.value if v else None
        if F._criterion_match(expr, vv):
            out.append({"row": r, "cells": row_cells})
    return {"rows": out}

# ---------- CSV ----------
@app.get('/api/workbooks/{wid}/csv')
async def export_csv(wid: int, sheet: str):
    wb = STATE.workbooks.get(wid)
    if wb is None: return _err_response(404, 'NotFound')
    if sheet not in wb.sheets: return _err_response(404, 'NotFound')
    sh = wb.sheets[sheet]
    if not sh.cells:
        return PlainTextResponse('', media_type='text/csv')
    max_c = max(c for (c, r) in sh.cells)
    max_r = max(r for (c, r) in sh.cells)
    buf = io.StringIO()
    w = csv.writer(buf)
    for r in range(max_r + 1):
        row = []
        for c in range(max_c + 1):
            cell = sh.get(c, r)
            v = cell.value if cell else None
            if v is None: row.append('')
            elif isinstance(v, bool): row.append('TRUE' if v else 'FALSE')
            elif isinstance(v, float) and v.is_integer(): row.append(str(int(v)))
            else: row.append(str(v))
        w.writerow(row)
    return PlainTextResponse(buf.getvalue(), media_type='text/csv')

@app.post('/api/workbooks/{wid}/csv')
async def import_csv(wid: int, sheet: str, file: UploadFile = File(...)):
    wb = STATE.workbooks.get(wid)
    if wb is None: return _err_response(404, 'NotFound')
    if sheet not in wb.sheets: return _err_response(404, 'NotFound')
    data = await file.read()
    text = data.decode('utf-8')
    sh = wb.sheets[sheet]
    # clear sheet
    for (c, r) in list(sh.cells.keys()):
        wb._clear_cell_deps((sheet, c, r))
    sh.cells.clear()
    rdr = csv.reader(io.StringIO(text))
    dirty = set()
    for r, row in enumerate(rdr):
        for c, val in enumerate(row):
            if val == '': continue
            try:
                dirty.update(set_cell_input(wb, sheet, c, r, val))
            except ParseError:
                # store as literal text
                dirty.update(set_cell_input(wb, sheet, c, r, val.lstrip('=') if val.startswith('=') else val))
    changed = recompute(wb, dirty)
    store.save_workbook(DATA_DIR, wb)
    return {"ok": True, "changed": len(changed)}

# ---------- XLSX ----------
@app.get('/api/workbooks/{wid}/xlsx')
async def export_xlsx(wid: int):
    wb = STATE.workbooks.get(wid)
    if wb is None: return _err_response(404, 'NotFound')
    blob = xlsxio.export_xlsx(wb)
    return Response(content=blob, media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

@app.post('/api/workbooks/{wid}/xlsx')
async def import_xlsx(wid: int, file: UploadFile = File(...)):
    wb = STATE.workbooks.get(wid)
    if wb is None: return _err_response(404, 'NotFound')
    data = await file.read()
    xlsxio.import_xlsx(wb, data)
    # recompute everything
    dirty = set()
    for sheet, sh in wb.sheets.items():
        for (c, r) in sh.cells:
            dirty.add((sheet, c, r))
    recompute(wb, dirty)
    store.save_workbook(DATA_DIR, wb)
    return {"ok": True}

# ---------- Names ----------
@app.get('/api/workbooks/{wid}/names')
async def list_names(wid: int):
    wb = STATE.workbooks.get(wid)
    if wb is None: return _err_response(404, 'NotFound')
    return {"names": [{"name": n, **wb.names[n]} for n in wb.names]}

@app.post('/api/workbooks/{wid}/names')
async def add_name(wid: int, request: Request):
    wb = STATE.workbooks.get(wid)
    if wb is None: return _err_response(404, 'NotFound')
    body = await request.json()
    name = body['name']
    wb.names[name] = {"scope": body.get('scope', 'workbook'), "sheet": body.get('sheet'), "range": body['range']}
    store.save_workbook(DATA_DIR, wb)
    return {"ok": True}

# ---------- Conditional formats ----------
@app.post('/api/workbooks/{wid}/conditional_formats')
async def add_cf(wid: int, request: Request):
    wb = STATE.workbooks.get(wid)
    if wb is None: return _err_response(404, 'NotFound')
    body = await request.json()
    wb.cf_rules.append({"sheet": body['sheet'], "range": body['range'], "rule": body['rule']})
    store.save_workbook(DATA_DIR, wb)
    return {"ok": True}

@app.get('/api/workbooks/{wid}/conditional_formats')
async def list_cf(wid: int):
    wb = STATE.workbooks.get(wid)
    if wb is None: return _err_response(404, 'NotFound')
    return {"rules": wb.cf_rules}

# ---------- Data validation ----------
@app.post('/api/workbooks/{wid}/data_validation')
async def add_dv(wid: int, request: Request):
    wb = STATE.workbooks.get(wid)
    if wb is None: return _err_response(404, 'NotFound')
    body = await request.json()
    wb.dv_rules.append({"sheet": body['sheet'], "range": body['range'], "rule": body['rule']})
    store.save_workbook(DATA_DIR, wb)
    return {"ok": True}

# ---------- Pivot ----------
@app.post('/api/workbooks/{wid}/pivot')
async def pivot(wid: int, request: Request):
    wb = STATE.workbooks.get(wid)
    if wb is None: return _err_response(404, 'NotFound')
    body = await request.json()
    src = body['source']
    rows_cols = body.get('rows', [])
    cols_cols = body.get('cols', [])
    values = body.get('values', {})
    # source is e.g. 'Sheet1!A1:B5'
    if '!' in src:
        sheet, rng = src.split('!', 1)
    else:
        sheet, rng = wb.sheet_order[0], src
    if sheet not in wb.sheets: return _err_response(404, 'NotFound')
    rg = parse_range(rng)
    if not rg: return _err_response(400, 'ValidationError')
    (c1, r1, _, _), (c2, r2, _, _) = rg
    if c1 > c2: c1, c2 = c2, c1
    if r1 > r2: r1, r2 = r2, r1
    sh = wb.sheets[sheet]
    headers = []
    for c in range(c1, c2+1):
        cell = sh.get(c, r1)
        headers.append(cell.value if cell else None)
    data = []
    for r in range(r1+1, r2+1):
        row = {}
        for i, c in enumerate(range(c1, c2+1)):
            cell = sh.get(c, r)
            row[headers[i]] = cell.value if cell else None
        data.append(row)
    # Group
    from collections import defaultdict
    groups = defaultdict(list)
    row_keys = []; col_keys = []
    for row in data:
        rkey = tuple(row.get(rc) for rc in rows_cols)
        ckey = tuple(row.get(cc) for cc in cols_cols)
        groups[(rkey, ckey)].append(row)
        if rkey not in row_keys: row_keys.append(rkey)
        if ckey not in col_keys: col_keys.append(ckey)
    def _agg(rows, col, agg):
        vals = [r.get(col) for r in rows]
        nums = [v for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if agg == 'sum': return sum(nums)
        if agg == 'avg': return sum(nums)/len(nums) if nums else 0
        if agg == 'count': return len(vals)
        if agg == 'min': return min(nums) if nums else 0
        if agg == 'max': return max(nums) if nums else 0
        return 0
    cells = []
    for rkey in row_keys:
        for ckey in col_keys:
            rows = groups.get((rkey, ckey), [])
            for col, agg in values.items():
                v = _agg(rows, col, agg)
                cells.append({"row": list(rkey), "col": list(ckey), "field": col, "agg": agg, "value": v})
    return {"cells": cells, "rows": [list(r) for r in row_keys], "cols": [list(c) for c in col_keys]}

# ---------- Settings ----------
@app.get('/api/workbooks/{wid}/settings')
async def get_settings(wid: int):
    wb = STATE.workbooks.get(wid)
    if wb is None: return _err_response(404, 'NotFound')
    return wb.settings

@app.put('/api/workbooks/{wid}/settings')
async def put_settings(wid: int, request: Request):
    wb = STATE.workbooks.get(wid)
    if wb is None: return _err_response(404, 'NotFound')
    body = await request.json()
    if 'iterative_calc' in body:
        wb.settings['iterative_calc'].update(body['iterative_calc'])
    if 'locale' in body:
        wb.settings['locale'] = body['locale']
    # recompute everything (cycles may now resolve)
    dirty = set()
    for sheet, sh in wb.sheets.items():
        for (c, r), cell in sh.cells.items():
            if cell.ast: dirty.add((sheet, c, r))
    recompute(wb, dirty)
    store.save_workbook(DATA_DIR, wb)
    return wb.settings

# ---------- Goal seek ----------
@app.post('/api/workbooks/{wid}/goal_seek')
async def goal_seek(wid: int, request: Request):
    wb = STATE.workbooks.get(wid)
    if wb is None: return _err_response(404, 'NotFound')
    body = await request.json()
    target = body['target_cell']; tval = body['target_value']
    chg = body['changing_cell']
    tol = body.get('tol', 1e-5); mx = body.get('max_iter', 100)
    # Save snapshot for restore on failure
    import copy as _cp
    snapshot_cells = {}
    for nm, sh in wb.sheets.items():
        for k, cell in sh.cells.items():
            snapshot_cells[(nm, k[0], k[1])] = (cell.input, cell.value, cell.kind, cell.ast)
    def _restore():
        for (nm, c, r), (i, v, k, a) in snapshot_cells.items():
            cell = wb.sheets[nm].get(c, r)
            if cell:
                cell.input = i; cell.value = v; cell.kind = k; cell.ast = a
    def parse_addr(s):
        if '!' in s: sh, rf = s.split('!', 1)
        else: sh, rf = wb.sheet_order[0], s
        p = parse_ref(rf)
        return sh, p[0], p[1]
    tsh, tc, tr = parse_addr(target)
    csh, cc, cr = parse_addr(chg)
    # Check that target depends (transitively) on changing cell
    def _depends_on(addr, target_addr):
        seen = set()
        stack = [addr]
        while stack:
            a = stack.pop()
            if a == target_addr: return True
            if a in seen: continue
            seen.add(a)
            for d in wb.deps.get(a, ()):
                stack.append(d)
        return False
    if not _depends_on((csh, cc, cr), (tsh, tc, tr)):
        return _err_response(400, 'NotConverged', 'target does not depend on changing cell')
    def f(x):
        # set override and recompute target's transitive
        # compute target value with override
        overrides = {(csh, cc, cr): x}
        # Build full topo list of all cells in workbook (formulas only)
        # then evaluate target through the engine using override
        # Simple approach: temporarily mutate the changing cell and recompute, then restore
        sh = wb.sheets[csh]
        cell = sh.get_or_create(cc, cr)
        old_input = cell.input; old_value = cell.value; old_kind = cell.kind; old_ast = cell.ast
        try:
            set_cell_input(wb, csh, cc, cr, str(x))
            recompute(wb, {(csh, cc, cr)})
            tcell = wb.sheets[tsh].get(tc, tr)
            return (tcell.value if tcell else 0) - tval
        finally:
            # restore (caller will set committed value at end)
            pass
    # bisection-ish hybrid: try secant
    try:
        x0 = wb.sheets[csh].get(cc, cr).value if wb.sheets[csh].get(cc, cr) else 0
        if not isinstance(x0, (int, float)): x0 = 0
        x0 = float(x0)
        x1 = x0 + 1 if x0 == 0 else x0 * 1.1
        f0 = f(x0); f1 = f(x1)
        for it in range(int(mx)):
            if abs(f1) < tol:
                return {"converged": True, "iterations": it, "input": x1, "output": tval + f1}
            if f1 == f0: break
            x2 = x1 - f1 * (x1 - x0) / (f1 - f0)
            x0, f0 = x1, f1
            x1 = x2
            f1 = f(x1)
        if abs(f1) < tol:
            return {"converged": True, "iterations": it+1, "input": x1, "output": tval + f1}
    except Exception as e:
        pass
    _restore()
    return _err_response(400, 'NotConverged')

# ---------- Collaboration WS ----------
class CollabConn:
    def __init__(self, ws, actor):
        self.ws = ws
        self.actor = actor
        self.queue = asyncio.Queue()

@app.websocket('/api/workbooks/{wid}/collab')
async def collab_ws(ws: WebSocket, wid: int):
    await ws.accept()
    wb = STATE.workbooks.get(wid)
    if wb is None:
        await ws.close(); return
    # await hello
    try:
        msg = await ws.receive_json()
    except Exception:
        await ws.close(); return
    if msg.get('type') != 'hello':
        await ws.close(); return
    actor = msg.get('actor', 'anon')
    since = msg.get('since_seq')
    conn = CollabConn(ws, actor)
    STATE.collab_clients.setdefault(wid, []).append(conn)
    # snapshot
    snap = _wb_full(wb)
    await ws.send_json({"type": "welcome", "snapshot": snap, "seq": STATE.collab_seq.get(wid, 0)})
    try:
        while True:
            m = await ws.receive_json()
            t = m.get('type')
            if t == 'op':
                op = m.get('op', {})
                client_seq = m.get('client_seq')
                cells_out = []
                kind = op.get('kind')
                if kind == 'set':
                    sheet = op['sheet']; ref = op['ref']; inp = op.get('input')
                    if sheet in wb.sheets:
                        p = parse_ref(ref)
                        if p:
                            try:
                                dirty = set_cell_input(wb, sheet, p[0], p[1], inp)
                                changed = recompute(wb, dirty)
                                for a in changed:
                                    cell = wb.sheets[a[0]].get(a[1], a[2])
                                    if cell: cells_out.append(_cell_dict(wb, a[0], a[1], a[2], cell))
                                store.save_workbook(DATA_DIR, wb)
                            except ParseError:
                                pass
                elif kind == 'clear':
                    sheet = op['sheet']; ref = op['ref']
                    if sheet in wb.sheets:
                        p = parse_ref(ref)
                        if p:
                            dirty = set_cell_input(wb, sheet, p[0], p[1], None)
                            changed = recompute(wb, dirty)
                            for a in changed:
                                cell = wb.sheets[a[0]].get(a[1], a[2])
                                if cell:
                                    cells_out.append(_cell_dict(wb, a[0], a[1], a[2], cell))
                                else:
                                    cells_out.append({"ref": make_ref(a[1], a[2]), "value": None, "kind": "empty", "display": ""})
                            store.save_workbook(DATA_DIR, wb)
                elif kind == 'add_sheet':
                    name = op.get('name')
                    if name and name not in wb.sheets:
                        wb.add_sheet(name); store.save_workbook(DATA_DIR, wb)
                elif kind == 'remove_sheet':
                    name = op.get('name')
                    if name in wb.sheets:
                        wb.remove_sheet(name); store.save_workbook(DATA_DIR, wb)
                STATE.collab_seq[wid] = STATE.collab_seq.get(wid, 0) + 1
                seq = STATE.collab_seq[wid]
                await ws.send_json({"type": "ack", "client_seq": client_seq, "seq": seq})
                ev = {"type": "event", "seq": seq, "actor": actor, "client_seq": client_seq, "op": op, "cells": cells_out}
                for c in STATE.collab_clients.get(wid, []):
                    try: await c.ws.send_json(ev)
                    except: pass
            elif t == 'presence':
                ev = {"type": "presence_event", "actor": actor, "sheet": m.get('sheet'), "ref": m.get('ref'), "online": True}
                for c in STATE.collab_clients.get(wid, []):
                    if c is conn: continue
                    try: await c.ws.send_json(ev)
                    except: pass
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        try:
            STATE.collab_clients.get(wid, []).remove(conn)
        except: pass
        # broadcast offline
        ev = {"type": "presence_event", "actor": actor, "sheet": None, "ref": None, "online": False}
        for c in STATE.collab_clients.get(wid, []):
            try: await c.ws.send_json(ev)
            except: pass

# ---------- Startup ----------
@app.on_event('startup')
def startup():
    store.load_all(DATA_DIR, STATE)
