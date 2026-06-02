"""Formula evaluator for a Workbook."""
from __future__ import annotations
import math
import re
from typing import Any, Dict, List, Optional, Tuple

from .ast import (
    Num, Str, Bool, Err, Ref, RangeRef, Name, BinOp, UnaryOp, PercentOp,
    Call, CallExpr, ArrayLit,
)
from .errors import XLError, NA, NAME, DIV0, VALUE, REF, NUM, CIRC, SPILL
from .funcs import all_funcs, _flatten, _to_array, _to_number, _to_text, _is_xl_error
from .refs import make_a1, parse_a1, parse_range, col_letters_to_index, col_index_to_letters

FUNCS = all_funcs()

# Spill-producing functions get array semantics by default; otherwise scalars.
ARRAY_FUNCS = {
    "SEQUENCE", "MAP", "BYROW", "BYCOL", "REDUCE", "FILTER", "SORT",
    "SORTBY", "UNIQUE", "TRANSPOSE", "RANDARRAY", "TOROW", "TOCOL",
    "WRAPROWS", "WRAPCOLS", "VSTACK", "HSTACK",
}


class Lambda:
    __slots__ = ("params", "body", "env")
    def __init__(self, params, body, env):
        self.params = params
        self.body = body
        self.env = env


class Evaluator:
    def __init__(self, workbook, current_sheet: str, anchor_ref: str = None,
                 iter_values: dict | None = None):
        self.wb = workbook
        self.current_sheet = current_sheet
        self.anchor_ref = anchor_ref
        # iter_values: a dict of {(sheet, ref): value} used during iterative calc
        # / goal seek, overriding the persisted cell value
        self.iter_values = iter_values or {}
        self.deps: set = set()  # (sheet, ref) accumulated during eval

    # ----- public entrypoint --------------------------------------

    def eval_node(self, node, env: dict | None = None):
        env = env or {}
        return self._eval(node, env)

    # ----- node dispatch ------------------------------------------

    def _eval(self, n, env):
        t = type(n)
        if t is Num: return n.value
        if t is Str: return n.value
        if t is Bool: return n.value
        if t is Err: return n.value
        if t is Ref:
            return self._eval_ref(n)
        if t is RangeRef:
            return self._eval_range(n)
        if t is Name:
            return self._eval_name(n, env)
        if t is BinOp:
            return self._eval_binop(n, env)
        if t is UnaryOp:
            return self._eval_unary(n, env)
        if t is PercentOp:
            v = self._eval(n.operand, env)
            return _to_number(v) / 100
        if t is Call:
            return self._eval_call(n, env)
        if t is CallExpr:
            fn = self._eval(n.fn, env)
            args = [self._eval(a, env) for a in n.args]
            return self._apply_lambda(fn, args)
        if t is ArrayLit:
            return [[self._eval(c, env) for c in row] for row in n.rows]
        raise XLError(VALUE)

    # ----- references --------------------------------------------

    def _eval_ref(self, n: Ref):
        sheet = n.sheet or self.current_sheet
        ref = make_a1(n.col, n.row)
        self.deps.add((sheet, ref))
        return self._cell_value(sheet, ref)

    def _cell_value(self, sheet, ref):
        # iterative override
        v = self.iter_values.get((sheet, ref))
        if v is not None:
            return v
        sh = self.wb.sheets.get(sheet)
        if sh is None:
            return 0  # missing sheet refs treated as empty
        cell = sh.cells.get(ref)
        if cell is None:
            return 0
        return cell.value if cell.value is not None else 0

    def _eval_range(self, n: RangeRef):
        sheet = n.sheet or self.current_sheet
        # accumulate dependencies for every cell in the range
        sh = self.wb.sheets.get(sheet)
        out = []
        for r in range(n.r0, n.r1 + 1):
            row = []
            for c in range(n.c0, n.c1 + 1):
                ref = make_a1(c, r)
                self.deps.add((sheet, ref))
                v = self.iter_values.get((sheet, ref))
                if v is None:
                    cell = sh.cells.get(ref) if sh else None
                    v = cell.value if cell is not None else None
                row.append(v)
            out.append(row)
        return out

    # ----- defined names -----------------------------------------

    def _eval_name(self, n: Name, env: dict):
        if n.name in env:
            return env[n.name]
        # workbook-scoped or sheet-scoped defined name
        nm = self.wb.lookup_name(n.name, self.current_sheet)
        if nm is None:
            raise XLError(NAME)
        sheet, ref = nm
        if ":" in ref:
            c0, r0, c1, r1 = parse_range(ref)
            # synthesize a RangeRef
            rr = RangeRef(sheet=sheet, c0=c0, r0=r0, c1=c1, r1=r1,
                          c0_abs=False, r0_abs=False, c1_abs=False, r1_abs=False)
            return self._eval_range(rr)
        c, r, _, _ = parse_a1(ref)
        rr = Ref(sheet=sheet, col=c, row=r, col_abs=False, row_abs=False)
        return self._eval_ref(rr)

    # ----- ops ---------------------------------------------------

    def _eval_binop(self, n: BinOp, env):
        op = n.op
        if op == ":":
            # range constructor — we don't support this dynamically here
            raise XLError(REF)
        l = self._eval(n.left, env)
        r = self._eval(n.right, env)
        if _is_xl_error(l): return l
        if _is_xl_error(r): return r
        if op == "&":
            return _to_text(l) + _to_text(r)
        if op in ("=", "<>", "<", "<=", ">", ">="):
            return self._broadcast(l, r, lambda a, b: self._cmp(op, a, b))
        # arithmetic
        if isinstance(l, list) or isinstance(r, list):
            return self._broadcast(l, r, lambda a, b: self._arith(op, a, b))
        return self._arith(op, l, r)

    def _eval_unary(self, n: UnaryOp, env):
        v = self._eval(n.operand, env)
        if _is_xl_error(v): return v
        if isinstance(v, list):
            return [[self._unary_scalar(n.op, x) for x in row] for row in v]
        return self._unary_scalar(n.op, v)

    def _unary_scalar(self, op, v):
        if op == "+":
            return _to_number(v)
        if op == "-":
            return -_to_number(v)
        return v

    def _arith(self, op, a, b):
        if a is None or a == "": a = 0
        if b is None or b == "": b = 0
        if isinstance(a, bool): a = int(a)
        if isinstance(b, bool): b = int(b)
        try:
            an = float(a); bn = float(b)
        except (TypeError, ValueError):
            raise XLError(VALUE)
        if op == "+": return an + bn
        if op == "-": return an - bn
        if op == "*": return an * bn
        if op == "/":
            if bn == 0: raise XLError(DIV0)
            return an / bn
        if op == "^":
            try:
                r = math.pow(an, bn)
            except (ValueError, OverflowError):
                raise XLError(NUM)
            return r
        raise XLError(VALUE)

    def _cmp(self, op, a, b):
        if a is None: a = 0
        if b is None: b = 0
        # numeric vs numeric
        if isinstance(a, (int, float)) and not isinstance(a, bool) and \
           isinstance(b, (int, float)) and not isinstance(b, bool):
            x, y = float(a), float(b)
        elif isinstance(a, bool) and isinstance(b, bool):
            x, y = a, b
        elif isinstance(a, str) and isinstance(b, str):
            x, y = a.lower(), b.lower()
        else:
            # Excel: types compare as: number < text < FALSE < TRUE
            order = lambda v: (0 if isinstance(v, (int, float)) and not isinstance(v, bool) else
                               1 if isinstance(v, str) else
                               2 if (isinstance(v, bool) and not v) else 3)
            ax, bx = order(a), order(b)
            x, y = ax, bx
        if op == "=": return x == y
        if op == "<>": return x != y
        if op == "<": return x < y
        if op == "<=": return x <= y
        if op == ">": return x > y
        if op == ">=": return x >= y
        return False

    def _broadcast(self, l, r, fn):
        """Element-wise broadcast over arrays. Scalar ↔ array, array ↔ array."""
        la = isinstance(l, list)
        ra = isinstance(r, list)
        if not la and not ra:
            return fn(l, r)
        if la and not ra:
            return [[fn(x, r) for x in row] for row in l]
        if ra and not la:
            return [[fn(l, x) for x in row] for row in r]
        # both arrays — pair shape; treat as 1D if needed
        l2 = l if (l and isinstance(l[0], list)) else [l]
        r2 = r if (r and isinstance(r[0], list)) else [r]
        rows = max(len(l2), len(r2))
        cols = max(max((len(row) for row in l2), default=1),
                   max((len(row) for row in r2), default=1))
        out = []
        for i in range(rows):
            row = []
            lr = l2[i] if i < len(l2) else l2[-1]
            rr = r2[i] if i < len(r2) else r2[-1]
            for j in range(cols):
                lv = lr[j] if j < len(lr) else lr[-1]
                rv = rr[j] if j < len(rr) else rr[-1]
                row.append(fn(lv, rv))
            out.append(row)
        return out

    # ----- function calls ----------------------------------------

    def _eval_call(self, n: Call, env):
        name = n.name.upper()

        # Pure-syntax forms: evaluate args lazily
        if name == "LET":
            return self._eval_let(n, env)
        if name == "LAMBDA":
            return self._eval_lambda_def(n, env)
        if name == "IF":
            if len(n.args) < 2:
                raise XLError(NA)
            cond = self._eval(n.args[0], env)
            if _is_xl_error(cond): return cond
            if isinstance(cond, list):
                # array IF
                tval = self._eval(n.args[1], env) if len(n.args) > 1 else False
                fval = self._eval(n.args[2], env) if len(n.args) > 2 else False
                return self._broadcast(cond, tval, lambda c, t: t) if False else \
                    [[self._scalar_if(c, n.args[1], n.args[2] if len(n.args) > 2 else None, env) for c in row] for row in cond]
            if self._truthy(cond):
                return self._eval(n.args[1], env)
            if len(n.args) > 2:
                return self._eval(n.args[2], env)
            return False
        if name == "IFS":
            for i in range(0, len(n.args), 2):
                if i + 1 >= len(n.args): break
                cond = self._eval(n.args[i], env)
                if _is_xl_error(cond): return cond
                if self._truthy(cond):
                    return self._eval(n.args[i + 1], env)
            raise XLError(NA)
        if name == "IFERROR":
            try:
                v = self._eval(n.args[0], env)
                if _is_xl_error(v):
                    return self._eval(n.args[1], env)
                return v
            except XLError:
                return self._eval(n.args[1], env)
        if name == "IFNA":
            try:
                v = self._eval(n.args[0], env)
                if v == NA:
                    return self._eval(n.args[1], env)
                return v
            except XLError as e:
                if e.code == NA:
                    return self._eval(n.args[1], env)
                raise
        if name == "TYPE":
            try:
                v = self._eval(n.args[0], env)
            except XLError:
                return 16
            if _is_xl_error(v): return 16
            if isinstance(v, bool): return 4
            if isinstance(v, (int, float)): return 1
            if isinstance(v, str): return 2
            if isinstance(v, list): return 64
            return 1
        if name == "ERROR.TYPE":
            try:
                v = self._eval(n.args[0], env)
            except XLError as e:
                v = e.code
            if not _is_xl_error(v):
                raise XLError(NA)
            mapping = {"#NULL!":1, "#DIV/0!":2, "#VALUE!":3, "#REF!":4, "#NAME?":5, "#NUM!":6, "#N/A":7, "#GETTING_DATA":8}
            return mapping.get(v, 8)
        if name == "ISBLANK":
            a = n.args[0] if n.args else None
            if isinstance(a, Ref):
                sheet = a.sheet or self.current_sheet
                ref = make_a1(a.col, a.row)
                self.deps.add((sheet, ref))
                sh = self.wb.sheets.get(sheet)
                cell = sh.cells.get(ref) if sh else None
                if cell is None: return True
                return cell.value is None or cell.value == ""
            v = self._eval(a, env) if a is not None else None
            return v is None or v == ""
        if name == "ISFORMULA":
            a = n.args[0] if n.args else None
            if isinstance(a, Ref):
                sheet = a.sheet or self.current_sheet
                ref = make_a1(a.col, a.row)
                sh = self.wb.sheets.get(sheet)
                cell = sh.cells.get(ref) if sh else None
                return bool(cell and cell.is_formula)
            return False
        if name in ("ISERROR", "ISERR", "ISNA"):
            try:
                v = self._eval(n.args[0], env)
            except XLError as e:
                if name == "ISNA":
                    return e.code == NA
                if name == "ISERR":
                    return e.code != NA
                return True
            if name == "ISNA":
                return v == NA
            if name == "ISERR":
                return _is_xl_error(v) and v != NA
            return _is_xl_error(v)

        # Special-cased dynamic-array functions (need lambdas/lazy)
        if name == "MAP":
            return self._fn_map(n.args, env)
        if name == "BYROW":
            return self._fn_byrow(n.args, env)
        if name == "BYCOL":
            return self._fn_bycol(n.args, env)
        if name == "REDUCE":
            return self._fn_reduce(n.args, env)
        if name == "FILTER":
            return self._fn_filter(n.args, env)
        if name == "SORT":
            return self._fn_sort(n.args, env)
        if name == "SORTBY":
            return self._fn_sortby(n.args, env)
        if name == "UNIQUE":
            return self._fn_unique(n.args, env)
        if name == "SEQUENCE":
            return self._fn_sequence(n.args, env)
        if name == "TRANSPOSE":
            arr = _to_array(self._eval(n.args[0], env))
            return [list(row) for row in zip(*arr)]
        if name == "OFFSET":
            return self._fn_offset(n.args, env)
        if name == "INDIRECT":
            return self._fn_indirect(n.args, env)
        if name == "ROWS":
            a = n.args[0]
            if isinstance(a, RangeRef):
                return a.r1 - a.r0 + 1
            if isinstance(a, Ref):
                return 1
            arr = _to_array(self._eval(a, env))
            return len(arr)
        if name == "COLUMNS":
            a = n.args[0]
            if isinstance(a, RangeRef):
                return a.c1 - a.c0 + 1
            if isinstance(a, Ref):
                return 1
            arr = _to_array(self._eval(a, env))
            return len(arr[0]) if arr else 0
        if name == "ROW":
            if not n.args:
                if self.anchor_ref:
                    _, r, _, _ = parse_a1(self.anchor_ref)
                    return r + 1
                return 1
            v = n.args[0]
            if isinstance(v, Ref):
                return v.row + 1
        if name == "COLUMN":
            if not n.args:
                if self.anchor_ref:
                    c, _, _, _ = parse_a1(self.anchor_ref)
                    return c + 1
                return 1
            v = n.args[0]
            if isinstance(v, Ref):
                return v.col + 1

        # Default: eager evaluation of arguments, then call FUNCS
        args = [self._eval(a, env) for a in n.args]
        # propagate first error
        for a in args:
            if _is_xl_error(a):
                return a
        if name in FUNCS:
            try:
                return FUNCS[name](*args)
            except XLError as e:
                return e.code
            except (TypeError, ValueError):
                raise XLError(VALUE)
            except ZeroDivisionError:
                raise XLError(DIV0)
        # could be a user-defined LET/LAMBDA in env
        if n.name in env and isinstance(env[n.name], Lambda):
            return self._apply_lambda(env[n.name], args)
        raise XLError(NAME)

    def _scalar_if(self, cond, t_node, f_node, env):
        return self._eval(t_node, env) if self._truthy(cond) else (self._eval(f_node, env) if f_node is not None else False)

    def _truthy(self, v):
        if isinstance(v, bool): return v
        if isinstance(v, (int, float)): return v != 0
        if isinstance(v, str):
            if v.upper() == "TRUE": return True
            if v.upper() == "FALSE": return False
            return False
        return False

    # ----- LET / LAMBDA -----------------------------------------

    def _eval_let(self, n: Call, env):
        new_env = dict(env)
        i = 0
        # Pairs of (Name, value) ending with a body expression
        while i + 1 < len(n.args):
            name_node = n.args[i]
            val_node = n.args[i + 1]
            if isinstance(name_node, Name):
                key = name_node.name
            else:
                raise XLError(NAME)
            v = self._eval(val_node, new_env)
            new_env[key] = v
            i += 2
            if i + 1 >= len(n.args):
                # last arg is the body
                break
        if i >= len(n.args):
            raise XLError(NA)
        return self._eval(n.args[i], new_env)

    def _eval_lambda_def(self, n: Call, env):
        if not n.args:
            raise XLError(NA)
        params = []
        for p in n.args[:-1]:
            if not isinstance(p, Name):
                raise XLError(VALUE)
            params.append(p.name)
        body = n.args[-1]
        return Lambda(params, body, env)

    def _apply_lambda(self, fn, args):
        if not isinstance(fn, Lambda):
            raise XLError(VALUE)
        env = dict(fn.env)
        for k, v in zip(fn.params, args):
            env[k] = v
        return self._eval(fn.body, env)

    # ----- dynamic-array implementations -------------------------

    def _fn_sequence(self, args, env):
        rows = _to_number(self._eval(args[0], env)) if len(args) >= 1 else 1
        cols = _to_number(self._eval(args[1], env)) if len(args) >= 2 else 1
        start = _to_number(self._eval(args[2], env)) if len(args) >= 3 else 1
        step = _to_number(self._eval(args[3], env)) if len(args) >= 4 else 1
        rows = int(rows); cols = int(cols)
        out = []
        v = start
        for r in range(rows):
            row = []
            for c in range(cols):
                row.append(v)
                v += step
            out.append(row)
        return out

    def _fn_map(self, args, env):
        if len(args) < 2:
            raise XLError(NA)
        arrays = [_to_array(self._eval(a, env)) for a in args[:-1]]
        fn = self._eval(args[-1], env)
        rows = len(arrays[0]); cols = len(arrays[0][0]) if arrays[0] else 0
        out = []
        for r in range(rows):
            row = []
            for c in range(cols):
                vs = [arr[r][c] if r < len(arr) and c < len(arr[r]) else None for arr in arrays]
                row.append(self._apply_lambda(fn, vs))
            out.append(row)
        return out

    def _fn_byrow(self, args, env):
        arr = _to_array(self._eval(args[0], env))
        fn = self._eval(args[1], env)
        out = []
        for r in arr:
            res = self._apply_lambda(fn, [[r]])  # pass as a 1-row 2D
            out.append([res if not isinstance(res, list) else _flatten([res])[0]])
        return out

    def _fn_bycol(self, args, env):
        arr = _to_array(self._eval(args[0], env))
        fn = self._eval(args[1], env)
        if not arr: return []
        ncols = len(arr[0])
        out_row = []
        for c in range(ncols):
            col = [[r[c] for r in arr]]
            res = self._apply_lambda(fn, [col])
            out_row.append(res if not isinstance(res, list) else _flatten([res])[0])
        return [out_row]

    def _fn_reduce(self, args, env):
        if len(args) < 3:
            raise XLError(NA)
        acc = self._eval(args[0], env)
        arr = _to_array(self._eval(args[1], env))
        fn = self._eval(args[2], env)
        for row in arr:
            for v in row:
                acc = self._apply_lambda(fn, [acc, v])
        return acc

    def _fn_filter(self, args, env):
        arr = _to_array(self._eval(args[0], env))
        keep = self._eval(args[1], env)
        if not isinstance(keep, list):
            keep = [[keep]]
        if isinstance(keep[0], list):
            flat = _flatten(keep)
        else:
            flat = list(keep)
        out = []
        for i, row in enumerate(arr):
            if i < len(flat):
                if self._truthy(flat[i]):
                    out.append(list(row))
        if not out:
            if len(args) >= 3:
                fb = self._eval(args[2], env)
                return fb
            raise XLError(NA)
        return out

    def _fn_sort(self, args, env):
        arr = _to_array(self._eval(args[0], env))
        sort_idx = int(_to_number(self._eval(args[1], env))) - 1 if len(args) >= 2 else 0
        order = int(_to_number(self._eval(args[2], env))) if len(args) >= 3 else 1
        by_col = self._truthy(self._eval(args[3], env)) if len(args) >= 4 else False
        if by_col:
            cols = list(zip(*arr))
            sorted_cols = sorted(cols, key=lambda c: self._sort_key(c[sort_idx]),
                                 reverse=(order < 0))
            return [list(r) for r in zip(*sorted_cols)]
        return sorted([list(r) for r in arr],
                      key=lambda r: self._sort_key(r[sort_idx]),
                      reverse=(order < 0))

    def _fn_sortby(self, args, env):
        arr = _to_array(self._eval(args[0], env))
        # pairs: by_arr, order, by_arr2, order2 ...
        bys = []
        i = 1
        while i < len(args):
            bv = _flatten(self._eval(args[i], env))
            order = 1
            if i + 1 < len(args):
                try:
                    order = int(_to_number(self._eval(args[i + 1], env)))
                    i += 2
                except Exception:
                    i += 1
            else:
                i += 1
            bys.append((bv, order))
        idx = list(range(len(arr)))
        for bv, order in reversed(bys):
            idx.sort(key=lambda k: self._sort_key(bv[k]), reverse=(order < 0))
        return [list(arr[i]) for i in idx]

    def _fn_unique(self, args, env):
        arr = _to_array(self._eval(args[0], env))
        seen = []
        for r in arr:
            key = tuple(r)
            if key not in seen:
                seen.append(key)
        return [list(r) for r in seen]

    def _sort_key(self, v):
        if v is None or v == "":
            return (3,)
        if isinstance(v, bool):
            return (2, v)
        if isinstance(v, (int, float)):
            return (0, v)
        if isinstance(v, str):
            return (1, v.lower())
        return (4,)

    # ----- OFFSET / INDIRECT -------------------------------------

    def _fn_offset(self, args, env):
        if not args:
            raise XLError(NA)
        ref_node = args[0]
        rows = int(_to_number(self._eval(args[1], env))) if len(args) > 1 else 0
        cols = int(_to_number(self._eval(args[2], env))) if len(args) > 2 else 0
        height = int(_to_number(self._eval(args[3], env))) if len(args) > 3 else None
        width = int(_to_number(self._eval(args[4], env))) if len(args) > 4 else None
        # Resolve the reference statically
        sheet, c0, r0, c1, r1 = self._static_ref(ref_node)
        if height is None: height = r1 - r0 + 1
        if width is None: width = c1 - c0 + 1
        c0 += cols; r0 += rows
        c1 = c0 + width - 1; r1 = r0 + height - 1
        return self._read_box(sheet, c0, r0, c1, r1)

    def _fn_indirect(self, args, env):
        s = _to_text(self._eval(args[0], env))
        try:
            from .refs import split_sheet_ref, parse_range, parse_a1
            sheet, local = split_sheet_ref(s)
            if sheet is None: sheet = self.current_sheet
            if ":" in local:
                c0, r0, c1, r1 = parse_range(local)
                return self._read_box(sheet, c0, r0, c1, r1)
            c, r, _, _ = parse_a1(local)
            self.deps.add((sheet, make_a1(c, r)))
            return self._cell_value(sheet, make_a1(c, r))
        except Exception:
            raise XLError(REF)

    def _static_ref(self, node):
        if isinstance(node, Ref):
            sheet = node.sheet or self.current_sheet
            return sheet, node.col, node.row, node.col, node.row
        if isinstance(node, RangeRef):
            sheet = node.sheet or self.current_sheet
            return sheet, node.c0, node.r0, node.c1, node.r1
        raise XLError(REF)

    def _read_box(self, sheet, c0, r0, c1, r1):
        if c0 > c1 or r0 > r1:
            raise XLError(REF)
        out = []
        for r in range(r0, r1 + 1):
            row = []
            for c in range(c0, c1 + 1):
                ref = make_a1(c, r)
                self.deps.add((sheet, ref))
                row.append(self._cell_value(sheet, ref))
            out.append(row)
        if len(out) == 1 and len(out[0]) == 1:
            return out[0][0]
        return out
