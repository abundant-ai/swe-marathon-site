"""Excel number-format renderer (subset)."""
import re, math, datetime as _dt
from values import is_err, serial_to_date, serial_to_datetime, EPOCH

def apply_format(v, fmt):
    if v is None or v == '': return ''
    if is_err(v): return v
    if not fmt or fmt == 'General':
        return _general(v)
    if isinstance(v, bool):
        return 'TRUE' if v else 'FALSE'
    # split on ; for positive;negative;zero;text
    parts = _split_format(fmt)
    if isinstance(v, str):
        if len(parts) >= 4: return _format_text(v, parts[3])
        return v
    n = float(v)
    if n > 0 or (n == 0 and len(parts) < 3):
        f = parts[0]
    elif n < 0 and len(parts) >= 2:
        f = parts[1]; n = -n
    elif n == 0 and len(parts) >= 3:
        f = parts[2]
    else:
        f = parts[0]
    return _format_number(n, f)

def _general(v):
    if isinstance(v, bool): return 'TRUE' if v else 'FALSE'
    if isinstance(v, (int, float)):
        if isinstance(v, float) and v.is_integer():
            return str(int(v))
        if isinstance(v, float):
            s = repr(v)
            return s
        return str(v)
    return str(v)

def _split_format(fmt):
    parts = []
    cur = ''
    in_q = False
    in_b = False
    for c in fmt:
        if c == '"': in_q = not in_q; cur += c
        elif c == '[' and not in_q: in_b = True; cur += c
        elif c == ']' and not in_q: in_b = False; cur += c
        elif c == ';' and not in_q and not in_b:
            parts.append(cur); cur = ''
        else: cur += c
    parts.append(cur)
    return parts

def _format_text(v, fmt):
    return fmt.replace('@', v)

def _is_date_format(fmt):
    # crude: contains y/m/d/h/s tokens outside quotes
    s = re.sub(r'"[^"]*"', '', fmt).lower()
    return any(t in s for t in ['yyyy','yy','mmm','mm','dd','d','h','s']) and 'y' in s or 'd' in s and ('y' in s or 'm' in s) or 'h' in s

DATE_TOKENS_RE = re.compile(r'(yyyy|yy|mmmm|mmm|mm|m|dddd|ddd|dd|d|hh|h|ss|s|AM/PM|am/pm)')

def _format_number(n, fmt):
    # remove and capture quoted parts
    quoted = []
    def qrep(m):
        quoted.append(m.group(1)); return f'\x00{len(quoted)-1}\x00'
    f = re.sub(r'"([^"]*)"', qrep, fmt)
    # remove leading [color] tokens
    f = re.sub(r'\[(?:[^\]]+)\]', '', f)
    # date?
    has_date_tok = bool(re.search(r'(?i)y{2,4}|m{1,4}|d{1,4}|h{1,2}|s{1,2}', f))
    if has_date_tok and not re.search(r'(?<![A-Za-z])(?:mmm)|y', f):
        # ambiguous m: only date if y or d present
        pass
    if has_date_tok and (re.search(r'y', f) or re.search(r'(?<![A-Za-z])d', f) or re.search(r'h', f) or re.search(r's', f)):
        return _format_date(n, f, quoted)
    # percentage?
    pct = '%' in f
    if pct: n *= 100
    # thousands separator?
    if ',' in f and re.search(r'#,#|0,0', f):
        thou = True
    else:
        thou = False
    # find decimal
    if '.' in f:
        int_part, dec_part = f.split('.', 1)
    else:
        int_part, dec_part = f, ''
    # count digit placeholders in dec_part
    dec_digits = sum(1 for c in dec_part if c in '0#')
    rounded = round(n, dec_digits)
    sign = '-' if rounded < 0 else ''
    av = abs(rounded)
    # build int and frac strings
    if dec_digits > 0:
        s = f'{av:.{dec_digits}f}'
        ip, fp = s.split('.')
    else:
        ip = str(int(round(av)))
        fp = ''
    if thou:
        ip_with_sep = '{:,}'.format(int(ip))
    else:
        ip_with_sep = ip
    # fill template
    out_int = _fill_template(int_part, ip_with_sep, thou=thou)
    out_dec = _fill_dec_template(dec_part, fp)
    if dec_part:
        body = out_int + '.' + out_dec
    else:
        body = out_int
    body = sign + body
    # restore quoted
    body = re.sub(r'\x00(\d+)\x00', lambda m: quoted[int(m.group(1))], body)
    return body

def _fill_template(tmpl, digits, thou=False):
    # Find first and last placeholder positions; literals before/after are prefix/suffix.
    chars = list(tmpl)
    first_p = next((i for i, c in enumerate(chars) if c in '0#'), -1)
    last_p = next((len(chars) - 1 - i for i, c in enumerate(reversed(chars)) if c in '0#'), -1)
    if first_p < 0:
        return ''.join(c for c in chars if c != ',')
    prefix = ''.join(c for c in chars[:first_p] if c != ',')
    suffix = ''.join(c for c in chars[last_p+1:] if c != ',')
    middle = chars[first_p:last_p+1]
    # Walk middle right-to-left, consuming digits from the right of digits string.
    di = len(digits) - 1
    out = []
    for ch in reversed(middle):
        if ch in '0#':
            if di >= 0:
                out.append(digits[di]); di -= 1
            else:
                out.append('0' if ch == '0' else '')
        elif ch == ',':
            continue
        else:
            out.append(ch)
    rest = digits[:di+1] if di >= 0 else ''
    return prefix + rest + ''.join(reversed(out)) + suffix

def _fill_dec_template(tmpl, frac):
    out = []
    fi = 0
    for ch in tmpl:
        if ch == '0':
            out.append(frac[fi] if fi < len(frac) else '0'); fi += 1
        elif ch == '#':
            out.append(frac[fi] if fi < len(frac) else ''); fi += 1
        else:
            out.append(ch)
    return ''.join(out)

MONTH_FULL = ['January','February','March','April','May','June','July','August','September','October','November','December']
MONTH_ABBR = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']
DAY_FULL = ['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday']
DAY_ABBR = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun']

def _format_date(n, fmt, quoted):
    dt = serial_to_datetime(n)
    is_pm = dt.hour >= 12
    has_ampm = bool(re.search(r'(?i)am/pm', fmt))
    out = ''
    i = 0
    # Decide if 'm' = month or minute by neighbour: minute if preceded by h or followed by s.
    # We'll do a tokenization pass.
    tokens = []
    j = 0
    while j < len(fmt):
        m = re.match(r'(?i)(yyyy|yy|mmmm|mmm|mm|m|dddd|ddd|dd|d|hh|h|ss|s|am/pm)', fmt[j:])
        if m:
            tokens.append(('T', m.group(0))); j += len(m.group(0))
        else:
            tokens.append(('L', fmt[j])); j += 1
    # classify m tokens
    out_parts = []
    for k, (kind, tk) in enumerate(tokens):
        if kind == 'L':
            out_parts.append(tk); continue
        t = tk.lower()
        if t in ('m','mm'):
            # minute if neighbouring h/hh or ss/s
            prev = next((tokens[x] for x in range(k-1, -1, -1) if tokens[x][0]=='T'), None)
            nxt = next((tokens[x] for x in range(k+1, len(tokens)) if tokens[x][0]=='T'), None)
            is_min = (prev and prev[1].lower() in ('h','hh')) or (nxt and nxt[1].lower() in ('s','ss'))
            if is_min:
                v = dt.minute
                out_parts.append(f'{v:02d}' if t == 'mm' else str(v))
            else:
                out_parts.append(f'{dt.month:02d}' if t == 'mm' else str(dt.month))
        elif t == 'mmm': out_parts.append(MONTH_ABBR[dt.month-1])
        elif t == 'mmmm': out_parts.append(MONTH_FULL[dt.month-1])
        elif t == 'yy': out_parts.append(f'{dt.year % 100:02d}')
        elif t == 'yyyy': out_parts.append(f'{dt.year:04d}')
        elif t == 'd': out_parts.append(str(dt.day))
        elif t == 'dd': out_parts.append(f'{dt.day:02d}')
        elif t == 'ddd': out_parts.append(DAY_ABBR[dt.weekday()])
        elif t == 'dddd': out_parts.append(DAY_FULL[dt.weekday()])
        elif t in ('h','hh'):
            h = dt.hour % 12 if has_ampm else dt.hour
            if has_ampm and h == 0: h = 12
            out_parts.append(f'{h:02d}' if t == 'hh' else str(h))
        elif t in ('s','ss'):
            out_parts.append(f'{dt.second:02d}' if t == 'ss' else str(dt.second))
        elif t == 'am/pm':
            out_parts.append('PM' if is_pm else 'AM')
    s = ''.join(out_parts)
    s = re.sub(r'\x00(\d+)\x00', lambda m: quoted[int(m.group(1))], s)
    return s
