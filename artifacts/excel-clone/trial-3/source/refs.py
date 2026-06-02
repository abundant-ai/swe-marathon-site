"""A1 reference parsing and manipulation."""
import re

CELL_RE = re.compile(r'^(\$?)([A-Za-z]+)(\$?)(\d+)$')
RANGE_RE = re.compile(r'^(\$?)([A-Za-z]+)(\$?)(\d+):(\$?)([A-Za-z]+)(\$?)(\d+)$')

def col_to_idx(s):
    s = s.upper()
    n = 0
    for c in s:
        n = n * 26 + (ord(c) - ord('A') + 1)
    return n - 1

def idx_to_col(i):
    s = ''
    i = i + 1
    while i > 0:
        i, r = divmod(i - 1, 26)
        s = chr(ord('A') + r) + s
    return s

def parse_ref(ref):
    """Returns (col_idx, row_idx, col_abs, row_abs) or None."""
    m = CELL_RE.match(ref)
    if not m:
        return None
    ca, cl, ra, rn = m.groups()
    return (col_to_idx(cl), int(rn) - 1, ca == '$', ra == '$')

def make_ref(col, row, col_abs=False, row_abs=False):
    return f"{'$' if col_abs else ''}{idx_to_col(col)}{'$' if row_abs else ''}{row + 1}"

def parse_range(rng):
    """Returns ((c1, r1, ca1, ra1), (c2, r2, ca2, ra2)) or None."""
    if ':' not in rng:
        p = parse_ref(rng)
        if p is None:
            return None
        return (p, p)
    a, b = rng.split(':', 1)
    pa = parse_ref(a)
    pb = parse_ref(b)
    if pa is None or pb is None:
        return None
    return (pa, pb)

def expand_range(rng):
    """Yield (col, row) tuples within range string like 'A1:B3'."""
    p = parse_range(rng)
    if p is None:
        return
    (c1, r1, _, _), (c2, r2, _, _) = p
    if c1 > c2: c1, c2 = c2, c1
    if r1 > r2: r1, r2 = r2, r1
    for r in range(r1, r2 + 1):
        for c in range(c1, c2 + 1):
            yield (c, r)

def range_dims(rng):
    p = parse_range(rng)
    if p is None:
        return None
    (c1, r1, _, _), (c2, r2, _, _) = p
    if c1 > c2: c1, c2 = c2, c1
    if r1 > r2: r1, r2 = r2, r1
    return (r2 - r1 + 1, c2 - c1 + 1)
