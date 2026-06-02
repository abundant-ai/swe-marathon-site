"""HTTP and WebSocket handlers."""
from __future__ import annotations
import asyncio
import csv
import io
import json
import logging
import math
import os
import re
import time
from typing import Any, Dict, List, Optional

from aiohttp import web, WSMsgType

from .errors import XLError
from .funcs import _is_xl_error
from .store import Store
from .workbook import Workbook, _clear_spill_ghosts
from .refs import (col_index_to_letters, col_letters_to_index, expand_range,
                    make_a1, parse_a1, parse_range, normalize_ref, split_sheet_ref)
from .formatting import format_value
from .parser import parse_formula, ParseError
from .ast import Ref as RefNode, RangeRef as RangeNode, Name as NameNode, Call, CallExpr, BinOp, UnaryOp, PercentOp, Num, Str, Bool, Err
from .ooxml import import_xlsx, export_xlsx


log = logging.getLogger("tabula")


# ----- helpers -------------------------------------------------------


def err_response(status: int, error: str, message: str = ""):
    return web.json_response({"error": error, "message": message}, status=status)


def workbook_or_404(store: Store, wb_id: str):
    try:
        wid = int(wb_id)
    except (TypeError, ValueError):
        return None
    return store.get(wid)


def workbook_summary(wb: Workbook) -> dict:
    sheets = []
    for s_name in wb.sheet_order:
        sh = wb.sheets[s_name]
        cells = []
        for ref in sorted(sh.cells.keys(), key=_ref_sort_key):
            cells.append(wb.cell_dict(s_name, ref))
        sheets.append({"name": s_name, "cells": cells})
    return {
        "id": wb.id,
        "name": wb.name,
        "sheets": sheets,
    }


def _ref_sort_key(ref: str):
    c, r, _, _ = parse_a1(ref)
    return (r, c)


# ----- workbook list / CRUD -----------------------------------------


async def health(request):
    return web.json_response({"status": "ok"})


async def list_workbooks(request):
    store: Store = request.app["store"]
    return web.json_response({"workbooks": store.list()})


async def create_workbook(request):
    store: Store = request.app["store"]
    try:
        body = await request.json()
    except Exception:
        body = {}
    name = body.get("name", "Untitled") if isinstance(body, dict) else "Untitled"
    wb = store.create(name)
    return web.json_response({"id": wb.id, "name": wb.name, "sheets": list(wb.sheet_order)})


async def get_workbook(request):
    store: Store = request.app["store"]
    wb = workbook_or_404(store, request.match_info["id"])
    if not wb:
        return err_response(404, "NotFound")
    return web.json_response(workbook_summary(wb))


async def delete_workbook(request):
    store: Store = request.app["store"]
    wb = workbook_or_404(store, request.match_info["id"])
    if not wb:
        return err_response(404, "NotFound")
    store.delete(wb.id)
    return web.json_response({"ok": True})


# ----- sheets --------------------------------------------------------


async def add_sheet(request):
    store: Store = request.app["store"]
    wb = workbook_or_404(store, request.match_info["id"])
    if not wb:
        return err_response(404, "NotFound")
    body = await request.json()
    name = body["name"]
    with wb.lock:
        wb.add_sheet(name)
        wb.save()
    return web.json_response({"name": name})


async def remove_sheet(request):
    store: Store = request.app["store"]
    wb = workbook_or_404(store, request.match_info["id"])
    if not wb:
        return err_response(404, "NotFound")
    name = request.match_info["name"]
    with wb.lock:
        ok = wb.remove_sheet(name)
        if ok:
            wb.save()
    return web.json_response({"ok": ok})


# ----- cells ---------------------------------------------------------


async def patch_cells(request):
    store: Store = request.app["store"]
    wb = workbook_or_404(store, request.match_info["id"])
    if not wb:
        return err_response(404, "NotFound")
    locale = request.query.get("locale")
    body = await request.json()
    patches = body.get("patches", [])
    # validate sheets present
    for p in patches:
        if p.get("sheet") not in wb.sheets:
            return err_response(404, "NotFound", f"unknown sheet {p.get('sheet')!r}")
    try:
        cells = wb.patch_cells(patches, locale=locale)
    except XLError as e:
        code = getattr(e, "code", None)
        if code in ("ParseError", "SpillTargetWriteError", "ValidationError"):
            return err_response(400, code)
        return err_response(400, "ParseError")
    except KeyError as e:
        return err_response(404, "NotFound", str(e))
    wb.save()
    return web.json_response({"cells": cells})


async def get_cell(request):
    store: Store = request.app["store"]
    wb = workbook_or_404(store, request.match_info["id"])
    if not wb:
        return err_response(404, "NotFound")
    sheet = request.match_info["sheet"]
    ref = request.match_info["ref"]
    if sheet not in wb.sheets:
        return err_response(404, "NotFound")
    return web.json_response(wb.cell_dict(sheet, normalize_ref(ref)))


# ----- fill ----------------------------------------------------------


async def fill(request):
    store: Store = request.app["store"]
    wb = workbook_or_404(store, request.match_info["id"])
    if not wb:
        return err_response(404, "NotFound")
    body = await request.json()
    sheet = body["sheet"]
    src = body["source"]
    targets = body["targets"]
    if sheet not in wb.sheets:
        return err_response(404, "NotFound")
    src_cell = wb.sheets[sheet].cells.get(normalize_ref(src))
    if not src_cell or src_cell.raw_input is None:
        return web.json_response({"cells": []})
    tgt_refs = expand_range(targets)
    src_c, src_r, _, _ = parse_a1(src)
    patches = []
    for tref in tgt_refs:
        tc, tr, _, _ = parse_a1(tref)
        delta_c = tc - src_c
        delta_r = tr - src_r
        new_input = src_cell.raw_input
        if isinstance(new_input, str) and new_input.startswith("="):
            new_input = "=" + _shift_formula(new_input[1:], delta_c, delta_r, sheet)
        patches.append({"sheet": sheet, "ref": tref, "input": new_input,
                        "format": src_cell.format, "style": src_cell.style})
    cells = wb.patch_cells(patches)
    wb.save()
    return web.json_response({"cells": cells})


def _shift_formula(body: str, dc: int, dr: int, current_sheet: str) -> str:
    """Rewrite cell references inside a formula body, applying (dc, dr).

    Returns the rewritten body. Honours `$` pins.
    """
    try:
        ast = parse_formula(body)
    except ParseError:
        return body
    new = _shift_node(ast, dc, dr)
    return _formula_to_str(new)


def _shift_node(n, dc, dr):
    if isinstance(n, RefNode):
        new_c = n.col if n.col_abs else n.col + dc
        new_r = n.row if n.row_abs else n.row + dr
        return RefNode(n.sheet, new_c, new_r, n.col_abs, n.row_abs)
    if isinstance(n, RangeNode):
        c0 = n.c0 if n.c0_abs else n.c0 + dc
        r0 = n.r0 if n.r0_abs else n.r0 + dr
        c1 = n.c1 if n.c1_abs else n.c1 + dc
        r1 = n.r1 if n.r1_abs else n.r1 + dr
        return RangeNode(n.sheet, c0, r0, c1, r1, n.c0_abs, n.r0_abs, n.c1_abs, n.r1_abs)
    if isinstance(n, BinOp):
        return BinOp(n.op, _shift_node(n.left, dc, dr), _shift_node(n.right, dc, dr))
    if isinstance(n, UnaryOp):
        return UnaryOp(n.op, _shift_node(n.operand, dc, dr))
    if isinstance(n, PercentOp):
        return PercentOp(_shift_node(n.operand, dc, dr))
    if isinstance(n, Call):
        return Call(n.name, [_shift_node(a, dc, dr) for a in n.args])
    if isinstance(n, CallExpr):
        return CallExpr(_shift_node(n.fn, dc, dr), [_shift_node(a, dc, dr) for a in n.args])
    return n


def _formula_to_str(n) -> str:
    if isinstance(n, Num):
        v = n.value
        if isinstance(v, float) and v.is_integer():
            return str(int(v))
        return str(v)
    if isinstance(n, Str):
        return '"' + n.value.replace('"', '""') + '"'
    if isinstance(n, Bool):
        return "TRUE" if n.value else "FALSE"
    if isinstance(n, Err):
        return n.value
    if isinstance(n, NameNode):
        return n.name
    if isinstance(n, RefNode):
        col_pref = "$" if n.col_abs else ""
        row_pref = "$" if n.row_abs else ""
        local = f"{col_pref}{col_index_to_letters(n.col)}{row_pref}{n.row+1}"
        if n.sheet:
            return _q_sheet(n.sheet) + "!" + local
        return local
    if isinstance(n, RangeNode):
        cp0 = "$" if n.c0_abs else ""; rp0 = "$" if n.r0_abs else ""
        cp1 = "$" if n.c1_abs else ""; rp1 = "$" if n.r1_abs else ""
        local = (f"{cp0}{col_index_to_letters(n.c0)}{rp0}{n.r0+1}:"
                 f"{cp1}{col_index_to_letters(n.c1)}{rp1}{n.r1+1}")
        if n.sheet:
            return _q_sheet(n.sheet) + "!" + local
        return local
    if isinstance(n, BinOp):
        l = _formula_to_str(n.left); r = _formula_to_str(n.right)
        return f"({l}{n.op}{r})"
    if isinstance(n, UnaryOp):
        return f"({n.op}{_formula_to_str(n.operand)})"
    if isinstance(n, PercentOp):
        return f"({_formula_to_str(n.operand)}%)"
    if isinstance(n, Call):
        return f"{n.name}(" + ",".join(_formula_to_str(a) for a in n.args) + ")"
    if isinstance(n, CallExpr):
        return f"{_formula_to_str(n.fn)}(" + ",".join(_formula_to_str(a) for a in n.args) + ")"
    return ""


def _q_sheet(name: str) -> str:
    if re.fullmatch(r"[A-Za-z_][\w]*", name):
        return name
    return "'" + name.replace("'", "''") + "'"


# ----- sort / filter --------------------------------------------------


async def sort_range(request):
    store: Store = request.app["store"]
    wb = workbook_or_404(store, request.match_info["id"])
    if not wb:
        return err_response(404, "NotFound")
    body = await request.json()
    sheet = body["sheet"]; rng = body["range"]
    by = body.get("by", [])
    if sheet not in wb.sheets:
        return err_response(404, "NotFound")
    c0, r0, c1, r1 = parse_range(rng)
    sh = wb.sheets[sheet]
    rows = []
    for r in range(r0, r1 + 1):
        row = []
        for c in range(c0, c1 + 1):
            cell = sh.cells.get(make_a1(c, r))
            row.append(cell.raw_input if cell else None)
        rows.append(row)
    def key_for(row):
        keys = []
        for spec in by:
            ci = int(spec.get("column", 0))
            v = row[ci] if ci < len(row) else None
            asc = spec.get("asc", True)
            try:
                vv = float(v) if v is not None else float("inf")
                keys.append((0, vv if asc else -vv))
            except (TypeError, ValueError):
                s = "" if v is None else str(v)
                keys.append((1, s if asc else _neg_str(s)))
        return tuple(keys)
    def _neg_str(s):
        return tuple(-ord(c) for c in s)
    # apply asc/desc by reversing per-spec — easier with multi-pass stable sort
    # Sort with the last key first
    indices = list(range(len(rows)))
    for spec in reversed(by):
        ci = int(spec.get("column", 0))
        asc = spec.get("asc", True)
        def _k(i, ci=ci):
            v = rows[i][ci] if ci < len(rows[i]) else None
            try:
                return (0, float(v))
            except (TypeError, ValueError):
                return (1, "" if v is None else str(v))
        indices.sort(key=_k, reverse=not asc)
    new_rows = [rows[i] for i in indices]
    patches = []
    for ri, row in enumerate(new_rows):
        for ci, val in enumerate(row):
            patches.append({"sheet": sheet, "ref": make_a1(c0 + ci, r0 + ri),
                            "input": val})
    cells = wb.patch_cells(patches)
    wb.save()
    return web.json_response({"cells": cells})


async def filter_range(request):
    store: Store = request.app["store"]
    wb = workbook_or_404(store, request.match_info["id"])
    if not wb:
        return err_response(404, "NotFound")
    body = await request.json()
    sheet = body["sheet"]; rng = body["range"]
    crit = body.get("criteria") or {}
    if sheet not in wb.sheets:
        return err_response(404, "NotFound")
    c0, r0, c1, r1 = parse_range(rng)
    sh = wb.sheets[sheet]
    out_rows = []
    ci = int(crit.get("column", 0))
    expr = crit.get("expr") or ""
    pred = _filter_pred(expr)
    for r in range(r0, r1 + 1):
        cells = []
        col_vals = []
        for c in range(c0, c1 + 1):
            cell = wb.cell_dict(sheet, make_a1(c, r))
            cells.append(cell)
            col_vals.append(cell.get("value"))
        v = col_vals[ci] if ci < len(col_vals) else None
        if pred(v):
            out_rows.append({"row": r, "cells": cells})
    return web.json_response({"rows": out_rows})


def _filter_pred(expr: str):
    expr = (expr or "").strip()
    if not expr:
        return lambda v: True
    m = re.match(r"^(<>|<=|>=|<|>|=)?(.*)$", expr, re.S)
    op, rest = m.group(1) or "=", m.group(2).strip()
    rest_num = None
    try: rest_num = float(rest)
    except (TypeError, ValueError): pass
    def num_pred(cmp):
        target = rest_num
        def _p(v):
            try:
                return cmp(float(v), target)
            except (TypeError, ValueError):
                return False
        return _p
    if rest_num is not None:
        if op == "=": return num_pred(lambda a, b: a == b)
        if op == "<>": return num_pred(lambda a, b: a != b)
        if op == "<": return num_pred(lambda a, b: a < b)
        if op == "<=": return num_pred(lambda a, b: a <= b)
        if op == ">": return num_pred(lambda a, b: a > b)
        if op == ">=": return num_pred(lambda a, b: a >= b)
    # text with wildcard
    if op in ("=", "<>"):
        rx = _wildcard_regex(rest)
        if op == "=":
            return lambda v: bool(rx.match("" if v is None else str(v)))
        return lambda v: not rx.match("" if v is None else str(v))
    return lambda v: True


def _wildcard_regex(s):
    out = []
    i = 0
    while i < len(s):
        c = s[i]
        if c == '~' and i + 1 < len(s) and s[i+1] in "*?":
            out.append(re.escape(s[i+1])); i += 2; continue
        if c == '*': out.append('.*')
        elif c == '?': out.append('.')
        else: out.append(re.escape(c))
        i += 1
    return re.compile("^" + "".join(out) + "$", re.IGNORECASE)


# ----- CSV -----------------------------------------------------------


async def export_csv(request):
    store: Store = request.app["store"]
    wb = workbook_or_404(store, request.match_info["id"])
    if not wb:
        return err_response(404, "NotFound")
    sheet = request.query.get("sheet")
    if sheet not in wb.sheets:
        return err_response(404, "NotFound")
    sh = wb.sheets[sheet]
    if not sh.cells:
        return web.Response(text="", content_type="text/csv")
    max_c = max(parse_a1(r)[0] for r in sh.cells.keys())
    max_r = max(parse_a1(r)[1] for r in sh.cells.keys())
    buf = io.StringIO()
    w = csv.writer(buf)
    for r in range(max_r + 1):
        row = []
        for c in range(max_c + 1):
            cell = sh.cells.get(make_a1(c, r))
            if cell is None:
                row.append("")
            else:
                v = cell.value
                if v is None: row.append("")
                elif isinstance(v, bool): row.append("TRUE" if v else "FALSE")
                else: row.append(str(v) if not isinstance(v, float) else (str(int(v)) if v.is_integer() else str(v)))
        w.writerow(row)
    return web.Response(text=buf.getvalue(), content_type="text/csv")


async def import_csv(request):
    store: Store = request.app["store"]
    wb = workbook_or_404(store, request.match_info["id"])
    if not wb:
        return err_response(404, "NotFound")
    sheet = request.query.get("sheet")
    if sheet not in wb.sheets:
        return err_response(404, "NotFound")
    reader = await request.multipart()
    text = ""
    async for part in reader:
        if part.name == "file":
            data = await part.read()
            text = data.decode("utf-8", errors="replace")
            break
    # clear existing sheet
    sh = wb.sheets[sheet]
    sh.cells.clear()
    # also clear deps for this sheet
    for k in list(wb.reads.keys()):
        if k[0] == sheet:
            wb._clear_reads(k)
    # parse rows
    rows = list(csv.reader(io.StringIO(text)))
    patches = []
    for ri, row in enumerate(rows):
        for ci, val in enumerate(row):
            ref = make_a1(ci, ri)
            patches.append({"sheet": sheet, "ref": ref, "input": val if val != "" else None})
    if patches:
        try:
            wb.patch_cells(patches)
        except XLError:
            pass
    wb.save()
    return web.json_response({"ok": True, "rows": len(rows)})


# ----- XLSX ----------------------------------------------------------


async def export_xlsx_route(request):
    store: Store = request.app["store"]
    wb = workbook_or_404(store, request.match_info["id"])
    if not wb:
        return err_response(404, "NotFound")
    blob = export_xlsx(wb)
    return web.Response(body=blob,
                        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


async def import_xlsx_route(request):
    store: Store = request.app["store"]
    wb = workbook_or_404(store, request.match_info["id"])
    if not wb:
        return err_response(404, "NotFound")
    reader = await request.multipart()
    data = b""
    async for part in reader:
        if part.name == "file":
            data = await part.read()
            break
    import_xlsx(wb, data)
    wb.save()
    return web.json_response(workbook_summary(wb))


# ----- defined names -------------------------------------------------


async def list_names(request):
    store: Store = request.app["store"]
    wb = workbook_or_404(store, request.match_info["id"])
    if not wb:
        return err_response(404, "NotFound")
    return web.json_response({"names": list(wb.names.values())})


async def add_name(request):
    store: Store = request.app["store"]
    wb = workbook_or_404(store, request.match_info["id"])
    if not wb:
        return err_response(404, "NotFound")
    body = await request.json()
    scope = body.get("scope", "workbook")
    sheet = body.get("sheet")
    name = body["name"]
    rng = body["range"]
    with wb.lock:
        wb.add_name(scope, sheet, name, rng)
        # recompute everything (formulas may now reference this name)
        for s_name, sh in wb.sheets.items():
            for ref in sh.cells.keys():
                wb._recompute({(s_name, ref)})
        wb.save()
    return web.json_response({"ok": True})


# ----- conditional formats ------------------------------------------


async def add_cf(request):
    store: Store = request.app["store"]
    wb = workbook_or_404(store, request.match_info["id"])
    if not wb:
        return err_response(404, "NotFound")
    body = await request.json()
    wb.conditional_formats.append({
        "sheet": body["sheet"],
        "range": body["range"],
        "rule": body["rule"],
    })
    wb.save()
    return web.json_response({"ok": True})


# ----- data validation ----------------------------------------------


async def add_validation(request):
    store: Store = request.app["store"]
    wb = workbook_or_404(store, request.match_info["id"])
    if not wb:
        return err_response(404, "NotFound")
    body = await request.json()
    wb.data_validations.append({
        "sheet": body["sheet"],
        "range": body["range"],
        "rule": body["rule"],
    })
    wb.save()
    return web.json_response({"ok": True})


# ----- pivot --------------------------------------------------------


async def pivot(request):
    store: Store = request.app["store"]
    wb = workbook_or_404(store, request.match_info["id"])
    if not wb:
        return err_response(404, "NotFound")
    body = await request.json()
    src = body["source"]
    rows_keys = body.get("rows", []) or []
    cols_keys = body.get("cols", []) or []
    values = body.get("values", {}) or {}
    sheet, local = split_sheet_ref(src)
    if not sheet or sheet not in wb.sheets:
        return err_response(404, "NotFound")
    sh = wb.sheets[sheet]
    c0, r0, c1, r1 = parse_range(local)
    # header row + data rows
    header = []
    for c in range(c0, c1 + 1):
        cell = sh.cells.get(make_a1(c, r0))
        header.append(cell.value if cell else None)
    rows = []
    for r in range(r0 + 1, r1 + 1):
        row = []
        for c in range(c0, c1 + 1):
            cell = sh.cells.get(make_a1(c, r))
            row.append(cell.value if cell else None)
        rows.append(row)
    # group
    def col_idx(name):
        try: return header.index(name)
        except ValueError: return -1
    row_idxs = [col_idx(k) for k in rows_keys]
    col_idxs = [col_idx(k) for k in cols_keys]
    cells_out = []
    grouped: Dict[tuple, Dict[tuple, list]] = {}
    for r in rows:
        rk = tuple(r[i] for i in row_idxs)
        ck = tuple(r[i] for i in col_idxs)
        grouped.setdefault(rk, {}).setdefault(ck, []).append(r)
    # ordering
    rks = sorted(grouped.keys(), key=lambda x: tuple(_pivot_sort(v) for v in x))
    cks = sorted({ck for inner in grouped.values() for ck in inner.keys()},
                 key=lambda x: tuple(_pivot_sort(v) for v in x))
    cells = []
    for rk in rks:
        for ck in cks if cks else [tuple()]:
            inner = grouped[rk].get(ck, [])
            for col_name, agg in values.items():
                ci = col_idx(col_name)
                vals = [row[ci] for row in inner]
                v = _pivot_agg(agg, vals)
                cells.append({"row": list(rk), "col": list(ck), "agg": agg, "field": col_name, "value": v})
    return web.json_response({"cells": cells})


def _pivot_sort(v):
    if v is None: return (3,)
    if isinstance(v, (int, float)) and not isinstance(v, bool): return (0, v)
    if isinstance(v, str): return (1, v)
    return (2, str(v))


def _pivot_agg(agg, vals):
    nums = [float(v) for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool)]
    agg = agg.lower()
    if agg == "sum": return sum(nums)
    if agg in ("avg", "average", "mean"): return (sum(nums) / len(nums)) if nums else 0
    if agg == "count": return sum(1 for v in vals if v not in (None, ""))
    if agg == "min": return min(nums) if nums else 0
    if agg == "max": return max(nums) if nums else 0
    return None


# ----- iterative settings + goal seek -------------------------------


async def get_settings(request):
    store: Store = request.app["store"]
    wb = workbook_or_404(store, request.match_info["id"])
    if not wb:
        return err_response(404, "NotFound")
    return web.json_response(wb.settings)


async def put_settings(request):
    store: Store = request.app["store"]
    wb = workbook_or_404(store, request.match_info["id"])
    if not wb:
        return err_response(404, "NotFound")
    body = await request.json()
    if "iterative_calc" in body:
        wb.settings["iterative_calc"] = {
            **wb.settings.get("iterative_calc", {}),
            **(body["iterative_calc"] or {}),
        }
    if "locale" in body:
        wb.settings["locale"] = body["locale"]
    # re-eval everything (toggling iter mode changes #CIRC! behaviour)
    with wb.lock:
        wb.recompute_all()
        wb.save()
    return web.json_response(wb.settings)


async def goal_seek(request):
    from .errors import XLError as _XL
    store: Store = request.app["store"]
    wb = workbook_or_404(store, request.match_info["id"])
    if not wb:
        return err_response(404, "NotFound")
    body = await request.json()
    target = body["target_cell"]
    target_value = float(body["target_value"])
    changing = body["changing_cell"]
    tol = float(body.get("tol", 1e-6))
    max_iter = int(body.get("max_iter", 100))
    t_sheet, t_ref = split_sheet_ref(target)
    c_sheet, c_ref = split_sheet_ref(changing)
    if not t_sheet or not c_sheet or t_sheet not in wb.sheets or c_sheet not in wb.sheets:
        return err_response(404, "NotFound")
    t_ref = normalize_ref(t_ref); c_ref = normalize_ref(c_ref)

    def f(x):
        return _eval_with_override(wb, c_sheet, c_ref, x, t_sheet, t_ref) - target_value

    # bisection / secant hybrid
    x0 = wb.sheets[c_sheet].cells.get(c_ref)
    x0 = float(x0.value) if x0 and isinstance(x0.value, (int, float)) else 0.0
    x1 = x0 + 1.0 if x0 == 0 else x0 * 1.1
    try:
        y0 = f(x0); y1 = f(x1)
    except XLError:
        return err_response(400, "NotConverged")
    converged = False
    final_x = x0
    for _ in range(max_iter):
        if abs(y1) < tol:
            final_x = x1; converged = True; break
        if y1 == y0:
            x1 = x1 + 1e-3
            try:
                y1 = f(x1)
            except XLError:
                break
            continue
        x2 = x1 - y1 * (x1 - x0) / (y1 - y0)
        if not math.isfinite(x2) or abs(x2) > 1e18:
            break
        try:
            y2 = f(x2)
        except XLError:
            break
        x0, y0 = x1, y1
        x1, y1 = x2, y2
        if abs(y1) < tol:
            final_x = x1; converged = True; break
    if not converged:
        return err_response(400, "NotConverged")
    # commit changing cell
    cells = wb.patch_cells([{"sheet": c_sheet, "ref": c_ref, "input": str(final_x)}])
    out_value = wb.sheets[t_sheet].cells.get(t_ref).value
    wb.save()
    return web.json_response({"converged": True, "iterations": max_iter,
                              "input": final_x, "output": out_value, "cells": cells})


def _eval_with_override(wb: Workbook, c_sheet, c_ref, x, t_sheet, t_ref):
    """Evaluate the target cell with the changing cell overridden to x.
    Doesn't mutate the workbook."""
    overrides = {(c_sheet, c_ref): x}
    # find dependents transitively from changing → target via dep graph
    # naive: re-evaluate everything that depends on the changing cell, store
    # results in `overrides`, then return target value.
    closure = set()
    stack = [(c_sheet, c_ref)]
    while stack:
        k = stack.pop()
        if k in closure: continue
        closure.add(k)
        for d in wb.dependents.get(k, ()):
            if d not in closure:
                stack.append(d)
    order = wb._topo(closure) or list(closure)
    from .eval import Evaluator
    for s, r in order:
        if (s, r) == (c_sheet, c_ref):
            continue
        sh = wb.sheets.get(s)
        cell = sh.cells.get(r) if sh else None
        if cell is None or not cell.is_formula:
            continue
        ev = Evaluator(wb, s, anchor_ref=r, iter_values=overrides)
        try:
            v = ev.eval_node(cell.formula)
        except XLError:
            v = "#N/A"
        if isinstance(v, list):
            v = v[0][0] if v and isinstance(v[0], list) else (v[0] if v else 0)
        overrides[(s, r)] = v
    val = overrides.get((t_sheet, t_ref))
    if val is None:
        cell = wb.sheets[t_sheet].cells.get(t_ref)
        val = cell.value if cell else 0
    if not isinstance(val, (int, float)) or isinstance(val, bool):
        raise XLError("#N/A")
    return float(val)


# ----- WebSocket collab --------------------------------------------


async def collab_ws(request):
    store: Store = request.app["store"]
    wb = workbook_or_404(store, request.match_info["id"])
    if not wb:
        return web.Response(status=404)
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    actor = None
    sessions = request.app["sessions"]
    sessions.setdefault(wb.id, [])
    sessions[wb.id].append(ws)
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                m = json.loads(msg.data)
            except Exception:
                continue
            t = m.get("type")
            if t == "hello":
                actor = m.get("actor")
                ws.actor = actor
                snapshot = workbook_summary(wb)
                await ws.send_json({"type": "welcome", "seq": wb.seq, "snapshot": snapshot})
                since = m.get("since_seq")
                if since is not None:
                    for ev in wb.collab_log:
                        if ev["seq"] > int(since):
                            await ws.send_json(ev)
            elif t == "op":
                op = m.get("op", {})
                client_seq = m.get("client_seq")
                try:
                    cells = _apply_collab_op(wb, op)
                    wb.seq += 1
                    seq = wb.seq
                    ack = {"type": "ack", "client_seq": client_seq, "seq": seq}
                    await ws.send_json(ack)
                    ev = {"type": "event", "seq": seq, "actor": actor,
                          "client_seq": client_seq, "op": op, "cells": cells}
                    wb.collab_log.append(ev)
                    await _broadcast(sessions[wb.id], ev)
                    wb.save()
                except XLError as e:
                    await ws.send_json({"type": "error", "client_seq": client_seq,
                                        "error": getattr(e, "code", "ParseError")})
            elif t == "presence":
                ev = {"type": "presence_event", "actor": actor,
                      "sheet": m.get("sheet"), "ref": m.get("ref"),
                      "online": True}
                await _broadcast(sessions[wb.id], ev)
    finally:
        sessions[wb.id].remove(ws)
        if actor:
            ev = {"type": "presence_event", "actor": actor, "online": False}
            await _broadcast(sessions[wb.id], ev)
    return ws


def _apply_collab_op(wb: Workbook, op: dict):
    kind = op.get("kind")
    if kind == "set":
        return wb.patch_cells([{"sheet": op["sheet"], "ref": op["ref"], "input": op.get("input")}])
    if kind == "clear":
        return wb.patch_cells([{"sheet": op["sheet"], "ref": op["ref"], "input": None}])
    if kind == "add_sheet":
        wb.add_sheet(op["name"])
        return []
    if kind == "remove_sheet":
        wb.remove_sheet(op["name"])
        return []
    return []


async def _broadcast(ws_list, payload):
    text = json.dumps(payload)
    for w in list(ws_list):
        try:
            await w.send_str(text)
        except Exception:
            pass


# ----- SPA -----------------------------------------------------------


async def index(request):
    return web.FileResponse("/app/server/static/index.html")


# ----- app builder ---------------------------------------------------


def build_app() -> web.Application:
    app = web.Application(client_max_size=64 * 1024 * 1024)
    app["store"] = Store()
    app["sessions"] = {}

    app.router.add_get("/", index)
    app.router.add_static("/static", "/app/server/static")

    app.router.add_get("/api/health", health)
    app.router.add_get("/api/workbooks", list_workbooks)
    app.router.add_post("/api/workbooks", create_workbook)
    app.router.add_get("/api/workbooks/{id}", get_workbook)
    app.router.add_delete("/api/workbooks/{id}", delete_workbook)

    app.router.add_post("/api/workbooks/{id}/sheets", add_sheet)
    app.router.add_delete("/api/workbooks/{id}/sheets/{name}", remove_sheet)

    app.router.add_post("/api/workbooks/{id}/cells", patch_cells)
    app.router.add_get("/api/workbooks/{id}/cells/{sheet}/{ref}", get_cell)
    app.router.add_post("/api/workbooks/{id}/fill", fill)
    app.router.add_post("/api/workbooks/{id}/sort", sort_range)
    app.router.add_post("/api/workbooks/{id}/filter", filter_range)

    app.router.add_get("/api/workbooks/{id}/csv", export_csv)
    app.router.add_post("/api/workbooks/{id}/csv", import_csv)
    app.router.add_get("/api/workbooks/{id}/xlsx", export_xlsx_route)
    app.router.add_post("/api/workbooks/{id}/xlsx", import_xlsx_route)

    app.router.add_get("/api/workbooks/{id}/names", list_names)
    app.router.add_post("/api/workbooks/{id}/names", add_name)
    app.router.add_post("/api/workbooks/{id}/conditional_formats", add_cf)
    app.router.add_post("/api/workbooks/{id}/data_validation", add_validation)
    app.router.add_post("/api/workbooks/{id}/pivot", pivot)

    app.router.add_get("/api/workbooks/{id}/settings", get_settings)
    app.router.add_put("/api/workbooks/{id}/settings", put_settings)
    app.router.add_post("/api/workbooks/{id}/goal_seek", goal_seek)

    app.router.add_get("/api/workbooks/{id}/collab", collab_ws)

    return app
