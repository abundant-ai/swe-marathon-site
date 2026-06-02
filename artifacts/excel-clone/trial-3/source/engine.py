"""Workbook engine: cells, dependency graph, evaluator."""
import math, re, threading, time
from collections import defaultdict
from refs import parse_ref, parse_range, expand_range, idx_to_col, col_to_idx, make_ref
from values import err, is_err, is_num, coerce_num, coerce_str, coerce_bool, value_kind, Err
from parser import parse_formula, ParseError
import functions as F
from formatx import apply_format

LambdaVal = F._LambdaVal

class Cell:
    __slots__ = ('input', 'value', 'kind', 'ast', 'format', 'style', 'spill_anchor', 'spill_ghost_of')
    def __init__(self):
        self.input = None
        self.value = None
        self.kind = 'empty'
        self.ast = None
        self.format = None
        self.style = None
        self.spill_anchor = None  # if this cell is anchor with spill, store (rows, cols)
        self.spill_ghost_of = None  # (col, row) of anchor if this is a ghost

class Sheet:
    def __init__(self, name):
        self.name = name
        self.cells = {}  # (col, row) -> Cell

    def get(self, c, r):
        return self.cells.get((c, r))

    def get_or_create(self, c, r):
        k = (c, r)
        if k not in self.cells:
            self.cells[k] = Cell()
        return self.cells[k]

class Workbook:
    def __init__(self, id_, name):
        self.id = id_
        self.name = name
        self.sheets = {}  # name -> Sheet
        self.sheet_order = []
        self.deps = defaultdict(set)  # cell_addr -> set(addr) cells that depend on it
        self.precs = defaultdict(set)  # cell_addr -> set(addr) cells it depends on
        self.names = {}  # name -> {scope, sheet?, range}
        self.cf_rules = []  # [{sheet, range, rule}]
        self.dv_rules = []  # [{sheet, range, rule}]
        self.settings = {'iterative_calc': {'enabled': False, 'max_iterations': 100, 'max_change': 0.001}, 'locale': 'en-US'}
        self.lock = threading.RLock()
        self.add_sheet('Sheet1')

    def add_sheet(self, name):
        if name in self.sheets: return False
        self.sheets[name] = Sheet(name)
        self.sheet_order.append(name)
        return True

    def remove_sheet(self, name):
        if name not in self.sheets: return False
        # remove all cells
        for (c, r) in list(self.sheets[name].cells.keys()):
            self._clear_cell_deps((name, c, r))
        del self.sheets[name]
        self.sheet_order.remove(name)
        return True

    def _addr(self, sheet, c, r):
        return (sheet, c, r)

    def _clear_cell_deps(self, addr):
        for p in list(self.precs.get(addr, ())):
            self.deps[p].discard(addr)
        if addr in self.precs: del self.precs[addr]

    def _set_precs(self, addr, prec_addrs):
        self._clear_cell_deps(addr)
        for p in prec_addrs:
            self.deps[p].add(addr)
            self.precs[addr].add(p)

    # ---- formula utilities ----
    def collect_refs(self, ast, sheet):
        """Yield (sheet, col, row) precedent addresses for an AST."""
        if ast is None: return
        t = ast[0]
        if t == 'ref':
            sh = ast[1] or sheet
            p = parse_ref(ast[2])
            if p:
                c, r, _, _ = p
                yield (sh, c, r)
            else:
                # may be a defined name
                if ast[2] in self.names:
                    nr = self.names[ast[2]]['range']
                    yield from self._refs_in_range(nr, sheet)
        elif t == 'range':
            sh = ast[1] or sheet
            for c, r in expand_range(ast[2]):
                yield (sh, c, r)
        elif t == 'name':
            nm = ast[1]
            if '!' in nm:
                shn, ln = nm.split('!', 1)
                if ln in self.names:
                    yield from self._refs_in_range(self.names[ln]['range'], shn)
            elif nm in self.names:
                yield from self._refs_in_range(self.names[nm]['range'], sheet)
        elif t == 'call':
            for a in ast[2]: yield from self.collect_refs(a, sheet)
        elif t == 'apply':
            yield from self.collect_refs(ast[1], sheet)
            for a in ast[2]: yield from self.collect_refs(a, sheet)
        elif t == 'unary':
            yield from self.collect_refs(ast[2], sheet)
        elif t == 'binop':
            yield from self.collect_refs(ast[2], sheet)
            yield from self.collect_refs(ast[3], sheet)
        elif t == 'let':
            for nm, e in ast[1]: yield from self.collect_refs(e, sheet)
            yield from self.collect_refs(ast[2], sheet)
        elif t == 'lambda':
            yield from self.collect_refs(ast[2], sheet)
        elif t == 'array':
            for row in ast[1]:
                for e in row: yield from self.collect_refs(e, sheet)

    def _refs_in_range(self, rngstr, default_sheet):
        # rngstr may be 'Sheet!A1:B2' or 'A1:B2' or 'A1'
        if '!' in rngstr:
            shn, rest = rngstr.split('!', 1)
            shn = shn.strip("'")
        else:
            shn = default_sheet; rest = rngstr
        for c, r in expand_range(rest):
            yield (shn, c, r)


# ---------- Evaluator ----------
class EvalCtx:
    def __init__(self, wb, sheet, col, row, env=None, calc_overrides=None):
        self.wb = wb
        self.sheet = sheet
        self.col = col
        self.row = row
        self.env = env or {}
        self.calc_overrides = calc_overrides or {}
    def child(self, **kw):
        new_env = dict(self.env)
        if 'add_env' in kw: new_env.update(kw.pop('add_env'))
        return EvalCtx(self.wb, kw.get('sheet', self.sheet), kw.get('col', self.col), kw.get('row', self.row), new_env, self.calc_overrides)
    def apply_lambda(self, lam, args):
        env = dict(lam.env)
        for i, p in enumerate(lam.params):
            env[p] = args[i] if i < len(args) else None
        sub = EvalCtx(self.wb, self.sheet, self.col, self.row, env, self.calc_overrides)
        return evaluate(sub, lam.body)

def cell_value(wb, sheet, col, row, ctx=None):
    if ctx and (sheet, col, row) in ctx.calc_overrides:
        return ctx.calc_overrides[(sheet, col, row)]
    sh = wb.sheets.get(sheet)
    if not sh: return None
    cell = sh.get(col, row)
    if cell is None: return None
    return cell.value

def evaluate(ctx, ast):
    if ast is None: return None
    t = ast[0]
    if t == 'num': return ast[1]
    if t == 'str': return ast[1]
    if t == 'err': return Err(ast[1])
    if t == 'ref':
        sh = ast[1] or ctx.sheet
        if sh not in ctx.wb.sheets: return Err('#REF!')
        p = parse_ref(ast[2])
        if p is None:
            if ast[2] in ctx.env: return ctx.env[ast[2]]
            if ast[2] in ctx.wb.names: return _eval_name(ctx, ast[2])
            return Err('#NAME?')
        c, r, _, _ = p
        return cell_value(ctx.wb, sh, c, r, ctx)
    if t == 'range':
        sh = ast[1] or ctx.sheet
        if sh not in ctx.wb.sheets: return Err('#REF!')
        rng = parse_range(ast[2])
        if not rng: return Err('#REF!')
        (c1, r1, _, _), (c2, r2, _, _) = rng
        if c1 > c2: c1, c2 = c2, c1
        if r1 > r2: r1, r2 = r2, r1
        out = []
        for r in range(r1, r2 + 1):
            row = []
            for c in range(c1, c2 + 1):
                row.append(cell_value(ctx.wb, sh, c, r, ctx))
            out.append(row)
        return out
    if t == 'name':
        nm = ast[1]
        if nm in ctx.env: return ctx.env[nm]
        return _eval_name(ctx, nm)
    if t == 'unary':
        op, e = ast[1], ast[2]
        v = evaluate(ctx, e)
        if is_err(v): return v
        if op == '-': n = coerce_num(v); return n if is_err(n) else -n
        if op == '+': return coerce_num(v)
        if op == '%':
            n = coerce_num(v)
            if is_err(n): return n
            return n / 100
        return v
    if t == 'binop':
        a = evaluate(ctx, ast[2]); b = evaluate(ctx, ast[3])
        return _binop(ast[1], a, b)
    if t == 'call':
        return _call(ctx, ast[1], ast[2])
    if t == 'apply':
        callable_v = evaluate(ctx, ast[1])
        args = [evaluate(ctx, a) for a in ast[2]]
        if isinstance(callable_v, LambdaVal):
            return ctx.apply_lambda(callable_v, args)
        return Err('#VALUE!')
    if t == 'lambda':
        return LambdaVal(ast[1], ast[2], dict(ctx.env))
    if t == 'let':
        env_add = {}
        for nm, e in ast[1]:
            sub = ctx.child(add_env=env_add)
            v = evaluate(sub, e)
            env_add[nm] = v
        sub = ctx.child(add_env=env_add)
        return evaluate(sub, ast[2])
    if t == 'array':
        return [[evaluate(ctx, e) for e in row] for row in ast[1]]
    return Err('#VALUE!')

def _eval_name(ctx, nm):
    up = nm.upper() if isinstance(nm, str) else nm
    if up == 'TRUE': return True
    if up == 'FALSE': return False
    if '!' in nm:
        shn, ln = nm.split('!', 1)
        if ln in ctx.wb.names:
            return _eval_named_range(ctx, ctx.wb.names[ln]['range'], shn)
        return Err('#NAME?')
    if nm in ctx.wb.names:
        return _eval_named_range(ctx, ctx.wb.names[nm]['range'], ctx.sheet)
    return Err('#NAME?')

def _eval_named_range(ctx, rngstr, default_sheet):
    if '!' in rngstr:
        shn, rest = rngstr.split('!', 1)
        shn = shn.strip("'")
    else:
        shn = default_sheet; rest = rngstr
    if shn not in ctx.wb.sheets: return Err('#REF!')
    rng = parse_range(rest)
    if not rng: return Err('#REF!')
    (c1, r1, _, _), (c2, r2, _, _) = rng
    if c1 == c2 and r1 == r2:
        return cell_value(ctx.wb, shn, c1, r1, ctx)
    if c1 > c2: c1, c2 = c2, c1
    if r1 > r2: r1, r2 = r2, r1
    out = []
    for r in range(r1, r2 + 1):
        row = []
        for c in range(c1, c2 + 1):
            row.append(cell_value(ctx.wb, shn, c, r, ctx))
        out.append(row)
    return out

def _binop(op, a, b):
    # ranges/arrays may be 1x1 -> scalarise
    if isinstance(a, list):
        if len(a) == 1 and len(a[0]) == 1: a = a[0][0]
    if isinstance(b, list):
        if len(b) == 1 and len(b[0]) == 1: b = b[0][0]
    # array broadcast for comparisons (used in FILTER)
    if isinstance(a, list) or isinstance(b, list):
        return _array_binop(op, a, b)
    if is_err(a): return a
    if is_err(b): return b
    if op == '&':
        return coerce_str(a) + coerce_str(b)
    if op in ('=', '<>', '<', '>', '<=', '>='):
        return _compare(op, a, b)
    na = coerce_num(a); nb = coerce_num(b)
    if is_err(na): return na
    if is_err(nb): return nb
    if op == '+': return na + nb
    if op == '-': return na - nb
    if op == '*': return na * nb
    if op == '/':
        if nb == 0: return Err('#DIV/0!')
        r = na / nb
        if isinstance(r, float) and r.is_integer(): return int(r)
        return r
    if op == '^':
        try:
            r = math.pow(na, nb)
            if math.isnan(r) or math.isinf(r): return Err('#NUM!')
            return r
        except: return Err('#NUM!')
    return Err('#VALUE!')

def _array_binop(op, a, b):
    def shape(x):
        if not isinstance(x, list): return (1, 1)
        if not x: return (0, 0)
        if not isinstance(x[0], list): return (1, len(x))
        return (len(x), len(x[0]))
    def get(x, r, c):
        if not isinstance(x, list): return x
        if not x: return None
        if not isinstance(x[0], list):
            return x[c] if c < len(x) else None
        return x[r][c] if r < len(x) and c < len(x[r]) else None
    sa = shape(a); sb = shape(b)
    rows = max(sa[0], sb[0]); cols = max(sa[1], sb[1])
    out = []
    for r in range(rows):
        row = []
        for c in range(cols):
            row.append(_binop(op, get(a, r if sa[0]>1 else 0, c if sa[1]>1 else 0),
                              get(b, r if sb[0]>1 else 0, c if sb[1]>1 else 0)))
        out.append(row)
    return out

def _compare(op, a, b):
    if a is None: a = 0
    if b is None: b = 0
    if isinstance(a, str) and isinstance(b, str):
        ax, bx = a.lower(), b.lower()
    elif type(a) == type(b):
        ax, bx = a, b
    else:
        if isinstance(a, (int, float)) and not isinstance(a, bool) and isinstance(b, (int, float)) and not isinstance(b, bool):
            ax, bx = a, b
        else:
            rank = {bool: 3, str: 2}
            ra = rank.get(type(a), 1); rb = rank.get(type(b), 1)
            if ra != rb:
                if op == '=': return False
                if op == '<>': return True
                return (ra < rb) if op in ('<', '<=') else (ra > rb)
            ax, bx = a, b
    if op == '=': return ax == bx
    if op == '<>': return ax != bx
    try:
        if op == '<': return ax < bx
        if op == '>': return ax > bx
        if op == '<=': return ax <= bx
        if op == '>=': return ax >= bx
    except TypeError:
        return Err('#VALUE!')
    return False

def _call(ctx, name, args_ast):
    name = name.upper()
    if name == 'INDIRECT':
        if not args_ast: return Err('#REF!')
        s = evaluate(ctx, args_ast[0])
        if is_err(s): return s
        return _eval_indirect(ctx, coerce_str(s))
    if name == 'OFFSET':
        return _eval_offset(ctx, args_ast)
    if name == 'ROW':
        if args_ast:
            v = args_ast[0]
            if v[0] == 'ref':
                p = parse_ref(v[2])
                if p: return p[1] + 1
            if v[0] == 'range':
                rng = parse_range(v[2])
                if rng: return rng[0][1] + 1
        return ctx.row + 1
    if name == 'COLUMN':
        if args_ast:
            v = args_ast[0]
            if v[0] == 'ref':
                p = parse_ref(v[2])
                if p: return p[0] + 1
            if v[0] == 'range':
                rng = parse_range(v[2])
                if rng: return rng[0][0] + 1
        return ctx.col + 1
    fn = F.FUNCS.get(name)
    if fn is None:
        # maybe a bound lambda in env (LET-defined function)
        if name in ctx.env and isinstance(ctx.env[name], LambdaVal):
            args = [evaluate(ctx, a) for a in args_ast]
            return ctx.apply_lambda(ctx.env[name], args)
        # case-insensitive env lookup
        for k, v in ctx.env.items():
            if k.upper() == name and isinstance(v, LambdaVal):
                args = [evaluate(ctx, a) for a in args_ast]
                return ctx.apply_lambda(v, args)
        return Err('#NAME?')
    args = [evaluate(ctx, a) for a in args_ast]
    try:
        return fn(ctx, args)
    except Exception:
        return Err('#VALUE!')

def _eval_indirect(ctx, s):
    sheet = ctx.sheet
    rest = s
    if '!' in s:
        sheet, rest = s.split('!', 1)
        sheet = sheet.strip("'")
    if sheet not in ctx.wb.sheets: return Err('#REF!')
    if ':' in rest:
        rng = parse_range(rest)
        if not rng: return Err('#REF!')
        (c1, r1, _, _), (c2, r2, _, _) = rng
        if c1 > c2: c1, c2 = c2, c1
        if r1 > r2: r1, r2 = r2, r1
        return [[cell_value(ctx.wb, sheet, c, r, ctx) for c in range(c1, c2+1)] for r in range(r1, r2+1)]
    p = parse_ref(rest)
    if not p: return Err('#REF!')
    return cell_value(ctx.wb, sheet, p[0], p[1], ctx)

def _eval_offset(ctx, args_ast):
    if len(args_ast) < 3: return Err('#VALUE!')
    base = args_ast[0]
    if base[0] == 'ref':
        sh = base[1] or ctx.sheet
        p = parse_ref(base[2])
        if not p: return Err('#REF!')
        bc, br = p[0], p[1]
    elif base[0] == 'range':
        sh = base[1] or ctx.sheet
        rng = parse_range(base[2])
        if not rng: return Err('#REF!')
        bc, br = rng[0][0], rng[0][1]
    else:
        return Err('#VALUE!')
    rows = coerce_num(evaluate(ctx, args_ast[1]))
    cols = coerce_num(evaluate(ctx, args_ast[2]))
    h = coerce_num(evaluate(ctx, args_ast[3])) if len(args_ast) > 3 else 1
    w = coerce_num(evaluate(ctx, args_ast[4])) if len(args_ast) > 4 else 1
    if any(is_err(x) for x in (rows, cols, h, w)): return Err('#VALUE!')
    sr = br + int(rows); sc = bc + int(cols)
    er = sr + int(h) - 1; ec = sc + int(w) - 1
    if sr < 0 or sc < 0: return Err('#REF!')
    if h == 1 and w == 1:
        return cell_value(ctx.wb, sh, sc, sr, ctx)
    out = []
    for r in range(sr, er+1):
        row = []
        for c in range(sc, ec+1):
            row.append(cell_value(ctx.wb, sh, c, r, ctx))
        out.append(row)
    return out

# ---------- Recompute / set_cell ----------
def parse_literal(text):
    """Parse a non-formula literal string into a value (number, bool, or string)."""
    if text is None: return None
    if text == '': return None
    s = text.strip()
    if s.upper() == 'TRUE': return True
    if s.upper() == 'FALSE': return False
    # number
    try:
        if re.fullmatch(r'-?\d+', s): return int(s)
        if re.fullmatch(r'-?\d*\.\d+(?:[eE][+-]?\d+)?|-?\d+(?:[eE][+-]?\d+)?', s):
            f = float(s)
            return int(f) if f.is_integer() else f
    except ValueError:
        pass
    # percent literal? "50%" -> 0.5
    if s.endswith('%'):
        try:
            f = float(s[:-1])
            return f / 100
        except ValueError:
            pass
    return text

def set_cell_input(wb, sheet, c, r, raw_input):
    """Stage a cell input. Does NOT recompute. Returns set of dirty addresses."""
    sh = wb.sheets[sheet]
    addr = (sheet, c, r)
    cell = sh.get_or_create(c, r)
    # If this cell was a spill ghost, that means anchor needs to know we're overwriting.
    # caller (apply_patch) should check before calling.
    # If cell was a spill anchor, clear ghosts.
    if cell.spill_anchor:
        _clear_spill_ghosts(wb, sheet, c, r, cell)
    cell.spill_anchor = None
    cell.spill_ghost_of = None
    cell.input = raw_input
    cell.ast = None
    if raw_input is None or raw_input == '':
        # clear cell entirely
        wb._clear_cell_deps(addr)
        cell.value = None
        cell.kind = 'empty'
        # leave format/style alone? Excel keeps style, but let's keep it.
        return {addr}
    if isinstance(raw_input, str) and raw_input.startswith('='):
        try:
            ast = parse_formula(raw_input)
        except ParseError as e:
            raise
        cell.ast = ast
        precs = set(wb.collect_refs(ast, sheet))
        wb._set_precs(addr, precs)
        cell.value = None
        cell.kind = 'empty'
        return {addr}
    # literal
    wb._clear_cell_deps(addr)
    v = parse_literal(raw_input)
    cell.value = v
    cell.kind = value_kind(v)
    return {addr}

def _clear_spill_ghosts(wb, sheet, c, r, cell):
    if not cell.spill_anchor: return
    rows, cols = cell.spill_anchor
    sh = wb.sheets[sheet]
    dirty = set()
    for rr in range(r, r + rows):
        for cc in range(c, c + cols):
            if rr == r and cc == c: continue
            g = sh.get(cc, rr)
            if g and g.spill_ghost_of == (c, r):
                g.spill_ghost_of = None
                g.value = None
                g.kind = 'empty'
                g.input = None
                dirty.add((sheet, cc, rr))
    cell.spill_anchor = None
    return dirty

def recompute(wb, dirty_addrs, max_iter=1):
    """Recompute the transitive closure of dirty cells in topological order.
    Detects cycles -> sets #CIRC! unless iterative_calc enabled.
    Returns set of all changed addresses.
    """
    # Build the set of cells to recompute
    visited = set()
    queue = list(dirty_addrs)
    while queue:
        a = queue.pop()
        if a in visited: continue
        visited.add(a)
        for d in wb.deps.get(a, ()):
            if d not in visited:
                queue.append(d)
    # Filter to formulas only (literals don't need recompute)
    to_compute = []
    for a in visited:
        sh, c, r = a
        s = wb.sheets.get(sh)
        if not s: continue
        cell = s.get(c, r)
        if cell and cell.ast is not None:
            to_compute.append(a)
        elif cell and cell.spill_ghost_of:
            # ghosts get recomputed when anchor recomputes
            pass
    # topo sort using Kahn's algorithm restricted to to_compute
    in_set = set(to_compute)
    indeg = {a: 0 for a in to_compute}
    succ = {a: [] for a in to_compute}
    for a in to_compute:
        for p in wb.precs.get(a, ()):
            if p in in_set:
                indeg[a] += 1
                succ[p].append(a)
    ready = [a for a, d in indeg.items() if d == 0]
    order = []
    while ready:
        a = ready.pop()
        order.append(a)
        for s in succ[a]:
            indeg[s] -= 1
            if indeg[s] == 0: ready.append(s)
    cycle = [a for a in to_compute if a not in order]
    iter_cfg = wb.settings.get('iterative_calc', {})
    iter_enabled = iter_cfg.get('enabled', False)
    changed = set(dirty_addrs)
    # Compute the topo-ordered cells
    for a in order:
        sh, c, r = a
        cell = wb.sheets[sh].get(c, r)
        if cell is None or cell.ast is None: continue
        ctx = EvalCtx(wb, sh, c, r)
        v = evaluate(ctx, cell.ast)
        _assign_cell_value(wb, a, cell, v, changed)
    # Handle cycles
    if cycle:
        if iter_enabled:
            max_it = int(iter_cfg.get('max_iterations', 100))
            tol = float(iter_cfg.get('max_change', 0.001))
            # initialize cycle cells to 0 if no value
            for a in cycle:
                sh, c, r = a
                cell = wb.sheets[sh].get(c, r)
                if cell.value is None or is_err(cell.value):
                    cell.value = 0; cell.kind = 'number'
            for it in range(max_it):
                max_change = 0
                for a in cycle:
                    sh, c, r = a
                    cell = wb.sheets[sh].get(c, r)
                    if cell is None or cell.ast is None: continue
                    ctx = EvalCtx(wb, sh, c, r)
                    new_v = evaluate(ctx, cell.ast)
                    old_v = cell.value
                    if isinstance(new_v, (int, float)) and isinstance(old_v, (int, float)):
                        max_change = max(max_change, abs(new_v - old_v))
                    else:
                        max_change = float('inf')
                    cell.value = new_v
                    cell.kind = value_kind(new_v)
                    changed.add(a)
                if max_change <= tol: break
        else:
            for a in cycle:
                sh, c, r = a
                cell = wb.sheets[sh].get(c, r)
                if cell is None: continue
                cell.value = Err('#CIRC!')
                cell.kind = 'error'
                changed.add(a)
    return changed

def _assign_cell_value(wb, addr, cell, v, changed):
    sheet, c, r = addr
    sh = wb.sheets[sheet]
    # If previous spill anchor, first clear ghosts
    if cell.spill_anchor:
        for rr in range(r, r + cell.spill_anchor[0]):
            for cc in range(c, c + cell.spill_anchor[1]):
                if (cc, rr) == (c, r): continue
                g = sh.get(cc, rr)
                if g and g.spill_ghost_of == (c, r):
                    g.spill_ghost_of = None
                    g.value = None
                    g.kind = 'empty'
                    g.input = None
                    changed.add((sheet, cc, rr))
        cell.spill_anchor = None
    if isinstance(v, list) and v and isinstance(v[0], list):
        rows = len(v); cols = max(len(row) for row in v) if v else 0
        # check if the spill area collides with non-empty / non-ghost cells
        blocked = False
        for rr in range(r, r + rows):
            for cc in range(c, c + cols):
                if (cc, rr) == (c, r): continue
                tgt = sh.get(cc, rr)
                if tgt is None: continue
                if tgt.spill_ghost_of and tgt.spill_ghost_of != (c, r): blocked = True; break
                if tgt.value is not None or tgt.input is not None: blocked = True; break
            if blocked: break
        if blocked:
            cell.value = Err('#SPILL!')
            cell.kind = 'error'
            changed.add(addr)
            return
        # write anchor + ghosts
        cell.value = v[0][0] if v[0] else None
        cell.kind = value_kind(cell.value)
        cell.spill_anchor = (rows, cols)
        changed.add(addr)
        for rr in range(rows):
            for cc in range(cols):
                if rr == 0 and cc == 0: continue
                gv = v[rr][cc] if cc < len(v[rr]) else None
                gcell = sh.get_or_create(c + cc, r + rr)
                gcell.spill_ghost_of = (c, r)
                gcell.value = gv
                gcell.kind = value_kind(gv)
                gcell.input = None
                gcell.ast = None
                changed.add((sheet, c + cc, r + rr))
        return
    cell.value = v
    cell.kind = value_kind(v)
    changed.add(addr)
