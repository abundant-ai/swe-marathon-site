"""Persistence: save/load workbook state to JSON files."""
import json, os, threading
from engine import Workbook, set_cell_input, recompute
from values import Err
from refs import make_ref, parse_ref

_save_lock = threading.RLock()

def _serialize_cell_val(v):
    if v is None: return None
    if isinstance(v, Err): return {"__err": str(v)}
    if isinstance(v, bool): return {"__bool": v}
    if isinstance(v, (int, float)): return v
    if isinstance(v, str): return v
    if isinstance(v, list): return {"__arr": [[_serialize_cell_val(x) for x in r] for r in v]}
    return str(v)

def _deserialize_cell_val(v):
    if isinstance(v, dict):
        if '__err' in v: return Err(v['__err'])
        if '__bool' in v: return v['__bool']
        if '__arr' in v: return [[_deserialize_cell_val(x) for x in r] for r in v['__arr']]
    return v

def save_workbook(data_dir, wb):
    with _save_lock:
        os.makedirs(data_dir, exist_ok=True)
        d = {
            'id': wb.id, 'name': wb.name,
            'sheet_order': wb.sheet_order,
            'sheets': {},
            'names': wb.names,
            'cf_rules': wb.cf_rules,
            'dv_rules': wb.dv_rules,
            'settings': wb.settings,
        }
        for name, sh in wb.sheets.items():
            cells = []
            for (c, r), cell in sh.cells.items():
                cd = {'c': c, 'r': r, 'input': cell.input, 'value': _serialize_cell_val(cell.value), 'kind': cell.kind}
                if cell.format: cd['format'] = cell.format
                if cell.style: cd['style'] = cell.style
                if cell.spill_anchor: cd['spill_anchor'] = list(cell.spill_anchor)
                if cell.spill_ghost_of: cd['spill_ghost_of'] = list(cell.spill_ghost_of)
                cells.append(cd)
            d['sheets'][name] = cells
        path = os.path.join(data_dir, f'wb_{wb.id}.json')
        tmp = path + '.tmp'
        with open(tmp, 'w') as f: json.dump(d, f)
        os.replace(tmp, path)
        meta = os.path.join(data_dir, 'meta.json')
        try:
            with open(meta) as f: m = json.load(f)
        except: m = {'next_id': 1}
        m['next_id'] = max(m.get('next_id', 1), wb.id + 1)
        with open(meta, 'w') as f: json.dump(m, f)

def delete_workbook(data_dir, wid):
    with _save_lock:
        path = os.path.join(data_dir, f'wb_{wid}.json')
        if os.path.exists(path): os.remove(path)

def load_all(data_dir, state):
    if not os.path.isdir(data_dir): return
    meta_path = os.path.join(data_dir, 'meta.json')
    if os.path.exists(meta_path):
        try:
            with open(meta_path) as f: m = json.load(f)
            state.next_id = m.get('next_id', 1)
        except: pass
    for fn in os.listdir(data_dir):
        if not fn.startswith('wb_') or not fn.endswith('.json'): continue
        try:
            with open(os.path.join(data_dir, fn)) as f:
                d = json.load(f)
            wb = Workbook(d['id'], d['name'])
            # remove default sheet
            wb.sheets.clear(); wb.sheet_order.clear()
            for nm in d.get('sheet_order', []):
                wb.add_sheet(nm)
            wb.names = d.get('names', {})
            wb.cf_rules = d.get('cf_rules', [])
            wb.dv_rules = d.get('dv_rules', [])
            wb.settings.update(d.get('settings', {}))
            for nm, cells in d.get('sheets', {}).items():
                if nm not in wb.sheets:
                    wb.add_sheet(nm)
                sh = wb.sheets[nm]
                for cd in cells:
                    cell = sh.get_or_create(cd['c'], cd['r'])
                    cell.input = cd.get('input')
                    cell.value = _deserialize_cell_val(cd.get('value'))
                    cell.kind = cd.get('kind', 'empty')
                    cell.format = cd.get('format')
                    cell.style = cd.get('style')
                    if 'spill_anchor' in cd: cell.spill_anchor = tuple(cd['spill_anchor'])
                    if 'spill_ghost_of' in cd: cell.spill_ghost_of = tuple(cd['spill_ghost_of'])
            # rebuild dep graph + recompute
            from parser import parse_formula
            dirty = set()
            for nm, sh in wb.sheets.items():
                for (c, r), cell in sh.cells.items():
                    if cell.input and isinstance(cell.input, str) and cell.input.startswith('='):
                        try:
                            ast = parse_formula(cell.input)
                            cell.ast = ast
                            precs = set(wb.collect_refs(ast, nm))
                            wb._set_precs((nm, c, r), precs)
                            dirty.add((nm, c, r))
                        except Exception:
                            pass
            recompute(wb, dirty)
            state.workbooks[wb.id] = wb
            if wb.id >= state.next_id: state.next_id = wb.id + 1
        except Exception as e:
            print('load err', fn, e)
