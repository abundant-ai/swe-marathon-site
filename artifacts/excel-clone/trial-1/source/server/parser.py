"""Pratt-style expression parser for Excel formulas."""
from __future__ import annotations
from typing import List

from .tokens import (
    Token, tokenize, ParseError,
    T_NUM, T_STR, T_BOOL, T_REF, T_RANGE, T_NAME, T_OP, T_LP, T_RP,
    T_COMMA, T_SEMI, T_PCT, T_ERR, T_LBRACE, T_RBRACE,
)
from .ast import (
    Num, Str, Bool, Err, Ref, RangeRef, Name,
    BinOp, UnaryOp, PercentOp, Call, CallExpr, ArrayLit,
)
from .refs import col_letters_to_index, parse_a1, split_sheet_ref

# Excel operator precedence (low → high)
PREC = {
    ":": 95,
    "%": 90,
    "^": 80,
    "*": 70, "/": 70,
    "+": 60, "-": 60,
    "&": 50,
    "=": 40, "<>": 40, "<": 40, ">": 40, "<=": 40, ">=": 40,
}
RIGHT_ASSOC = {"^"}


def _parse_ref_token(tx: str) -> Ref:
    sheet, local = split_sheet_ref(tx)
    c, r, ca, ra = parse_a1(local)
    return Ref(sheet=sheet, col=c, row=r, col_abs=ca, row_abs=ra)


def _parse_range_token(tx: str) -> RangeRef:
    sheet, local = split_sheet_ref(tx)
    a, b = local.split(":", 1)
    ca, ra, ca_abs, ra_abs = parse_a1(a)
    cb, rb, cb_abs, rb_abs = parse_a1(b)
    return RangeRef(
        sheet=sheet,
        c0=min(ca, cb), r0=min(ra, rb),
        c1=max(ca, cb), r1=max(ra, rb),
        c0_abs=ca_abs, r0_abs=ra_abs,
        c1_abs=cb_abs, r1_abs=rb_abs,
    )


class Parser:
    def __init__(self, tokens: List[Token]):
        self.toks = tokens
        self.pos = 0

    def peek(self):
        return self.toks[self.pos] if self.pos < len(self.toks) else None

    def eat(self, kind=None, text=None):
        t = self.peek()
        if t is None:
            raise ParseError("unexpected EOF")
        if kind and t.kind != kind:
            raise ParseError(f"expected {kind} got {t}")
        if text is not None and t.text != text:
            raise ParseError(f"expected {text!r} got {t.text!r}")
        self.pos += 1
        return t

    def parse(self):
        node = self.parse_expr(0)
        if self.pos < len(self.toks):
            raise ParseError(f"trailing tokens at {self.pos}: {self.toks[self.pos]}")
        return node

    def parse_expr(self, min_prec: int):
        left = self.parse_unary()
        while True:
            t = self.peek()
            if t is None:
                break
            if t.kind == T_OP and t.text in PREC:
                p = PREC[t.text]
                if p < min_prec:
                    break
                op = t.text
                self.pos += 1
                next_min = p + 1 if op not in RIGHT_ASSOC else p
                right = self.parse_expr(next_min)
                left = BinOp(op, left, right)
                continue
            if t.kind == T_PCT:
                self.pos += 1
                left = PercentOp(left)
                continue
            break
        return left

    def parse_unary(self):
        t = self.peek()
        if t and t.kind == T_OP and t.text in ("+", "-"):
            self.pos += 1
            inner = self.parse_unary()
            return UnaryOp(t.text, inner)
        return self.parse_atom()

    def parse_atom(self):
        t = self.peek()
        if t is None:
            raise ParseError("unexpected end")
        if t.kind == T_NUM:
            self.pos += 1
            return Num(t.value)
        if t.kind == T_STR:
            self.pos += 1
            return Str(t.value)
        if t.kind == T_BOOL:
            self.pos += 1
            return Bool(t.value)
        if t.kind == T_ERR:
            self.pos += 1
            return Err(t.value)
        if t.kind == T_REF:
            self.pos += 1
            return _parse_ref_token(t.text)
        if t.kind == T_RANGE:
            self.pos += 1
            return _parse_range_token(t.text)
        if t.kind == T_LP:
            self.pos += 1
            inner = self.parse_expr(0)
            self.eat(T_RP)
            return inner
        if t.kind == T_LBRACE:
            self.pos += 1
            rows = []
            cur = []
            if self.peek() and self.peek().kind != T_RBRACE:
                cur.append(self.parse_expr(0))
                while True:
                    nx = self.peek()
                    if nx is None: raise ParseError("unterminated array literal")
                    if nx.kind == T_COMMA:
                        self.pos += 1
                        cur.append(self.parse_expr(0))
                    elif nx.kind == T_SEMI:
                        self.pos += 1
                        rows.append(cur)
                        cur = [self.parse_expr(0)]
                    else:
                        break
                rows.append(cur)
            self.eat(T_RBRACE)
            return ArrayLit(rows)
        if t.kind == T_NAME:
            self.pos += 1
            nxt = self.peek()
            if nxt and nxt.kind == T_LP:
                self.pos += 1
                args = []
                if self.peek() and self.peek().kind != T_RP:
                    args.append(self.parse_expr(0))
                    while self.peek() and self.peek().kind == T_COMMA:
                        self.pos += 1
                        args.append(self.parse_expr(0))
                self.eat(T_RP)
                node = Call(t.text, args)
                # support (LAMBDA(...)(args))(args2) — call chains
                while self.peek() and self.peek().kind == T_LP:
                    self.pos += 1
                    a2 = []
                    if self.peek() and self.peek().kind != T_RP:
                        a2.append(self.parse_expr(0))
                        while self.peek() and self.peek().kind == T_COMMA:
                            self.pos += 1
                            a2.append(self.parse_expr(0))
                    self.eat(T_RP)
                    node = CallExpr(node, a2)
                return node
            return Name(t.value)
        raise ParseError(f"unexpected token {t}")


def parse_formula(text: str, arg_sep: str = ",", decimal_sep: str = "."):
    """Parse a formula (without the leading '=') into an AST."""
    src = text
    if decimal_sep == ",":
        # tokenizer expects '.' as the decimal separator. swap user ',' to '.'
        # but only inside numeric literals. Do this conservatively:
        # only swap the comma if it sits between digits (not the arg-sep).
        # Since arg_sep is ';' in those locales, every other ',' is safe to convert.
        out = []
        i = 0
        in_str = False
        while i < len(src):
            ch = src[i]
            if ch == '"':
                in_str = not in_str
                out.append(ch); i += 1; continue
            if not in_str and ch == ',':
                # is this a decimal? require digit on either side
                left_ok = i > 0 and src[i-1].isdigit()
                right_ok = i + 1 < len(src) and src[i+1].isdigit()
                if left_ok and right_ok:
                    out.append('.')
                    i += 1
                    continue
            out.append(ch)
            i += 1
        src = "".join(out)
    toks = tokenize(src, arg_sep=arg_sep)
    return Parser(toks).parse()
