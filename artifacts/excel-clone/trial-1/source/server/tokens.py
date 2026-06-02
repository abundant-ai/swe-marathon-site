"""Excel-formula tokenizer."""
from __future__ import annotations
import re

# token kinds
T_NUM   = "num"
T_STR   = "str"
T_BOOL  = "bool"
T_REF   = "ref"           # may be `Sheet!A1`, `A1`, `$A$1`
T_RANGE = "range"         # `A1:B5` (may be sheet-qualified)
T_NAME  = "name"          # function name or named range
T_OP    = "op"
T_LP    = "lp"
T_RP    = "rp"
T_COMMA = "comma"
T_SEMI  = "semi"          # used by some locales
T_PCT   = "pct"
T_ERR   = "err"           # #DIV/0! etc
T_LBRACE = "lbrace"
T_RBRACE = "rbrace"

# Keywords that look like names but parse as bools
BOOL_LITERALS = {"TRUE": True, "FALSE": False}

# Excel error literals
ERROR_LITERALS = {
    "#DIV/0!", "#NAME?", "#N/A", "#NUM!", "#REF!", "#VALUE!",
    "#NULL!", "#SPILL!", "#CIRC!", "#GETTING_DATA",
}

# Order matters: match longer ops first
OPS = ["<>", "<=", ">=", "=", "<", ">", "&", "+", "-", "*", "/", "^", ":"]

CELL_RE = re.compile(
    r"(?:'(?:[^']|'')+'!|[A-Za-z_][\w\.]*!)?"  # optional sheet
    r"\$?[A-Za-z]+\$?\d+(?::\$?[A-Za-z]+\$?\d+)?"
)
NAME_RE = re.compile(r"[A-Za-z_][\w\.]*(?:\.[A-Za-z_][\w\.]*)*")
NUM_RE  = re.compile(r"\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?")
ERR_RE  = re.compile(r"#(?:DIV/0!|NAME\?|N/A|NUM!|REF!|VALUE!|NULL!|SPILL!|CIRC!|GETTING_DATA)")


class ParseError(ValueError):
    pass


class Token:
    __slots__ = ("kind", "text", "value")
    def __init__(self, kind, text, value=None):
        self.kind = kind
        self.text = text
        self.value = value
    def __repr__(self):
        return f"Token({self.kind}, {self.text!r}, {self.value!r})"


def _is_ref_segment(s: str) -> bool:
    """True if the trailing portion of a sheet-qualified token like `Sheet1!A1` is a cell or range."""
    return bool(re.fullmatch(r"\$?[A-Za-z]+\$?\d+(?::\$?[A-Za-z]+\$?\d+)?", s))


def tokenize(text: str, arg_sep: str = ",") -> list:
    """Tokenize a formula. Leading '=' should already be stripped."""
    s = text
    i = 0
    n = len(s)
    out = []
    # arg_sep is `,` (en-US) or `;` (de-DE/fr-FR/es-ES)
    while i < n:
        c = s[i]
        if c.isspace():
            i += 1
            continue
        if c == '"':
            j = i + 1
            buf = []
            while j < n:
                if s[j] == '"':
                    if j + 1 < n and s[j + 1] == '"':
                        buf.append('"')
                        j += 2
                        continue
                    break
                buf.append(s[j])
                j += 1
            if j >= n:
                raise ParseError("unterminated string")
            out.append(Token(T_STR, s[i:j+1], "".join(buf)))
            i = j + 1
            continue
        if c == "'" and (i + 1 < n):
            # quoted sheet-name: possibly part of a ref
            j = i + 1
            while j < n:
                if s[j] == "'":
                    if j + 1 < n and s[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            if j >= n:
                raise ParseError("unterminated sheet name")
            # j is closing quote; need !ref after
            if j + 1 < n and s[j + 1] == "!":
                m = CELL_RE.match(s, i)
                if m:
                    txt = m.group(0)
                    kind = T_RANGE if ":" in txt else T_REF
                    out.append(Token(kind, txt))
                    i = m.end()
                    continue
            raise ParseError(f"bad sheet ref at {i}")
        if c == '#':
            m = ERR_RE.match(s, i)
            if m:
                out.append(Token(T_ERR, m.group(0), m.group(0)))
                i = m.end()
                continue
            raise ParseError(f"bad error literal at {i}")
        if c.isdigit() or (c == '.' and i + 1 < n and s[i+1].isdigit()):
            m = NUM_RE.match(s, i)
            if not m:
                raise ParseError(f"bad number at {i}")
            tx = m.group(0)
            out.append(Token(T_NUM, tx, float(tx) if "." in tx or "e" in tx.lower() else int(tx)))
            i = m.end()
            continue
        if c == '(':
            out.append(Token(T_LP, c)); i += 1; continue
        if c == ')':
            out.append(Token(T_RP, c)); i += 1; continue
        if c == '{':
            out.append(Token(T_LBRACE, c)); i += 1; continue
        if c == '}':
            out.append(Token(T_RBRACE, c)); i += 1; continue
        if c == arg_sep:
            out.append(Token(T_COMMA, c)); i += 1; continue
        if c == ',' and arg_sep != ',':
            # in non-en-US locales `,` is used as the decimal point inside numbers
            # any other comma here is a syntax error
            raise ParseError(f"unexpected ',' at {i}")
        if c == ';' and arg_sep != ';':
            # `;` is also used as a row separator in array literals,
            # which the parser handles. Emit a SEMI token so the parser
            # can distinguish from the arg-sep.
            out.append(Token(T_SEMI, c)); i += 1; continue
        if c == '%':
            out.append(Token(T_PCT, c)); i += 1; continue
        # operators
        op_matched = False
        for op in OPS:
            if s.startswith(op, i):
                out.append(Token(T_OP, op))
                i += len(op)
                op_matched = True
                break
        if op_matched:
            continue
        # cell reference or name
        m = CELL_RE.match(s, i)
        if m:
            tx = m.group(0)
            # If it looks like LETTERS+DIGITS (no $, no sheet, no `:`)
            # AND the next char is `(`, treat it as a name (e.g., LOG10).
            end = m.end()
            if (end < n and s[end] == '(' and "$" not in tx and "!" not in tx
                    and ":" not in tx):
                # try to match as name instead
                mn = NAME_RE.match(s, i)
                if mn and mn.end() >= end:
                    tx2 = mn.group(0)
                    up = tx2.upper()
                    if up in BOOL_LITERALS:
                        out.append(Token(T_BOOL, tx2, BOOL_LITERALS[up]))
                    else:
                        out.append(Token(T_NAME, tx2, tx2))
                    i = mn.end()
                    continue
            kind = T_RANGE if ":" in tx else T_REF
            out.append(Token(kind, tx))
            i = end
            continue
        m = NAME_RE.match(s, i)
        if m:
            tx = m.group(0)
            up = tx.upper()
            if up in BOOL_LITERALS:
                out.append(Token(T_BOOL, tx, BOOL_LITERALS[up]))
            else:
                out.append(Token(T_NAME, tx, tx))
            i = m.end()
            continue
        raise ParseError(f"bad char {c!r} at {i}")
    return out
