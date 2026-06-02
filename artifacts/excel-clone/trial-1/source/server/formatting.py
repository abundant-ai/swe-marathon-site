"""Excel number-format string evaluator. Best-effort; covers the cases we need."""
from __future__ import annotations
import datetime as _dt
import math
import re


_EPOCH = _dt.date(1899, 12, 30)


def _is_date_format(fmt: str) -> bool:
    f = re.sub(r'"[^"]*"', '', fmt).lower()
    if 'y' in f or 'd' in f:
        return True
    if 'mm/' in f or 'mm-' in f or '/m' in f or '-m' in f:
        return True
    return False


def _split_sections(fmt: str):
    parts = []
    cur = []
    in_q = False
    in_b = 0
    i = 0
    while i < len(fmt):
        c = fmt[i]
        if c == '"':
            in_q = not in_q; cur.append(c); i += 1; continue
        if c == '[':
            in_b += 1; cur.append(c); i += 1; continue
        if c == ']':
            in_b -= 1; cur.append(c); i += 1; continue
        if c == '\\' and i + 1 < len(fmt):
            cur.append(c); cur.append(fmt[i+1]); i += 2; continue
        if c == ';' and not in_q and in_b == 0:
            parts.append(''.join(cur)); cur = []; i += 1; continue
        cur.append(c); i += 1
    parts.append(''.join(cur))
    return parts


def _serial_to_dt(n: float) -> _dt.datetime:
    days = int(math.floor(n))
    frac = n - days
    base = _EPOCH + _dt.timedelta(days=days)
    secs = int(round(frac * 86400))
    return _dt.datetime(base.year, base.month, base.day) + _dt.timedelta(seconds=secs)


def _format_date(value, fmt: str) -> str:
    if isinstance(value, _dt.datetime):
        d = value
    elif isinstance(value, _dt.date):
        d = _dt.datetime(value.year, value.month, value.day)
    else:
        d = _serial_to_dt(float(value))
    out = []
    i = 0
    while i < len(fmt):
        c = fmt[i]
        if c == '"':
            j = fmt.find('"', i + 1)
            if j < 0: j = len(fmt)
            out.append(fmt[i+1:j]); i = j + 1; continue
        if c == '\\' and i + 1 < len(fmt):
            out.append(fmt[i+1]); i += 2; continue
        rest = fmt[i:].lower()
        for tok, fn in (
            ("yyyy", lambda d: f"{d.year:04d}"),
            ("yy",   lambda d: f"{d.year%100:02d}"),
            ("mmmm", lambda d: d.strftime("%B")),
            ("mmm",  lambda d: d.strftime("%b")),
            ("mm",   lambda d: f"{d.month:02d}"),
            ("dd",   lambda d: f"{d.day:02d}"),
            ("hh",   lambda d: f"{d.hour:02d}"),
            ("ss",   lambda d: f"{d.second:02d}"),
            ("am/pm",lambda d: "AM" if d.hour < 12 else "PM"),
            ("m",    lambda d: str(d.month)),
            ("d",    lambda d: str(d.day)),
            ("h",    lambda d: str(d.hour)),
            ("s",    lambda d: str(d.second)),
        ):
            if rest.startswith(tok):
                out.append(fn(d)); i += len(tok); break
        else:
            out.append(c); i += 1
    return ''.join(out)


def _classify_section(sec: str):
    """Tokenize a number section into a list of (kind, text) tokens.

    kind ∈ {'lit', 'hole', 'point', 'comma', 'pct'}.
    """
    out = []
    i = 0
    while i < len(sec):
        c = sec[i]
        if c == '"':
            j = sec.find('"', i + 1)
            if j < 0: j = len(sec)
            out.append(('lit', sec[i+1:j])); i = j + 1; continue
        if c == '\\' and i + 1 < len(sec):
            out.append(('lit', sec[i+1])); i += 2; continue
        if c in '0#?':
            out.append(('hole', c)); i += 1; continue
        if c == '.':
            out.append(('point', c)); i += 1; continue
        if c == ',':
            out.append(('comma', c)); i += 1; continue
        if c == '%':
            out.append(('pct', c)); i += 1; continue
        out.append(('lit', c)); i += 1
    return out


def _format_number(value: float, fmt: str) -> str:
    sections = _split_sections(fmt)
    sign = ""
    if value < 0 and len(sections) >= 2:
        sec = sections[1]; v = abs(value); explicit_neg = True
    elif value == 0 and len(sections) >= 3:
        sec = sections[2]; v = value; explicit_neg = False
    else:
        sec = sections[0]; v = value; explicit_neg = False
        if value < 0:
            sign = "-"; v = abs(value)
    tokens = _classify_section(sec)

    pct_count = sum(1 for t in tokens if t[0] == 'pct')
    if pct_count:
        v *= (100 ** pct_count)

    # split tokens at the decimal point (only the first 'point')
    split_idx = next((k for k, t in enumerate(tokens) if t[0] == 'point'), -1)
    if split_idx >= 0:
        int_toks = tokens[:split_idx]
        dec_toks = tokens[split_idx+1:]
    else:
        int_toks = tokens
        dec_toks = []

    # detect grouping: comma between two holes within int_toks
    grouping = False
    int_holes_positions = [k for k, t in enumerate(int_toks) if t[0] == 'hole']
    for k, t in enumerate(int_toks):
        if t[0] == 'comma' and any(k - 1 == h for h in int_holes_positions) and any(k + 1 == h for h in int_holes_positions):
            grouping = True
            break

    int_hole_count = sum(1 for t in int_toks if t[0] == 'hole')
    dec_hole_count = sum(1 for t in dec_toks if t[0] == 'hole')

    # round
    if dec_hole_count == 0:
        s_int = f"{round(v):.0f}"
        s_dec = ""
    else:
        s = f"{v:.{dec_hole_count}f}"
        s_int, s_dec = s.split('.')

    zero_count = sum(1 for t in int_toks if t[0] == 'hole' and t[1] == '0')
    if len(s_int) < zero_count:
        s_int = "0" * (zero_count - len(s_int)) + s_int

    if grouping:
        digits = list(s_int)
        out = []
        for i, dch in enumerate(reversed(digits)):
            if i and i % 3 == 0:
                out.append(',')
            out.append(dch)
        s_int = ''.join(reversed(out))

    # render the integer side, right-to-left
    rev_int = list(reversed(int_toks))
    rev_consume = list(s_int)  # we will pop from the right
    rendered_int_rev = []
    def _pop_digit():
        # skip past any thousands-separator chars when consuming digits
        while rev_consume and not rev_consume[-1].isdigit():
            sep = rev_consume.pop()
            rendered_int_rev.append(sep)
        if rev_consume:
            return rev_consume.pop()
        return None
    for k, t in enumerate(rev_int):
        kind, text = t
        if kind == 'hole':
            d = _pop_digit()
            if d is not None:
                rendered_int_rev.append(d)
            else:
                if text == '0':
                    rendered_int_rev.append('0')
                elif text == '?':
                    rendered_int_rev.append(' ')
                # '#' empty
        elif kind == 'comma':
            if not grouping:
                rendered_int_rev.append(',')
        elif kind == 'pct':
            rendered_int_rev.append('%')
        elif kind == 'point':
            pass
        else:
            rendered_int_rev.append(text)
    rendered_int = ''.join(reversed(rendered_int_rev))
    leftover = ''.join(rev_consume)
    if leftover:
        # find leftmost digit position; prepend leftover before it
        m = re.search(r'\d', rendered_int)
        if m:
            rendered_int = rendered_int[:m.start()] + leftover + rendered_int[m.start():]
        else:
            rendered_int = leftover + rendered_int

    # render decimal side
    rendered_dec = []
    di = 0
    for kind, text in dec_toks:
        if kind == 'hole':
            if di < len(s_dec):
                rendered_dec.append(s_dec[di]); di += 1
            elif text == '0':
                rendered_dec.append('0')
            elif text == '?':
                rendered_dec.append(' ')
        elif kind == 'comma':
            rendered_dec.append(',')
        elif kind == 'pct':
            rendered_dec.append('%')
        else:
            rendered_dec.append(text)
    dec_str = ''.join(rendered_dec)

    if dec_toks:
        return f"{sign}{rendered_int}.{dec_str}"
    return f"{sign}{rendered_int}"


def format_value(value, fmt: str | None) -> str:
    if fmt is None or fmt == "":
        return _default_display(value)
    if value is None or value == "":
        return ""
    if isinstance(value, str) and value.startswith("#"):
        return value
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if fmt.lower() == "general":
        return _default_display(value)
    if isinstance(value, str):
        if "@" in fmt:
            return fmt.replace("@", value)
        return value
    if _is_date_format(fmt):
        return _format_date(value, fmt)
    return _format_number(float(value), fmt)


def _default_display(v) -> str:
    if v is None: return ""
    if isinstance(v, bool): return "TRUE" if v else "FALSE"
    if isinstance(v, float):
        if v.is_integer(): return str(int(v))
        return f"{v:.10g}"
    if isinstance(v, int): return str(v)
    return str(v)
