"""In-memory workbook with persistence to /app/data/<id>.json."""
from __future__ import annotations
import json
import os
import threading
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from .ast import Num, Str, Bool, Ref, RangeRef, Name, BinOp, UnaryOp, Call, CallExpr, PercentOp, Err, ArrayLit

def _extract_deps(node, current_sheet, wb):
    """Walk an AST and return a set of (sheet, ref) cells that this
    formula reads — used to seed the dependency graph before evaluation."""
    out = set()
    def go(n):
        if n is None:
            return
        if isinstance(n, Ref):
            sheet = n.sheet or current_sheet
            out.add((sheet, make_a1(n.col, n.row)))
        elif isinstance(n, RangeRef):
            sheet = n.sheet or current_sheet
            for r in range(n.r0, n.r1 + 1):
                for c in range(n.c0, n.c1 + 1):
                    out.add((sheet, make_a1(c, r)))
        elif isinstance(n, Name):
            # could resolve to a defined name → cell or range
            nm = wb.lookup_name(n.name, current_sheet)
            if nm is not None:
                sheet, ref = nm
                if ":" in ref:
                    from .refs import parse_range as _pr
                    c0, r0, c1, r1 = _pr(ref)
                    for r in range(r0, r1 + 1):
                        for c in range(c0, c1 + 1):
                            out.add((sheet, make_a1(c, r)))
                else:
                    c, r, _, _ = parse_a1(ref)
                    out.add((sheet, make_a1(c, r)))
        elif isinstance(n, BinOp):
            go(n.left); go(n.right)
        elif isinstance(n, UnaryOp):
            go(n.operand)
        elif isinstance(n, PercentOp):
            go(n.operand)
        elif isinstance(n, Call):
            up = n.name.upper()
            # ROWS/COLUMNS only inspect shape; don't add deps
            if up in ("ROWS", "COLUMNS"):
                # but if the arg is itself a non-trivial expression, recurse
                for a in n.args:
                    if not isinstance(a, (Ref, RangeRef)):
                        go(a)
            else:
                for a in n.args:
                    go(a)
        elif isinstance(n, CallExpr):
            go(n.fn)
            for a in n.args:
                go(a)
        elif isinstance(n, ArrayLit):
            for row in n.rows:
                for cell in row:
                    go(cell)
    go(node)
    return out

from .cell import Cell
from .eval import Evaluator, Lambda
from .errors import XLError, CIRC, NA, NAME, REF, SPILL, VALUE
from .funcs import _is_xl_error
from .parser import parse_formula, ParseError
from .refs import (col_index_to_letters, col_letters_to_index, expand_range,
                    make_a1, parse_a1, parse_range, normalize_ref, split_sheet_ref)
from .locale_aliases import (canonicalize_call, arg_separator, decimal_separator,
                             locale_table)


DATA_DIR = "/app/data"


class Sheet:
    def __init__(self, name: str):
        self.name = name
        self.cells: Dict[str, Cell] = {}

    def get(self, ref: str) -> Optional[Cell]:
        return self.cells.get(normalize_ref(ref))

    def set(self, cell: Cell):
        self.cells[normalize_ref(cell.ref)] = cell


class Workbook:
    def __init__(self, id_: int, name: str):
        self.id = id_
        self.name = name
        self.sheets: Dict[str, Sheet] = {}
        self.sheet_order: List[str] = []
        # forward dep graph: (sheet, ref) -> set of (sheet, ref) that depend on it
        self.dependents: Dict[Tuple[str, str], set] = defaultdict(set)
        # reverse: (sheet, ref) -> set of (sheet, ref) it reads
        self.reads: Dict[Tuple[str, str], set] = defaultdict(set)
        # defined names: (scope, name) -> {scope, sheet, name, range}
        self.names: Dict[Tuple[str, str, str], dict] = {}
        # data_validations: list of {sheet, range, rule}
        self.data_validations: List[dict] = []
        # conditional formats: list of {sheet, range, rule}
        self.conditional_formats: List[dict] = []
        # settings
        self.settings = {
            "iterative_calc": {"enabled": False, "max_iterations": 100, "max_change": 0.001},
            "locale": None,
        }
        self.lock = threading.RLock()
        self.seq = 0  # collab seq counter
        self.collab_log: List[dict] = []  # past events for backfill

    # ------------------------------------------------------------
    # serialization

    def to_json(self) -> dict:
        sheets_out = []
        for s_name in self.sheet_order:
            sh = self.sheets[s_name]
            cells_out = []
            for ref, cell in sh.cells.items():
                if cell.is_empty() and not cell.spill_anchor:
                    continue
                if cell.spill_anchor and not cell.raw_input:
                    continue  # ghosts derived; don't persist
                cells_out.append({
                    "ref": ref,
                    "input": cell.raw_input,
                    "format": cell.format,
                    "style": cell.style,
                })
            sheets_out.append({"name": s_name, "cells": cells_out})
        return {
            "id": self.id,
            "name": self.name,
            "sheets": sheets_out,
            "names": list(self.names.values()),
            "data_validations": self.data_validations,
            "conditional_formats": self.conditional_formats,
            "settings": self.settings,
        }

    @classmethod
    def from_json(cls, data: dict) -> "Workbook":
        wb = cls(data["id"], data.get("name", ""))
        for s in data.get("sheets", []):
            sh = Sheet(s["name"])
            wb.sheets[s["name"]] = sh
            wb.sheet_order.append(s["name"])
            for c in s.get("cells", []):
                cell = Cell(sheet=s["name"], ref=c["ref"], raw_input=c.get("input"),
                            format=c.get("format"), style=c.get("style"))
                sh.set(cell)
        for n in data.get("names", []):
            key = (n.get("scope", "workbook"), (n.get("sheet") or ""), n["name"])
            wb.names[key] = n
        wb.data_validations = data.get("data_validations", [])
        wb.conditional_formats = data.get("conditional_formats", [])
        wb.settings = data.get("settings", wb.settings)
        # parse formulas + recompute
        wb.recompute_all()
        return wb

    def save(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        path = os.path.join(DATA_DIR, f"{self.id}.json")
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.to_json(), f)
        os.replace(tmp, path)

    # ------------------------------------------------------------
    # sheet management

    def add_sheet(self, name: str) -> Sheet:
        if name in self.sheets:
            return self.sheets[name]
        sh = Sheet(name)
        self.sheets[name] = sh
        self.sheet_order.append(name)
        return sh

    def remove_sheet(self, name: str) -> bool:
        if name not in self.sheets:
            return False
        # drop deps that reference cells in this sheet
        for ref, cell in list(self.sheets[name].cells.items()):
            self._clear_reads((name, ref))
        del self.sheets[name]
        self.sheet_order = [s for s in self.sheet_order if s != name]
        return True

    # ------------------------------------------------------------
    # defined names

    def add_name(self, scope: str, sheet: Optional[str], name: str, rng: str):
        key = (scope, sheet or "", name)
        self.names[key] = {"scope": scope, "sheet": sheet, "name": name, "range": rng}

    def lookup_name(self, name: str, current_sheet: str) -> Optional[Tuple[str, str]]:
        # try sheet-scoped first
        sheet_key = ("sheet", current_sheet, name)
        if sheet_key in self.names:
            n = self.names[sheet_key]
            return self._split_name_range(n["range"], current_sheet)
        wb_key = ("workbook", "", name)
        if wb_key in self.names:
            n = self.names[wb_key]
            return self._split_name_range(n["range"], current_sheet)
        return None

    def _split_name_range(self, rng: str, current_sheet: str):
        s, local = split_sheet_ref(rng)
        return (s or current_sheet, local)

    # ------------------------------------------------------------
    # cell mutation

    def patch_cells(self, patches: List[dict], locale: Optional[str] = None):
        """Apply a list of cell patches and return the updated cells.

        Each patch: {sheet, ref, input?, format?, style?}.
        Returns: list of cell-dicts (changed cells, including dependents
        and any spilled ghosts).
        """
        with self.lock:
            return self._patch_locked(patches, locale)

    def _patch_locked(self, patches: List[dict], locale: Optional[str]):
        loc = locale or self.settings.get("locale")
        # validate and stage
        dirty = set()
        affected = set()
        # spill ghosts that may need cleanup before writes
        spill_cleared = set()
        for p in patches:
            sheet = p["sheet"]
            ref_in = p["ref"]
            ref = normalize_ref(ref_in)
            if sheet not in self.sheets:
                raise KeyError(f"unknown sheet {sheet!r}")
            input_val = p.get("input", None)
            sh = self.sheets[sheet]
            existing = sh.cells.get(ref)
            # reject writes to spill ghosts (unless we're writing the anchor itself)
            if existing and existing.spill_anchor is not None and (existing.sheet, existing.ref) != existing.spill_anchor:
                # ghost cell — only allowed to clear via clearing the anchor, OR
                # writing the anchor itself
                if input_val not in (None, ""):
                    from .errors import XLError as _XL
                    err = _XL("SpillTargetWriteError")
                    err.code = "SpillTargetWriteError"
                    raise err

            # data validation
            if input_val not in (None, ""):
                if not (isinstance(input_val, str) and input_val.startswith("=")):
                    if not self._validate_literal(sheet, ref, input_val):
                        from .errors import XLError as _XL
                        err = _XL("ValidationError")
                        err.code = "ValidationError"
                        raise err

            # if existing cell was an anchor with spill, clear ghosts before overwrite
            if existing and existing.spill_array is not None:
                anchor = (existing.sheet, existing.ref)
                _clear_spill_ghosts(self, anchor)
                spill_cleared.add(anchor)

            # build new cell
            cell = Cell(sheet=sheet, ref=ref)
            cell.format = p.get("format", existing.format if existing else None)
            cell.style = p.get("style", existing.style if existing else None)
            if input_val is None or input_val == "":
                cell.raw_input = None
                cell.formula = None
                cell.is_formula = False
                cell.value = None
            else:
                cell.raw_input = input_val
                if isinstance(input_val, str) and input_val.startswith("="):
                    body = input_val[1:]
                    sep = arg_separator(loc)
                    dec = decimal_separator(loc)
                    try:
                        ast = parse_formula(body, arg_sep=sep, decimal_sep=dec)
                    except ParseError:
                        from .errors import XLError as _XL
                        err = _XL("ParseError")
                        err.code = "ParseError"
                        raise err
                    # canonicalize call names
                    if loc:
                        ast = self._canonicalize(ast, loc)
                    cell.formula = ast
                    cell.is_formula = True
                else:
                    cell.value = self._coerce_literal(input_val)
            sh.set(cell)
            dirty.add((sheet, ref))
            affected.add((sheet, ref))

        # clear reads/dependents for dirtied cells
        for s, r in dirty:
            self._clear_reads((s, r))

        # statically register dependency edges for newly-parsed formulas
        # so the topo-sort can see the structure before evaluation
        for s, r in dirty:
            cell = self.sheets[s].cells.get(r)
            if cell and cell.is_formula and cell.formula is not None:
                static_deps = _extract_deps(cell.formula, s, self)
                self._add_reads((s, r), static_deps)

        # recompute dirty + transitively
        changed = self._recompute(dirty)
        affected.update(changed)
        # add any cells freshly cleared by spill clearing
        for s, r in spill_cleared:
            sh = self.sheets[s]
            cell = sh.cells.get(r)
            if cell:
                affected.add((s, r))

        # rebuild conditional format payloads for affected cells
        return self._cells_dicts_for(affected)

    def _cells_dicts_for(self, refs):
        out = []
        seen = set()
        for s, r in refs:
            if (s, r) in seen: continue
            seen.add((s, r))
            sh = self.sheets.get(s)
            if not sh: continue
            cell = sh.cells.get(r)
            if cell is None:
                out.append({"sheet": s, "ref": r, "value": None, "kind": "empty", "input": None, "display": ""})
                continue
            out.append(self.cell_dict(s, r))
        return out

    def cell_dict(self, sheet: str, ref: str) -> dict:
        from .formatting import format_value
        sh = self.sheets.get(sheet)
        if sh is None or ref not in sh.cells:
            return {"sheet": sheet, "ref": ref, "value": None, "kind": "empty", "input": None, "display": ""}
        cell = sh.cells[ref]
        v = cell.value
        kind = self._kind_for(v, cell)
        out = {
            "sheet": sheet,
            "ref": ref,
            "input": cell.raw_input,
            "value": v,
            "kind": kind,
            "format": cell.format,
            "style": cell.style or {},
            "display": format_value(v, cell.format),
        }
        if cell.spill_array is not None and cell.spill_anchor and cell.spill_anchor == (cell.sheet, cell.ref):
            out["spill"] = True
            arr = cell.spill_array
            out["spill_range"] = {"rows": len(arr), "cols": len(arr[0]) if arr else 0}
        cf = self._compute_cf(sheet, ref, v)
        if cf:
            out["cf"] = cf
        return out

    def _kind_for(self, v, cell):
        if cell.spill_anchor is not None and cell.spill_anchor != (cell.sheet, cell.ref):
            return "spill"
        if v is None or v == "":
            return "empty"
        if _is_xl_error(v):
            return "error"
        if isinstance(v, bool):
            return "bool"
        if isinstance(v, (int, float)):
            return "number"
        if isinstance(v, str):
            return "string"
        return "string"

    def _coerce_literal(self, v: str):
        s = v.strip()
        if s.upper() == "TRUE": return True
        if s.upper() == "FALSE": return False
        # number?
        try:
            if any(c in s for c in ".eE"):
                return float(s)
            return int(s)
        except ValueError:
            pass
        return v

    def _canonicalize(self, ast, locale):
        """Walk the AST and rename Call nodes per the locale alias table."""
        from .ast import Call as _Call, CallExpr as _CallExpr, BinOp as _Bin, UnaryOp as _U, PercentOp as _P, ArrayLit as _AL
        def go(n):
            if isinstance(n, _Call):
                new = canonicalize_call(n.name, locale)
                args = [go(a) for a in n.args]
                return _Call(new, args)
            if isinstance(n, _CallExpr):
                return _CallExpr(go(n.fn), [go(a) for a in n.args])
            if isinstance(n, _Bin):
                return _Bin(n.op, go(n.left), go(n.right))
            if isinstance(n, _U):
                return _U(n.op, go(n.operand))
            if isinstance(n, _P):
                return _P(go(n.operand))
            if isinstance(n, _AL):
                return _AL([[go(c) for c in row] for row in n.rows])
            return n
        return go(ast)

    # ------------------------------------------------------------
    # data validation

    def _validate_literal(self, sheet: str, ref: str, input_str: str) -> bool:
        """Return True if value passes; False if blocked."""
        # find applicable rules
        c, r, _, _ = parse_a1(ref)
        for rule in self.data_validations:
            if rule.get("sheet") != sheet: continue
            c0, r0, c1, r1 = parse_range(rule["range"])
            if not (c0 <= c <= c1 and r0 <= r <= r1):
                continue
            ok = self._check_rule(input_str, rule["rule"])
            if not ok:
                return False
        return True

    def _check_rule(self, raw_input, rule):
        kind = rule.get("kind")
        v_str = raw_input
        # try numeric coercion
        try:
            v_num = float(v_str) if not isinstance(v_str, (int, float)) else float(v_str)
        except (ValueError, TypeError):
            v_num = None
        if kind == "list":
            allowed = rule.get("values", [])
            return v_str in allowed
        if kind == "integer":
            if v_num is None or v_num != int(v_num): return False
            return self._range_check(v_num, rule)
        if kind == "decimal":
            if v_num is None: return False
            return self._range_check(v_num, rule)
        if kind == "text_length":
            n = len(str(v_str))
            return self._range_check(n, rule)
        return True

    def _range_check(self, val, rule):
        op = rule.get("op", "between")
        if op == "between":
            return rule.get("min", -1e308) <= val <= rule.get("max", 1e308)
        if op == "not_between":
            return not (rule.get("min", -1e308) <= val <= rule.get("max", 1e308))
        ref = rule.get("value", 0)
        if op in ("=", "equal"): return val == ref
        if op in ("<>", "not_equal"): return val != ref
        if op == "<": return val < ref
        if op == "<=": return val <= ref
        if op == ">": return val > ref
        if op == ">=": return val >= ref
        return True

    # ------------------------------------------------------------
    # conditional formats

    def _compute_cf(self, sheet, ref, value):
        c, r, _, _ = parse_a1(ref)
        out = []
        for cf in self.conditional_formats:
            if cf.get("sheet") != sheet: continue
            c0, r0, c1, r1 = parse_range(cf["range"])
            if not (c0 <= c <= c1 and r0 <= r <= r1):
                continue
            rule = cf.get("rule", {})
            if rule.get("kind") == "cell_value":
                if self._cf_cell_value_matches(value, rule):
                    out.append({"style": rule.get("style", {})})
        return out

    def _cf_cell_value_matches(self, v, rule):
        op = rule.get("op")
        if op == "between":
            lo, hi = rule.get("value", [0, 0])
            try:
                vn = float(v)
            except Exception:
                return False
            return lo <= vn <= hi
        cmp_v = rule.get("value")
        if v is None or _is_xl_error(v): return False
        if op == "=":
            try: return float(v) == float(cmp_v)
            except Exception: return v == cmp_v
        if op == "<>":
            try: return float(v) != float(cmp_v)
            except Exception: return v != cmp_v
        try:
            x = float(v); y = float(cmp_v)
        except Exception:
            return False
        return {"<": x < y, "<=": x <= y, ">": x > y, ">=": x >= y}.get(op, False)

    # ------------------------------------------------------------
    # dependency graph

    def _clear_reads(self, key):
        old_reads = self.reads.pop(key, set())
        for r in old_reads:
            self.dependents[r].discard(key)

    def _add_reads(self, key, reads):
        self.reads[key] = set(reads)
        for r in reads:
            self.dependents[r].add(key)

    # ------------------------------------------------------------
    # recompute

    def _recompute(self, seeds):
        """Recompute the seeds plus their transitive dependents in
        topological order. Returns the set of cells whose value
        actually changed (so callers know what to broadcast)."""
        # gather closure
        closure = set()
        stack = list(seeds)
        while stack:
            k = stack.pop()
            if k in closure: continue
            closure.add(k)
            for d in self.dependents.get(k, ()):
                if d not in closure:
                    stack.append(d)
        # topo sort by Kahn over a sub-graph of `closure`
        order = self._topo(closure)
        if order is None:
            # cycle — set everyone in cycle to #CIRC! unless iterative-calc enabled
            ic = self.settings.get("iterative_calc", {})
            if ic.get("enabled"):
                # iterative: start values 0, run up to max_iter
                self._iterate(closure)
                return closure
            for s, r in closure:
                sh = self.sheets.get(s)
                if not sh: continue
                cell = sh.cells.get(r)
                if cell and cell.is_formula:
                    cell.value = "#CIRC!"
            return closure

        changed = set()
        for s, r in order:
            sh = self.sheets.get(s)
            if not sh: continue
            cell = sh.cells.get(r)
            if cell is None:
                continue
            if not cell.is_formula:
                continue
            old_value = cell.value
            self._eval_cell(cell)
            if cell.value != old_value:
                changed.add((s, r))
        # always include seeds (they may be literals whose value changed)
        return closure

    def _topo(self, closure):
        # restricted in-degree
        in_deg = {k: 0 for k in closure}
        for k in closure:
            for d in self.dependents.get(k, ()):
                if d in closure:
                    in_deg[d] = in_deg.get(d, 0) + 1
        ready = [k for k, d in in_deg.items() if d == 0]
        order = []
        while ready:
            k = ready.pop()
            order.append(k)
            for d in self.dependents.get(k, ()):
                if d in closure:
                    in_deg[d] -= 1
                    if in_deg[d] == 0:
                        ready.append(d)
        if len(order) != len(closure):
            return None
        return order

    def _eval_cell(self, cell: Cell):
        ev = Evaluator(self, cell.sheet, anchor_ref=cell.ref)
        try:
            v = ev.eval_node(cell.formula)
        except XLError as e:
            cell.value = e.code
            cell.spill_array = None
            self._add_reads((cell.sheet, cell.ref), ev.deps)
            return
        # array result -> spill
        if isinstance(v, list):
            arr = v if v and isinstance(v[0], list) else [v]
            # check spill targets are clear
            anchor_c, anchor_r, _, _ = parse_a1(cell.ref)
            sh = self.sheets[cell.sheet]
            blocked = False
            for dr, row in enumerate(arr):
                for dc, _v in enumerate(row):
                    if dr == 0 and dc == 0: continue
                    target_ref = make_a1(anchor_c + dc, anchor_r + dr)
                    other = sh.cells.get(target_ref)
                    if other and (other.raw_input not in (None, "") and other.spill_anchor != (cell.sheet, cell.ref)):
                        blocked = True; break
                if blocked: break
            if blocked:
                cell.value = "#SPILL!"
                cell.spill_array = None
                self._add_reads((cell.sheet, cell.ref), ev.deps)
                return
            cell.value = arr[0][0] if arr and arr[0] else None
            cell.spill_array = arr
            cell.spill_anchor = (cell.sheet, cell.ref)
            # write ghosts
            for dr, row in enumerate(arr):
                for dc, vv in enumerate(row):
                    target_ref = make_a1(anchor_c + dc, anchor_r + dr)
                    if dr == 0 and dc == 0:
                        # cell itself; nothing to do
                        continue
                    ghost = Cell(sheet=cell.sheet, ref=target_ref)
                    ghost.value = vv
                    ghost.spill_anchor = (cell.sheet, cell.ref)
                    sh.set(ghost)
        else:
            cell.value = v
            cell.spill_array = None
        self._add_reads((cell.sheet, cell.ref), ev.deps)

    def _iterate(self, closure):
        """Iterative-calc fixed-point loop."""
        ic = self.settings.get("iterative_calc", {})
        max_iter = int(ic.get("max_iterations", 100))
        max_change = float(ic.get("max_change", 0.001))
        # initialise unknowns to 0 (do not replace cells that have a current
        # numeric value — keep the current as the seed)
        for s, r in closure:
            cell = self.sheets[s].cells.get(r)
            if cell is None: continue
            if cell.is_formula and not isinstance(cell.value, (int, float)):
                cell.value = 0
        # iteration loop
        # unique evaluators per pass
        for _ in range(max_iter):
            max_delta = 0.0
            for s, r in closure:
                cell = self.sheets[s].cells.get(r)
                if cell is None or not cell.is_formula: continue
                ev = Evaluator(self, s, anchor_ref=r)
                try:
                    v = ev.eval_node(cell.formula)
                except XLError as e:
                    v = e.code
                old = cell.value
                if isinstance(v, list):
                    v = v[0][0] if v and isinstance(v[0], list) else (v[0] if v else 0)
                cell.value = v
                self._add_reads((s, r), ev.deps)
                if isinstance(old, (int, float)) and isinstance(v, (int, float)):
                    d = abs(float(v) - float(old))
                    if d > max_delta:
                        max_delta = d
            if max_delta <= max_change:
                break

    def recompute_all(self):
        """Parse all formulas and fully recompute (used at load time)."""
        all_keys = []
        loc = self.settings.get("locale")
        for s_name, sh in self.sheets.items():
            for ref, cell in sh.cells.items():
                if cell.raw_input and isinstance(cell.raw_input, str) and cell.raw_input.startswith("="):
                    try:
                        sep = arg_separator(loc); dec = decimal_separator(loc)
                        ast = parse_formula(cell.raw_input[1:], arg_sep=sep, decimal_sep=dec)
                        if loc:
                            ast = self._canonicalize(ast, loc)
                        cell.formula = ast
                        cell.is_formula = True
                    except ParseError:
                        cell.value = "#NAME?"
                        cell.is_formula = False
                else:
                    cell.is_formula = False
                    if cell.raw_input is not None:
                        cell.value = self._coerce_literal(cell.raw_input)
                all_keys.append((s_name, ref))
        # naive: dirty everything and recompute
        # First clear any stale reads
        for k in all_keys:
            self._clear_reads(k)
        # static dep extraction so topo can order them
        for s, r in all_keys:
            cell = self.sheets[s].cells.get(r)
            if cell and cell.is_formula and cell.formula is not None:
                static_deps = _extract_deps(cell.formula, s, self)
                self._add_reads((s, r), static_deps)
        self._recompute(set(all_keys))


def _clear_spill_ghosts(wb: Workbook, anchor: Tuple[str, str]):
    """Drop ghost cells whose anchor matches `anchor`."""
    sh = wb.sheets.get(anchor[0])
    if sh is None: return
    for ref in list(sh.cells.keys()):
        cell = sh.cells[ref]
        if cell.spill_anchor == anchor and (cell.sheet, cell.ref) != anchor:
            del sh.cells[ref]
