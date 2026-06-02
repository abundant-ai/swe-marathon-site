"""Formula tokenizer + parser.
Produces an AST of nodes:
  ('num', n) ('str', s) ('bool', b) ('err', s)
  ('ref', sheet_or_None, ref_str)
  ('range', sheet_or_None, range_str)
  ('name', name_str)  -- bare name
  ('unary', op, expr) ('binop', op, l, r)
  ('call', name, [args])
  ('lambda', [params], body)
  ('let', [(name, expr)...], body)
  ('apply', callable_expr, [args])
  ('array', [[row]])
"""
import re

TOKEN_RE = re.compile(r'''
    \s+ |                                     # whitespace
    (?P<NUM>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?) |
    (?P<STR>"(?:[^"]|"")*") |
    (?P<ERR>\#(?:DIV/0!|NAME\?|N/A|NUM!|VALUE!|REF!|NULL!|SPILL!|CIRC!|CALC!|GETTING_DATA)) |
    (?P<SHEETQ>'(?:[^']|'')*'!) |
    (?P<SHEET>[A-Za-z_][A-Za-z0-9_\.]*!) |
    (?P<RNG>\$?[A-Za-z]+\$?\d+:\$?[A-Za-z]+\$?\d+)(?![A-Za-z0-9_]) |
    (?P<CELL>\$?[A-Za-z]+\$?\d+)(?![A-Za-z0-9_]) |
    (?P<IDENT>[A-Za-z_][A-Za-z0-9_\.]*) |
    (?P<OP><=|>=|<>|[+\-*/^&=<>%]) |
    (?P<LP>\() | (?P<RP>\)) |
    (?P<LB>\{) | (?P<RB>\}) |
    (?P<COMMA>,) | (?P<SEMI>;) |
    (?P<COLON>:)
''', re.VERBOSE)

class ParseError(Exception):
    pass

_FUNCTION_NAMES = set()  # populated externally

def tokenize(s):
    toks = []
    i = 0
    while i < len(s):
        m = TOKEN_RE.match(s, i)
        if not m:
            raise ParseError(f"unexpected char at {i}: {s[i:i+10]!r}")
        i = m.end()
        if m.lastgroup is None:
            continue
        toks.append((m.lastgroup, m.group()))
    toks.append(('END', ''))
    return toks


class Parser:
    def __init__(self, tokens, arg_sep=','):
        self.toks = tokens
        self.i = 0
        self.arg_sep = arg_sep  # ',' for en-US, ';' for de/fr/es

    def peek(self):
        return self.toks[self.i]

    def take(self):
        t = self.toks[self.i]
        self.i += 1
        return t

    def expect(self, kind):
        t = self.take()
        if t[0] != kind:
            raise ParseError(f"expected {kind}, got {t}")
        return t

    def parse(self):
        e = self.parse_expr()
        if self.peek()[0] != 'END':
            raise ParseError(f"trailing tokens: {self.toks[self.i:]}")
        return e

    # comparison
    def parse_expr(self):
        return self.parse_compare()

    def parse_compare(self):
        left = self.parse_concat()
        while self.peek()[0] == 'OP' and self.peek()[1] in ('=', '<>', '<', '>', '<=', '>='):
            op = self.take()[1]
            right = self.parse_concat()
            left = ('binop', op, left, right)
        return left

    def parse_concat(self):
        left = self.parse_addsub()
        while self.peek()[0] == 'OP' and self.peek()[1] == '&':
            self.take()
            right = self.parse_addsub()
            left = ('binop', '&', left, right)
        return left

    def parse_addsub(self):
        left = self.parse_muldiv()
        while self.peek()[0] == 'OP' and self.peek()[1] in ('+', '-'):
            op = self.take()[1]
            right = self.parse_muldiv()
            left = ('binop', op, left, right)
        return left

    def parse_muldiv(self):
        left = self.parse_pow()
        while self.peek()[0] == 'OP' and self.peek()[1] in ('*', '/'):
            op = self.take()[1]
            right = self.parse_pow()
            left = ('binop', op, left, right)
        return left

    def parse_pow(self):
        left = self.parse_percent()
        while self.peek()[0] == 'OP' and self.peek()[1] == '^':
            self.take()
            right = self.parse_percent()
            left = ('binop', '^', left, right)
        return left

    def parse_percent(self):
        e = self.parse_unary()
        while self.peek()[0] == 'OP' and self.peek()[1] == '%':
            self.take()
            e = ('unary', '%', e)
        return e

    def parse_unary(self):
        if self.peek()[0] == 'OP' and self.peek()[1] in ('-', '+'):
            op = self.take()[1]
            e = self.parse_unary()
            return ('unary', op, e)
        return self.parse_apply()

    def parse_apply(self):
        e = self.parse_atom()
        # function call applied to value: e.g. LAMBDA(...)(args)
        while self.peek()[0] == 'LP':
            self.take()
            args = self.parse_args()
            self.expect('RP')
            e = ('apply', e, args)
        return e

    def parse_args(self):
        args = []
        if self.peek()[0] == 'RP':
            return args
        args.append(self.parse_expr())
        while self.peek()[0] in ('COMMA', 'SEMI'):
            self.take()
            args.append(self.parse_expr())
        return args

    def parse_atom(self):
        t = self.peek()
        if t[0] == 'NUM':
            self.take()
            v = float(t[1])
            if v.is_integer():
                v = int(v)
            return ('num', v)
        if t[0] == 'STR':
            self.take()
            s = t[1][1:-1].replace('""', '"')
            return ('str', s)
        if t[0] == 'ERR':
            self.take()
            return ('err', t[1])
        if t[0] == 'LP':
            self.take()
            e = self.parse_expr()
            self.expect('RP')
            return e
        if t[0] == 'LB':
            return self.parse_array_literal()
        if t[0] in ('SHEET', 'SHEETQ'):
            sheet_tok = self.take()[1]
            if sheet_tok.startswith("'"):
                sheet = sheet_tok[1:-2].replace("''", "'")
            else:
                sheet = sheet_tok[:-1]
            nxt = self.peek()
            if nxt[0] == 'RNG':
                self.take()
                return ('range', sheet, nxt[1])
            if nxt[0] == 'CELL':
                self.take()
                # could be sheet!Name or sheet!A1; parser already classified A1
                return ('ref', sheet, nxt[1])
            if nxt[0] == 'IDENT':
                self.take()
                return ('name', f"{sheet}!{nxt[1]}")
            raise ParseError(f"after sheet ref, got {nxt}")
        if t[0] == 'RNG':
            self.take()
            return ('range', None, t[1])
        if t[0] == 'CELL':
            self.take()
            # could be cell or short range (e.g. =A1:A1) — also could be name (won't be here, IDENT would handle)
            # peek for colon to extend to range
            if self.peek()[0] == 'COLON':
                self.take()
                p2 = self.take()
                if p2[0] != 'CELL':
                    raise ParseError("bad range")
                return ('range', None, f"{t[1]}:{p2[1]}")
            return ('ref', None, t[1])
        if t[0] == 'IDENT':
            name = self.take()[1]
            # function call?
            if self.peek()[0] == 'LP':
                self.take()
                args = self.parse_args()
                self.expect('RP')
                up = name.upper()
                if up == 'LAMBDA':
                    if not args:
                        raise ParseError("LAMBDA requires body")
                    params = []
                    for a in args[:-1]:
                        if a[0] != 'name' and a[0] != 'ref':
                            raise ParseError("LAMBDA params must be names")
                        params.append(a[1] if a[0] == 'name' else a[2])
                    return ('lambda', params, args[-1])
                if up == 'LET':
                    if len(args) < 3 or len(args) % 2 == 0:
                        raise ParseError("LET needs odd args >=3")
                    bindings = []
                    for j in range(0, len(args) - 1, 2):
                        ne = args[j]
                        if ne[0] == 'name':
                            nm = ne[1]
                        elif ne[0] == 'ref':
                            nm = ne[2]
                        else:
                            raise ParseError("LET name expected")
                        bindings.append((nm, args[j + 1]))
                    return ('let', bindings, args[-1])
                return ('call', up, args)
            # bare name (could be defined name, lambda var, or named range)
            return ('name', name)
        raise ParseError(f"unexpected token {t}")

    def parse_array_literal(self):
        self.expect('LB')
        rows = [[]]
        rows[-1].append(self.parse_expr())
        while True:
            t = self.peek()
            if t[0] == 'COMMA':
                self.take()
                rows[-1].append(self.parse_expr())
            elif t[0] == 'SEMI':
                self.take()
                rows.append([self.parse_expr()])
            else:
                break
        self.expect('RB')
        return ('array', rows)


def parse_formula(text, arg_sep=','):
    # text starts with =
    if not text.startswith('='):
        raise ParseError("formula must start with =")
    body = text[1:]
    toks = tokenize(body)
    p = Parser(toks, arg_sep=arg_sep)
    return p.parse()
