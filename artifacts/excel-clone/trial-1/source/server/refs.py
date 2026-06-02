"""Cell reference helpers: A1 <-> (row, col), ranges, parsing."""
from __future__ import annotations
import re

A1_RE = re.compile(r"^(\$?)([A-Za-z]+)(\$?)(\d+)$")


def col_letters_to_index(letters: str) -> int:
    n = 0
    for ch in letters.upper():
        n = n * 26 + (ord(ch) - ord('A') + 1)
    return n - 1


def col_index_to_letters(idx: int) -> str:
    n = idx + 1
    out = []
    while n > 0:
        n, r = divmod(n - 1, 26)
        out.append(chr(ord('A') + r))
    return "".join(reversed(out))


def parse_a1(ref: str):
    """Return (col_index, row_index, col_abs, row_abs) for an A1 string."""
    m = A1_RE.match(ref.strip())
    if not m:
        raise ValueError(f"bad ref: {ref!r}")
    cdollar, letters, rdollar, digits = m.groups()
    return (col_letters_to_index(letters), int(digits) - 1,
            cdollar == '$', rdollar == '$')


def make_a1(col: int, row: int, col_abs: bool = False, row_abs: bool = False) -> str:
    cs = "$" if col_abs else ""
    rs = "$" if row_abs else ""
    return f"{cs}{col_index_to_letters(col)}{rs}{row + 1}"


def parse_range(rng: str):
    """`A1:B5` → (c0, r0, c1, r1) with c0<=c1, r0<=r1.

    A bare cell like `A1` returns a 1x1 range.
    """
    if ":" in rng:
        a, b = rng.split(":", 1)
        c0, r0, _, _ = parse_a1(a)
        c1, r1, _, _ = parse_a1(b)
    else:
        c0, r0, _, _ = parse_a1(rng)
        c1, r1 = c0, r0
    if c0 > c1:
        c0, c1 = c1, c0
    if r0 > r1:
        r0, r1 = r1, r0
    return c0, r0, c1, r1


def expand_range(rng: str):
    c0, r0, c1, r1 = parse_range(rng)
    refs = []
    for r in range(r0, r1 + 1):
        for c in range(c0, c1 + 1):
            refs.append(make_a1(c, r))
    return refs


def split_sheet_ref(qualified: str):
    """Split 'Sheet!A1' or 'Sheet!A1:B2' into (sheet_or_None, local).

    Sheet may be quoted with single quotes, e.g. `'My Sheet'!A1`.
    """
    if "!" in qualified:
        s, rest = qualified.rsplit("!", 1)
        s = s.strip()
        if s.startswith("'") and s.endswith("'"):
            s = s[1:-1].replace("''", "'")
        return s, rest
    return None, qualified


def normalize_ref(ref: str) -> str:
    """Strip $ pins to canonical A1 (used as dict key)."""
    c, r, _, _ = parse_a1(ref)
    return make_a1(c, r)
