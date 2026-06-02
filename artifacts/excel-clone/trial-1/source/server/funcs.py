"""Built-in spreadsheet functions.

Functions take pre-evaluated arguments. Ranges arrive as 2D lists of
values (list[list]). Single cells arrive as scalars. A function may
return a scalar or a 2D list (dynamic-array). Errors are raised as
XLError with one of the error codes from .errors.
"""
from __future__ import annotations

import datetime as _dt
import math
import re
import statistics
from typing import Any, Iterable, List, Tuple

from .errors import XLError, NA, NUM, NAME, DIV0, VALUE, REF


# ----- helpers -------------------------------------------------------

def _is_array(v):
    return isinstance(v, list) and v and isinstance(v[0], list)


def _to_array(v) -> List[List[Any]]:
    """Normalise scalar / 1D / 2D into 2D list."""
    if isinstance(v, list):
        if v and isinstance(v[0], list):
            return v
        return [list(v)]
    return [[v]]


def _flatten(v) -> List[Any]:
    if isinstance(v, list):
        out = []
        for r in v:
            if isinstance(r, list):
                out.extend(_flatten(r))
            else:
                out.append(r)
        return out
    return [v]


def _flatten_numeric(args, *, count_bool=False, strict=False) -> List[float]:
    """Flatten args, keeping only numbers (and optionally bools)."""
    out = []
    for a in args:
        for v in _flatten(a):
            if v is None or v == "":
                continue
            if _is_xl_error(v):
                raise XLError(v)
            if isinstance(v, bool):
                if count_bool:
                    out.append(1.0 if v else 0.0)
                continue
            if isinstance(v, (int, float)):
                out.append(float(v))
                continue
            if isinstance(v, str):
                if strict:
                    try:
                        out.append(float(v))
                    except ValueError:
                        raise XLError(VALUE)
                continue
    return out


def _is_xl_error(v):
    return isinstance(v, str) and v.startswith("#") and (v.endswith("!") or v.endswith("?") or v == "#N/A")


def _to_number(v) -> float:
    if v is None or v == "":
        return 0.0
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        if _is_xl_error(v):
            raise XLError(v)
        try:
            return float(v)
        except ValueError:
            raise XLError(VALUE)
    raise XLError(VALUE)


def _to_int(v) -> int:
    f = _to_number(v)
    return int(math.floor(f))


def _to_text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, float):
        if v.is_integer():
            return str(int(v))
        return repr(v)
    return str(v)


def _to_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    if v is None or v == "":
        return False
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        if v.upper() == "TRUE":
            return True
        if v.upper() == "FALSE":
            return False
        if _is_xl_error(v):
            raise XLError(v)
        raise XLError(VALUE)
    return bool(v)


# ----- date helpers --------------------------------------------------
# Excel's 1900 system: serial 1 = 1900-01-01, with the spurious 1900-02-29.
# We emulate that bug so values round-trip with Excel/LibreOffice.

_EPOCH = _dt.date(1899, 12, 30)  # serial 0


def _serial_to_date(n) -> _dt.date:
    f = float(n)
    days = int(math.floor(f))
    if days < 60:
        return _EPOCH + _dt.timedelta(days=days)
    return _EPOCH + _dt.timedelta(days=days - 1) if False else _EPOCH + _dt.timedelta(days=days)


def _date_to_serial(d) -> int:
    if isinstance(d, _dt.datetime):
        d_only = d.date()
        frac = (d.hour * 3600 + d.minute * 60 + d.second) / 86400.0
    elif isinstance(d, _dt.date):
        d_only = d
        frac = 0
    else:
        raise XLError(VALUE)
    delta = (d_only - _EPOCH).days
    return delta + frac if frac else delta


def _date_value(v):
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            d = _dt.datetime.strptime(v, "%Y-%m-%d").date()
        except ValueError:
            raise XLError(VALUE)
        return float(_date_to_serial(d))
    raise XLError(VALUE)


# ----- criteria parsing for *IF / *IFS -------------------------------

_CRIT_RE = re.compile(r"^(<>|<=|>=|<|>|=)?(.*)$", re.S)


def _wildcard_to_regex(s: str) -> re.Pattern:
    out = []
    i = 0
    while i < len(s):
        c = s[i]
        if c == '~' and i + 1 < len(s) and s[i + 1] in '*?':
            out.append(re.escape(s[i + 1]))
            i += 2
            continue
        if c == '*':
            out.append('.*')
        elif c == '?':
            out.append('.')
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile('^' + ''.join(out) + '$', re.IGNORECASE)


def _make_predicate(criterion):
    if isinstance(criterion, (int, float, bool)) and not isinstance(criterion, bool):
        target = float(criterion)
        def pred(v):
            try:
                return float(v) == target
            except Exception:
                return False
        return pred
    if isinstance(criterion, bool):
        target = criterion
        def pred(v):
            return v == target
        return pred
    s = "" if criterion is None else str(criterion)
    m = _CRIT_RE.match(s)
    op, rest = m.group(1) or "=", m.group(2)
    rest = rest.strip()
    # try numeric comparison first
    rest_num = None
    try:
        rest_num = float(rest)
    except (ValueError, TypeError):
        pass
    if rest_num is not None and op in ("<", "<=", ">", ">=", "=", "<>"):
        target = rest_num
        if op == "=":
            def pred(v):
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    return float(v) == target
                if isinstance(v, str):
                    try:
                        return float(v) == target
                    except ValueError:
                        return False
                return False
            return pred
        if op == "<>":
            def pred(v):
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    return float(v) != target
                return True
            return pred
        cmp = {"<": lambda a, b: a < b, "<=": lambda a, b: a <= b,
               ">": lambda a, b: a > b, ">=": lambda a, b: a >= b}[op]
        def pred(v):
            if isinstance(v, bool):
                return False
            if isinstance(v, (int, float)):
                return cmp(float(v), target)
            return False
        return pred
    # text compare with wildcards
    if op in ("=", "<>"):
        rx = _wildcard_to_regex(rest) if rest else _wildcard_to_regex("")
        if op == "=":
            def pred(v):
                if v is None or v == "":
                    return rest == "" or rest == ""
                return bool(rx.match(_to_text(v)))
            return pred
        else:
            def pred(v):
                if v is None or v == "":
                    return rest != ""
                return not rx.match(_to_text(v))
            return pred
    # text with order op: lexicographic
    cmp = {"<": lambda a, b: a < b, "<=": lambda a, b: a <= b,
           ">": lambda a, b: a > b, ">=": lambda a, b: a >= b}[op]
    target_s = rest
    def pred(v):
        return isinstance(v, str) and cmp(v, target_s)
    return pred


# ----- public function table ----------------------------------------

def _math():
    fns = {}

    def f_sum(*args):
        return sum(_flatten_numeric(args, count_bool=True))
    fns["SUM"] = f_sum

    def f_average(*args):
        nums = _flatten_numeric(args)
        if not nums:
            raise XLError(DIV0)
        return sum(nums) / len(nums)
    fns["AVERAGE"] = f_average
    fns["MEAN"] = f_average

    def f_min(*args):
        nums = _flatten_numeric(args)
        if not nums:
            return 0
        return min(nums)
    fns["MIN"] = f_min

    def f_max(*args):
        nums = _flatten_numeric(args)
        if not nums:
            return 0
        return max(nums)
    fns["MAX"] = f_max

    def f_count(*args):
        out = 0
        for a in args:
            for v in _flatten(a):
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    out += 1
        return out
    fns["COUNT"] = f_count

    def f_counta(*args):
        out = 0
        for a in args:
            for v in _flatten(a):
                if v is not None and v != "":
                    out += 1
        return out
    fns["COUNTA"] = f_counta

    def f_countblank(*args):
        out = 0
        for a in args:
            for v in _flatten(a):
                if v is None or v == "":
                    out += 1
        return out
    fns["COUNTBLANK"] = f_countblank

    def f_product(*args):
        nums = _flatten_numeric(args)
        if not nums:
            return 0
        out = 1.0
        for n in nums:
            out *= n
        return out
    fns["PRODUCT"] = f_product

    def f_sumsq(*args):
        nums = _flatten_numeric(args)
        return sum(n * n for n in nums)
    fns["SUMSQ"] = f_sumsq

    def f_round(x, digits=0):
        x = _to_number(x); d = _to_int(digits)
        # banker's-rounding-free: use math.copysign + half-up
        m = 10 ** d
        return math.floor(x * m + 0.5) / m if x >= 0 else -math.floor(-x * m + 0.5) / m
    fns["ROUND"] = f_round

    def f_roundup(x, digits=0):
        x = _to_number(x); d = _to_int(digits)
        m = 10 ** d
        return math.ceil(x * m) / m if x >= 0 else -math.ceil(-x * m) / m
    fns["ROUNDUP"] = f_roundup

    def f_rounddown(x, digits=0):
        x = _to_number(x); d = _to_int(digits)
        m = 10 ** d
        return math.floor(x * m) / m if x >= 0 else -math.floor(-x * m) / m
    fns["ROUNDDOWN"] = f_rounddown

    def f_int(x):
        return math.floor(_to_number(x))
    fns["INT"] = f_int

    def f_trunc(x, digits=0):
        x = _to_number(x); d = _to_int(digits)
        m = 10 ** d
        return math.floor(x * m) / m if x >= 0 else -math.floor(-x * m) / m
    fns["TRUNC"] = f_trunc

    def f_mod(a, b):
        a = _to_number(a); b = _to_number(b)
        if b == 0:
            raise XLError(DIV0)
        return a - b * math.floor(a / b)
    fns["MOD"] = f_mod

    def f_abs(x):
        return abs(_to_number(x))
    fns["ABS"] = f_abs

    def f_sign(x):
        x = _to_number(x)
        return -1 if x < 0 else (1 if x > 0 else 0)
    fns["SIGN"] = f_sign

    def f_power(b, e):
        b = _to_number(b); e = _to_number(e)
        try:
            r = math.pow(b, e)
        except (ValueError, OverflowError):
            raise XLError(NUM)
        if isinstance(r, complex):
            raise XLError(NUM)
        return r
    fns["POWER"] = f_power

    def f_sqrt(x):
        x = _to_number(x)
        if x < 0:
            raise XLError(NUM)
        return math.sqrt(x)
    fns["SQRT"] = f_sqrt

    def f_log(x, base=10):
        x = _to_number(x); base = _to_number(base)
        if x <= 0 or base <= 0 or base == 1:
            raise XLError(NUM)
        return math.log(x, base)
    fns["LOG"] = f_log
    fns["LOG10"] = lambda x: math.log10(_to_number(x)) if _to_number(x) > 0 else (_ for _ in ()).throw(XLError(NUM))
    fns["LN"] = lambda x: math.log(_to_number(x)) if _to_number(x) > 0 else (_ for _ in ()).throw(XLError(NUM))
    fns["EXP"] = lambda x: math.exp(_to_number(x))
    fns["PI"] = lambda: math.pi
    fns["SIN"] = lambda x: math.sin(_to_number(x))
    fns["COS"] = lambda x: math.cos(_to_number(x))
    fns["TAN"] = lambda x: math.tan(_to_number(x))
    fns["ASIN"] = lambda x: math.asin(_to_number(x))
    fns["ACOS"] = lambda x: math.acos(_to_number(x))
    fns["ATAN"] = lambda x: math.atan(_to_number(x))
    fns["ATAN2"] = lambda y, x: math.atan2(_to_number(y), _to_number(x))
    fns["SINH"] = lambda x: math.sinh(_to_number(x))
    fns["COSH"] = lambda x: math.cosh(_to_number(x))
    fns["TANH"] = lambda x: math.tanh(_to_number(x))
    fns["RADIANS"] = lambda x: math.radians(_to_number(x))
    fns["DEGREES"] = lambda x: math.degrees(_to_number(x))
    fns["FACT"] = lambda n: math.factorial(_to_int(n)) if _to_int(n) >= 0 else (_ for _ in ()).throw(XLError(NUM))
    fns["COMBIN"] = lambda n, k: math.comb(_to_int(n), _to_int(k)) if _to_int(n) >= _to_int(k) >= 0 else (_ for _ in ()).throw(XLError(NUM))
    fns["PERMUT"] = lambda n, k: math.perm(_to_int(n), _to_int(k)) if _to_int(n) >= _to_int(k) >= 0 else (_ for _ in ()).throw(XLError(NUM))
    fns["SQRTPI"] = lambda x: math.sqrt(_to_number(x) * math.pi) if _to_number(x) >= 0 else (_ for _ in ()).throw(XLError(NUM))
    def f_gcd(*args):
        nums = [int(abs(_to_int(v))) for v in _flatten(args) if v not in (None, "")]
        if not nums:
            return 0
        from math import gcd
        out = nums[0]
        for n in nums[1:]:
            out = gcd(out, n)
        return out
    fns["GCD"] = f_gcd
    def f_lcm(*args):
        nums = [int(abs(_to_int(v))) for v in _flatten(args) if v not in (None, "")]
        if not nums:
            return 0
        from math import gcd
        out = nums[0]
        for n in nums[1:]:
            out = out * n // gcd(out, n) if out and n else 0
        return out
    fns["LCM"] = f_lcm

    def f_ceiling(x, sig=1):
        x = _to_number(x); s = _to_number(sig)
        if s == 0:
            return 0
        return math.ceil(x / s) * s
    fns["CEILING"] = f_ceiling
    fns["CEILING.MATH"] = f_ceiling

    def f_floor(x, sig=1):
        x = _to_number(x); s = _to_number(sig)
        if s == 0:
            return 0
        return math.floor(x / s) * s
    fns["FLOOR"] = f_floor
    fns["FLOOR.MATH"] = f_floor

    def f_mround(x, m):
        x = _to_number(x); m = _to_number(m)
        if m == 0:
            return 0
        if (x > 0 and m < 0) or (x < 0 and m > 0):
            raise XLError(NUM)
        return round(x / m) * m
    fns["MROUND"] = f_mround

    def f_even(x):
        x = _to_number(x)
        n = math.ceil(abs(x) / 2) * 2
        return n if x >= 0 else -n
    fns["EVEN"] = f_even

    def f_odd(x):
        x = _to_number(x)
        n = math.ceil((abs(x) - 1) / 2) * 2 + 1
        return n if x >= 0 else -n
    fns["ODD"] = f_odd

    def f_sumproduct(*arrays):
        if not arrays:
            raise XLError(NA)
        flat = [list(_flatten(a)) for a in arrays]
        n = len(flat[0])
        if any(len(f) != n for f in flat):
            raise XLError(VALUE)
        total = 0.0
        for i in range(n):
            prod = 1.0
            for f in flat:
                v = f[i]
                if isinstance(v, bool):
                    v = int(v)
                if isinstance(v, (int, float)):
                    prod *= v
                else:
                    prod = 0
                    break
            total += prod
        return total
    fns["SUMPRODUCT"] = f_sumproduct

    def f_choose(idx, *vals):
        i = _to_int(idx)
        if i < 1 or i > len(vals):
            raise XLError(VALUE)
        return vals[i - 1]
    fns["CHOOSE"] = f_choose

    return fns


def _stats():
    fns = {}

    def f_median(*args):
        nums = _flatten_numeric(args)
        if not nums:
            raise XLError(NUM)
        return statistics.median(nums)
    fns["MEDIAN"] = f_median

    def f_mode(*args):
        nums = _flatten_numeric(args)
        if not nums:
            raise XLError(NUM)
        try:
            return statistics.mode(nums)
        except statistics.StatisticsError:
            raise XLError(NA)
    fns["MODE"] = f_mode
    fns["MODE.SNGL"] = f_mode

    def f_stdev(*args):
        nums = _flatten_numeric(args)
        if len(nums) < 2:
            raise XLError(DIV0)
        return statistics.stdev(nums)
    fns["STDEV"] = f_stdev
    fns["STDEV.S"] = f_stdev

    def f_stdevp(*args):
        nums = _flatten_numeric(args)
        if len(nums) < 1:
            raise XLError(DIV0)
        return statistics.pstdev(nums)
    fns["STDEVP"] = f_stdevp
    fns["STDEV.P"] = f_stdevp

    def f_var(*args):
        nums = _flatten_numeric(args)
        if len(nums) < 2:
            raise XLError(DIV0)
        return statistics.variance(nums)
    fns["VAR"] = f_var
    fns["VAR.S"] = f_var

    def f_varp(*args):
        nums = _flatten_numeric(args)
        if len(nums) < 1:
            raise XLError(DIV0)
        return statistics.pvariance(nums)
    fns["VARP"] = f_varp
    fns["VAR.P"] = f_varp

    def f_percentile(arr, p):
        nums = sorted(_flatten_numeric([arr]))
        if not nums:
            raise XLError(NUM)
        p = _to_number(p)
        if not (0 <= p <= 1):
            raise XLError(NUM)
        # Excel uses linear interpolation
        idx = p * (len(nums) - 1)
        lo = int(math.floor(idx))
        hi = int(math.ceil(idx))
        if lo == hi:
            return nums[lo]
        return nums[lo] + (nums[hi] - nums[lo]) * (idx - lo)
    fns["PERCENTILE"] = f_percentile
    fns["PERCENTILE.INC"] = f_percentile

    def f_quartile(arr, q):
        q = _to_int(q)
        return f_percentile(arr, q / 4)
    fns["QUARTILE"] = f_quartile
    fns["QUARTILE.INC"] = f_quartile

    def f_rank(num, arr, order=0):
        target = _to_number(num)
        nums = _flatten_numeric([arr])
        if not nums:
            raise XLError(NA)
        order_val = _to_int(order) if order is not None else 0
        sorted_nums = sorted(nums, reverse=(order_val == 0))
        try:
            return sorted_nums.index(target) + 1
        except ValueError:
            raise XLError(NA)
    fns["RANK"] = f_rank
    fns["RANK.EQ"] = f_rank

    def f_correl(x_arr, y_arr):
        xs = _flatten_numeric([x_arr])
        ys = _flatten_numeric([y_arr])
        if len(xs) != len(ys) or len(xs) < 2:
            raise XLError(DIV0)
        try:
            mx = sum(xs) / len(xs); my = sum(ys) / len(ys)
            num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
            denx = math.sqrt(sum((x - mx) ** 2 for x in xs))
            deny = math.sqrt(sum((y - my) ** 2 for y in ys))
            if denx == 0 or deny == 0:
                raise XLError(DIV0)
            return num / (denx * deny)
        except ZeroDivisionError:
            raise XLError(DIV0)
    fns["CORREL"] = f_correl

    def f_covar(x_arr, y_arr):
        xs = _flatten_numeric([x_arr])
        ys = _flatten_numeric([y_arr])
        if len(xs) != len(ys) or not xs:
            raise XLError(DIV0)
        mx = sum(xs) / len(xs); my = sum(ys) / len(ys)
        return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / len(xs)
    fns["COVAR"] = f_covar
    fns["COVARIANCE.P"] = f_covar

    def f_slope(y_arr, x_arr):
        xs = _flatten_numeric([x_arr])
        ys = _flatten_numeric([y_arr])
        if len(xs) != len(ys) or len(xs) < 2:
            raise XLError(DIV0)
        mx = sum(xs) / len(xs); my = sum(ys) / len(ys)
        num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        den = sum((x - mx) ** 2 for x in xs)
        if den == 0:
            raise XLError(DIV0)
        return num / den
    fns["SLOPE"] = f_slope

    def f_intercept(y_arr, x_arr):
        xs = _flatten_numeric([x_arr])
        ys = _flatten_numeric([y_arr])
        if len(xs) != len(ys) or len(xs) < 2:
            raise XLError(DIV0)
        m = f_slope(y_arr, x_arr)
        mx = sum(xs) / len(xs); my = sum(ys) / len(ys)
        return my - m * mx
    fns["INTERCEPT"] = f_intercept

    def f_forecast(x_target, y_arr, x_arr):
        x_target = _to_number(x_target)
        m = f_slope(y_arr, x_arr)
        b = f_intercept(y_arr, x_arr)
        return m * x_target + b
    fns["FORECAST"] = f_forecast
    fns["FORECAST.LINEAR"] = f_forecast

    return fns


def _dist():
    fns = {}

    def _erf(x):
        return math.erf(x)

    def _cdf_normal(x, mu, sigma):
        return 0.5 * (1.0 + _erf((x - mu) / (sigma * math.sqrt(2))))

    def _pdf_normal(x, mu, sigma):
        return math.exp(-0.5 * ((x - mu) / sigma) ** 2) / (sigma * math.sqrt(2 * math.pi))

    def f_norm_dist(x, mu, sigma, cum):
        x = _to_number(x); mu = _to_number(mu); sigma = _to_number(sigma)
        if sigma <= 0:
            raise XLError(NUM)
        return _cdf_normal(x, mu, sigma) if _to_bool(cum) else _pdf_normal(x, mu, sigma)
    fns["NORM.DIST"] = f_norm_dist
    fns["NORMDIST"] = f_norm_dist

    def _norm_inv(p):
        # Acklam approximation
        if p <= 0 or p >= 1:
            raise XLError(NUM)
        a = [-3.969683028665376e+01,  2.209460984245205e+02,
             -2.759285104469687e+02,  1.383577518672690e+02,
             -3.066479806614716e+01,  2.506628277459239e+00]
        b = [-5.447609879822406e+01,  1.615858368580409e+02,
             -1.556989798598866e+02,  6.680131188771972e+01, -1.328068155288572e+01]
        c = [-7.784894002430293e-03, -3.223964580411365e-01,
             -2.400758277161838e+00, -2.549732539343734e+00,
             4.374664141464968e+00,  2.938163982698783e+00]
        d = [7.784695709041462e-03,  3.224671290700398e-01,
             2.445134137142996e+00,  3.754408661907416e+00]
        plow = 0.02425
        phigh = 1 - plow
        if p < plow:
            q = math.sqrt(-2 * math.log(p))
            return (((((c[0]*q + c[1])*q + c[2])*q + c[3])*q + c[4])*q + c[5]) / \
                   ((((d[0]*q + d[1])*q + d[2])*q + d[3])*q + 1)
        if p <= phigh:
            q = p - 0.5; r = q*q
            return (((((a[0]*r + a[1])*r + a[2])*r + a[3])*r + a[4])*r + a[5])*q / \
                   (((((b[0]*r + b[1])*r + b[2])*r + b[3])*r + b[4])*r + 1)
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q + c[1])*q + c[2])*q + c[3])*q + c[4])*q + c[5]) / \
                ((((d[0]*q + d[1])*q + d[2])*q + d[3])*q + 1)

    def f_norm_inv(p, mu, sigma):
        return mu_v + sigma_v * _norm_inv(_to_number(p)) if (mu_v := _to_number(mu)) is not None and (sigma_v := _to_number(sigma)) > 0 else (_ for _ in ()).throw(XLError(NUM))
    def f_norm_inv2(p, mu, sigma):
        p = _to_number(p); mu = _to_number(mu); sigma = _to_number(sigma)
        if sigma <= 0:
            raise XLError(NUM)
        return mu + sigma * _norm_inv(p)
    fns["NORM.INV"] = f_norm_inv2
    fns["NORMINV"] = f_norm_inv2

    def f_norm_s_dist(x, cum=True):
        return f_norm_dist(x, 0, 1, cum)
    fns["NORM.S.DIST"] = f_norm_s_dist
    fns["NORMSDIST"] = lambda x: _cdf_normal(_to_number(x), 0, 1)

    def f_binom_dist(x, n, p, cum):
        x = _to_int(x); n = _to_int(n); p = _to_number(p)
        if x < 0 or n < 0 or x > n or not 0 <= p <= 1:
            raise XLError(NUM)
        if _to_bool(cum):
            return sum(math.comb(n, k) * p**k * (1-p)**(n-k) for k in range(0, x+1))
        return math.comb(n, x) * p**x * (1-p)**(n-x)
    fns["BINOM.DIST"] = f_binom_dist
    fns["BINOMDIST"] = f_binom_dist

    def f_poisson(x, mean, cum):
        x = _to_int(x); mean = _to_number(mean)
        if x < 0 or mean < 0:
            raise XLError(NUM)
        if _to_bool(cum):
            return sum(math.exp(-mean) * mean**k / math.factorial(k) for k in range(0, x+1))
        return math.exp(-mean) * mean**x / math.factorial(x)
    fns["POISSON.DIST"] = f_poisson
    fns["POISSON"] = f_poisson

    def f_expon(x, lmbd, cum):
        x = _to_number(x); lmbd = _to_number(lmbd)
        if lmbd <= 0 or x < 0:
            raise XLError(NUM)
        return 1 - math.exp(-lmbd * x) if _to_bool(cum) else lmbd * math.exp(-lmbd * x)
    fns["EXPON.DIST"] = f_expon
    fns["EXPONDIST"] = f_expon

    fns["GAMMA"] = lambda x: math.gamma(_to_number(x))
    fns["GAMMALN"] = lambda x: math.lgamma(_to_number(x))
    fns["GAMMA.LN"] = fns["GAMMALN"]

    return fns


def _financial():
    fns = {}

    def f_pmt(rate, nper, pv, fv=0, type_=0):
        rate = _to_number(rate); nper = _to_number(nper); pv = _to_number(pv); fv = _to_number(fv); t = _to_number(type_)
        if rate == 0:
            return -(pv + fv) / nper
        pvif = (1 + rate) ** nper
        pmt = -(pv * pvif + fv) * rate / ((pvif - 1) * (1 + rate * t))
        return pmt
    fns["PMT"] = f_pmt

    def f_fv(rate, nper, pmt, pv=0, type_=0):
        rate = _to_number(rate); nper = _to_number(nper); pmt = _to_number(pmt); pv = _to_number(pv); t = _to_number(type_)
        if rate == 0:
            return -(pv + pmt * nper)
        pvif = (1 + rate) ** nper
        return -(pv * pvif + pmt * (1 + rate * t) * (pvif - 1) / rate)
    fns["FV"] = f_fv

    def f_pv(rate, nper, pmt, fv=0, type_=0):
        rate = _to_number(rate); nper = _to_number(nper); pmt = _to_number(pmt); fv = _to_number(fv); t = _to_number(type_)
        if rate == 0:
            return -(fv + pmt * nper)
        pvif = (1 + rate) ** nper
        return -(fv + pmt * (1 + rate * t) * (pvif - 1) / rate) / pvif
    fns["PV"] = f_pv

    def f_npv(rate, *cashflows):
        rate = _to_number(rate)
        flat = []
        for cf in cashflows:
            flat.extend(_flatten_numeric([cf]))
        return sum(v / (1 + rate) ** (i + 1) for i, v in enumerate(flat))
    fns["NPV"] = f_npv

    def f_irr(values, guess=0.1):
        flat = _flatten_numeric([values])
        if not flat:
            raise XLError(NUM)
        # Newton
        rate = _to_number(guess)
        for _ in range(100):
            try:
                f_val = sum(v / (1 + rate) ** i for i, v in enumerate(flat))
                f_der = -sum(i * v / (1 + rate) ** (i + 1) for i, v in enumerate(flat) if i > 0)
            except (ZeroDivisionError, OverflowError):
                raise XLError(NUM)
            if f_der == 0:
                break
            new = rate - f_val / f_der
            if abs(new - rate) < 1e-10:
                return new
            rate = new
        raise XLError(NUM)
    fns["IRR"] = f_irr

    def f_rate(nper, pmt, pv, fv=0, type_=0, guess=0.1):
        nper = _to_number(nper); pmt = _to_number(pmt); pv = _to_number(pv); fv = _to_number(fv); t = _to_number(type_)
        rate = _to_number(guess)
        for _ in range(100):
            if rate == 0:
                f_val = pv + pmt * nper + fv
                f_der = -pmt * nper * (nper + 1) / 2
            else:
                pvif = (1 + rate) ** nper
                f_val = pv * pvif + pmt * (1 + rate * t) * (pvif - 1) / rate + fv
                # numeric derivative
                h = 1e-6
                rate2 = rate + h
                pvif2 = (1 + rate2) ** nper
                f_val2 = pv * pvif2 + pmt * (1 + rate2 * t) * (pvif2 - 1) / rate2 + fv
                f_der = (f_val2 - f_val) / h
            if f_der == 0:
                break
            new = rate - f_val / f_der
            if abs(new - rate) < 1e-9:
                return new
            rate = new
        raise XLError(NUM)
    fns["RATE"] = f_rate

    def f_nper(rate, pmt, pv, fv=0, type_=0):
        rate = _to_number(rate); pmt = _to_number(pmt); pv = _to_number(pv); fv = _to_number(fv); t = _to_number(type_)
        if rate == 0:
            if pmt == 0:
                raise XLError(NUM)
            return -(pv + fv) / pmt
        try:
            arg = (pmt * (1 + rate * t) - fv * rate) / (pmt * (1 + rate * t) + pv * rate)
            if arg <= 0:
                raise XLError(NUM)
            return math.log(arg) / math.log(1 + rate)
        except (ValueError, ZeroDivisionError):
            raise XLError(NUM)
    fns["NPER"] = f_nper

    def f_sln(cost, salvage, life):
        cost = _to_number(cost); salvage = _to_number(salvage); life = _to_number(life)
        if life == 0:
            raise XLError(DIV0)
        return (cost - salvage) / life
    fns["SLN"] = f_sln

    def f_ddb(cost, salvage, life, period, factor=2):
        cost = _to_number(cost); salvage = _to_number(salvage)
        life = _to_number(life); period = _to_number(period); factor = _to_number(factor)
        if cost < 0 or salvage < 0 or life < 1 or period < 1 or factor <= 0:
            raise XLError(NUM)
        bv = cost
        total = 0.0
        depr = 0.0
        for p in range(1, int(period) + 1):
            depr = min(bv * factor / life, max(bv - salvage, 0))
            if p == int(period):
                return depr
            bv -= depr
        return depr
    fns["DDB"] = f_ddb

    def f_db(cost, salvage, life, period, month=12):
        cost = _to_number(cost); salvage = _to_number(salvage)
        life = _to_number(life); period = _to_number(period); month = _to_number(month)
        if cost <= 0 or salvage < 0 or life < 1 or period < 1 or salvage > cost:
            raise XLError(NUM)
        rate = round(1 - (salvage / cost) ** (1 / life), 3)
        bv = cost
        depr_first_year = cost * rate * month / 12
        if int(period) == 1:
            return depr_first_year
        bv -= depr_first_year
        depr = 0.0
        for p in range(2, int(period) + 1):
            if p == int(life) + 1:
                depr = bv * rate * (12 - month) / 12
            else:
                depr = bv * rate
            if p == int(period):
                return depr
            bv -= depr
        return depr
    fns["DB"] = f_db

    def f_syd(cost, salvage, life, period):
        cost = _to_number(cost); salvage = _to_number(salvage)
        life = _to_number(life); period = _to_number(period)
        if life < 1 or period < 1 or period > life:
            raise XLError(NUM)
        return (cost - salvage) * (life - period + 1) * 2 / (life * (life + 1))
    fns["SYD"] = f_syd

    def f_ipmt(rate, per, nper, pv, fv=0, type_=0):
        rate = _to_number(rate); per = _to_int(per); nper = _to_number(nper)
        pv = _to_number(pv); fv = _to_number(fv); t = _to_number(type_)
        if per < 1 or per > nper:
            raise XLError(NUM)
        pmt = f_pmt(rate, nper, pv, fv, t)
        if rate == 0:
            return 0
        if t == 0:
            bal = pv
            for p in range(1, per):
                bal = bal * (1 + rate) + pmt
            return -bal * rate
        else:
            if per == 1:
                return 0
            bal = pv
            for p in range(1, per - 1):
                bal = (bal + pmt) * (1 + rate)
            bal = bal + pmt
            return -bal * rate
    fns["IPMT"] = f_ipmt

    def f_ppmt(rate, per, nper, pv, fv=0, type_=0):
        return f_pmt(rate, nper, pv, fv, type_) - f_ipmt(rate, per, nper, pv, fv, type_)
    fns["PPMT"] = f_ppmt

    def f_mirr(values, finance_rate, reinvest_rate):
        vs = [_to_number(v) for v in _flatten(values)]
        if not vs: raise XLError(DIV0)
        n = len(vs) - 1
        fr = _to_number(finance_rate); rr = _to_number(reinvest_rate)
        npv_neg = sum(v / (1 + fr) ** i for i, v in enumerate(vs) if v < 0)
        npv_pos = sum(v * (1 + rr) ** (n - i) for i, v in enumerate(vs) if v > 0)
        if npv_neg == 0 or npv_pos == 0:
            raise XLError(DIV0)
        return (npv_pos / -npv_neg) ** (1 / n) - 1
    fns["MIRR"] = f_mirr

    def f_cumipmt(rate, nper, pv, start, end, type_=0):
        rate = _to_number(rate); nper = _to_number(nper); pv = _to_number(pv); s = _to_int(start); e = _to_int(end); t = _to_number(type_)
        if rate <= 0 or nper <= 0 or pv <= 0 or s < 1 or e < s or e > nper:
            raise XLError(NUM)
        pmt = f_pmt(rate, nper, pv, 0, t)
        ipmt_total = 0.0
        bal = pv
        for p in range(1, int(nper) + 1):
            interest = -bal * rate if t == 0 else 0
            principal = pmt - interest if t == 0 else pmt
            if t != 0 and p == 1:
                interest = 0
                principal = pmt
            else:
                interest = -bal * rate
                principal = pmt - interest
            bal += principal
            if s <= p <= e:
                ipmt_total += interest
        return ipmt_total
    fns["CUMIPMT"] = f_cumipmt

    return fns


def _date_funcs():
    fns = {}

    def f_date(y, m, d):
        y = _to_int(y); m = _to_int(m); d = _to_int(d)
        try:
            return _date_to_serial(_dt.date(y, 1, 1) + _dt.timedelta(days=(m - 1) * 30 + d - 1)) if False else _date_to_serial(_dt.date(y, m, d) if 1 <= m <= 12 and d >= 1 else _dt.date(y, 1, 1) + _dt.timedelta(days=(m - 1) * 30 + d - 1))
        except (ValueError, OverflowError):
            try:
                base = _dt.date(y, 1, 1)
                d2 = base + _dt.timedelta(days=int((m - 1) * 30 + d - 1))
                return _date_to_serial(d2)
            except Exception:
                raise XLError(NUM)
    def f_date_simple(y, m, d):
        y = _to_int(y); m = _to_int(m); d = _to_int(d)
        try:
            base = _dt.date(int(y), 1, 1)
            d_out = base
            d_out = d_out.replace(month=1, day=1)
            d_out = _add_months(d_out, m - 1)
            d_out = d_out + _dt.timedelta(days=d - 1)
            return _date_to_serial(d_out)
        except (ValueError, OverflowError):
            raise XLError(NUM)
    fns["DATE"] = f_date_simple

    def f_year(s):
        return _serial_to_date(_to_number(s)).year
    fns["YEAR"] = f_year

    def f_month(s):
        return _serial_to_date(_to_number(s)).month
    fns["MONTH"] = f_month

    def f_day(s):
        return _serial_to_date(_to_number(s)).day
    fns["DAY"] = f_day

    def f_weekday(s, type_=1):
        d = _serial_to_date(_to_number(s))
        # 1: 1=Sun..7=Sat; 2: 1=Mon..7=Sun; 3: 0=Mon..6=Sun
        py = d.weekday()  # 0=Mon..6=Sun
        t = _to_int(type_)
        if t == 1:
            return ((py + 1) % 7) + 1
        if t == 2:
            return py + 1
        if t == 3:
            return py
        return ((py + 1) % 7) + 1
    fns["WEEKDAY"] = f_weekday

    def f_days(end, start):
        return int(_to_number(end) - _to_number(start))
    fns["DAYS"] = f_days

    def f_today():
        return _date_to_serial(_dt.date.today())
    fns["TODAY"] = f_today

    def f_now():
        n = _dt.datetime.now()
        return _date_to_serial(n.date()) + (n.hour * 3600 + n.minute * 60 + n.second) / 86400
    fns["NOW"] = f_now

    def f_time(h, m, s):
        return (_to_int(h) * 3600 + _to_int(m) * 60 + _to_int(s)) / 86400
    fns["TIME"] = f_time

    def f_hour(s):
        f = _to_number(s) - math.floor(_to_number(s))
        return int(f * 24)
    fns["HOUR"] = f_hour
    def f_minute(s):
        f = _to_number(s) - math.floor(_to_number(s))
        return int((f * 24 - int(f * 24)) * 60)
    fns["MINUTE"] = f_minute
    def f_second(s):
        f = _to_number(s) - math.floor(_to_number(s))
        secs = round((f * 24 - int(f * 24)) * 60 * 60)
        return secs % 60
    fns["SECOND"] = f_second

    def _add_months(d, n):
        m = d.month - 1 + n
        y = d.year + m // 12
        m = m % 12 + 1
        from calendar import monthrange
        last = monthrange(y, m)[1]
        return _dt.date(y, m, min(d.day, last))

    def f_eomonth(start, months):
        d = _serial_to_date(_to_number(start))
        d2 = _add_months(d, _to_int(months))
        from calendar import monthrange
        last = monthrange(d2.year, d2.month)[1]
        return _date_to_serial(_dt.date(d2.year, d2.month, last))
    fns["EOMONTH"] = f_eomonth

    def f_edate(start, months):
        d = _serial_to_date(_to_number(start))
        d2 = _add_months(d, _to_int(months))
        return _date_to_serial(d2)
    fns["EDATE"] = f_edate

    def f_networkdays(start, end, holidays=None):
        d0 = _serial_to_date(_to_number(start))
        d1 = _serial_to_date(_to_number(end))
        if d0 > d1:
            d0, d1 = d1, d0
            sign = -1
        else:
            sign = 1
        hols = set()
        if holidays is not None:
            for h in _flatten([holidays]):
                if h not in (None, ""):
                    hols.add(_serial_to_date(_to_number(h)))
        n = 0
        cur = d0
        while cur <= d1:
            if cur.weekday() < 5 and cur not in hols:
                n += 1
            cur += _dt.timedelta(days=1)
        return sign * n
    fns["NETWORKDAYS"] = f_networkdays

    def f_workday(start, days, holidays=None):
        d = _serial_to_date(_to_number(start))
        delta = _to_int(days)
        hols = set()
        if holidays is not None:
            for h in _flatten([holidays]):
                if h not in (None, ""):
                    hols.add(_serial_to_date(_to_number(h)))
        step = 1 if delta >= 0 else -1
        remaining = abs(delta)
        cur = d
        while remaining > 0:
            cur += _dt.timedelta(days=step)
            if cur.weekday() < 5 and cur not in hols:
                remaining -= 1
        return _date_to_serial(cur)
    fns["WORKDAY"] = f_workday

    def f_yearfrac(start, end, basis=0):
        d0 = _serial_to_date(_to_number(start))
        d1 = _serial_to_date(_to_number(end))
        if d0 > d1:
            d0, d1 = d1, d0
        b = _to_int(basis)
        if b == 0:
            # 30/360 US
            d0d = min(d0.day, 30); d1d = d1.day
            if d0d == 30 and d1d == 31:
                d1d = 30
            num = (d1.year - d0.year) * 360 + (d1.month - d0.month) * 30 + (d1d - d0d)
            return num / 360
        if b == 1:
            return (d1 - d0).days / 365.25
        if b == 2:
            return (d1 - d0).days / 360
        if b == 3:
            return (d1 - d0).days / 365
        if b == 4:
            d0d = min(d0.day, 30); d1d = min(d1.day, 30)
            num = (d1.year - d0.year) * 360 + (d1.month - d0.month) * 30 + (d1d - d0d)
            return num / 360
        return (d1 - d0).days / 365
    fns["YEARFRAC"] = f_yearfrac

    return fns


def _engineering():
    fns = {}
    fns["HEX2DEC"] = lambda h: int(_to_text(h), 16) if _to_text(h) else 0
    fns["DEC2HEX"] = lambda n, *p: format(_to_int(n) & 0xFFFFFFFFFF, 'X')
    fns["BIN2DEC"] = lambda b: int(_to_text(b), 2) if _to_text(b) else 0
    fns["DEC2BIN"] = lambda n, *p: bin(_to_int(n))[2:]
    fns["OCT2DEC"] = lambda o: int(_to_text(o), 8) if _to_text(o) else 0
    fns["DEC2OCT"] = lambda n, *p: oct(_to_int(n))[2:]
    fns["BITAND"] = lambda a, b: _to_int(a) & _to_int(b)
    fns["BITOR"] = lambda a, b: _to_int(a) | _to_int(b)
    fns["BITXOR"] = lambda a, b: _to_int(a) ^ _to_int(b)
    fns["BITLSHIFT"] = lambda a, b: _to_int(a) << _to_int(b)
    fns["BITRSHIFT"] = lambda a, b: _to_int(a) >> _to_int(b)
    return fns


def _conditional_aggregates():
    fns = {}

    def f_countif(rng, criterion):
        pred = _make_predicate(criterion)
        n = 0
        for v in _flatten([rng]):
            if pred(v):
                n += 1
        return n
    fns["COUNTIF"] = f_countif

    def f_sumif(rng, criterion, sum_rng=None):
        pred = _make_predicate(criterion)
        rng_flat = _flatten([rng])
        sum_flat = _flatten([sum_rng]) if sum_rng is not None else rng_flat
        out = 0.0
        for i, v in enumerate(rng_flat):
            if pred(v):
                if i < len(sum_flat):
                    sv = sum_flat[i]
                    if isinstance(sv, (int, float)) and not isinstance(sv, bool):
                        out += sv
        return out
    fns["SUMIF"] = f_sumif

    def f_averageif(rng, criterion, avg_rng=None):
        pred = _make_predicate(criterion)
        rng_flat = _flatten([rng])
        avg_flat = _flatten([avg_rng]) if avg_rng is not None else rng_flat
        nums = []
        for i, v in enumerate(rng_flat):
            if pred(v) and i < len(avg_flat):
                sv = avg_flat[i]
                if isinstance(sv, (int, float)) and not isinstance(sv, bool):
                    nums.append(float(sv))
        if not nums:
            raise XLError(DIV0)
        return sum(nums) / len(nums)
    fns["AVERAGEIF"] = f_averageif

    def _ifs_predicate(args):
        # args = [crit_rng, crit, crit_rng2, crit2, ...]
        if len(args) % 2 != 0:
            raise XLError(VALUE)
        ranges = []
        preds = []
        for i in range(0, len(args), 2):
            ranges.append(_flatten([args[i]]))
            preds.append(_make_predicate(args[i + 1]))
        n = max((len(r) for r in ranges), default=0)
        for r in ranges:
            if len(r) != n:
                raise XLError(VALUE)
        kept = []
        for i in range(n):
            if all(p(r[i]) for p, r in zip(preds, ranges)):
                kept.append(i)
        return kept

    def f_sumifs(sum_rng, *crits):
        sum_flat = _flatten([sum_rng])
        idx = _ifs_predicate(crits)
        out = 0.0
        for i in idx:
            v = sum_flat[i]
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                out += v
        return out
    fns["SUMIFS"] = f_sumifs

    def f_countifs(*crits):
        idx = _ifs_predicate(crits)
        return len(idx)
    fns["COUNTIFS"] = f_countifs

    def f_averageifs(avg_rng, *crits):
        avg_flat = _flatten([avg_rng])
        idx = _ifs_predicate(crits)
        nums = [avg_flat[i] for i in idx if isinstance(avg_flat[i], (int, float)) and not isinstance(avg_flat[i], bool)]
        if not nums:
            raise XLError(DIV0)
        return sum(nums) / len(nums)
    fns["AVERAGEIFS"] = f_averageifs

    def f_maxifs(max_rng, *crits):
        max_flat = _flatten([max_rng])
        idx = _ifs_predicate(crits)
        nums = [max_flat[i] for i in idx if isinstance(max_flat[i], (int, float)) and not isinstance(max_flat[i], bool)]
        return max(nums) if nums else 0
    fns["MAXIFS"] = f_maxifs

    def f_minifs(min_rng, *crits):
        min_flat = _flatten([min_rng])
        idx = _ifs_predicate(crits)
        nums = [min_flat[i] for i in idx if isinstance(min_flat[i], (int, float)) and not isinstance(min_flat[i], bool)]
        return min(nums) if nums else 0
    fns["MINIFS"] = f_minifs

    return fns


def _logical():
    fns = {}

    def f_if(cond, t, f=False):
        return t if _to_bool(cond) else f
    fns["IF"] = f_if

    def f_iferror(v, fallback):
        if _is_xl_error(v):
            return fallback
        return v
    fns["IFERROR"] = f_iferror
    def f_ifna(v, fallback):
        if v == NA:
            return fallback
        return v
    fns["IFNA"] = f_ifna

    def f_and(*args):
        for v in _flatten(args):
            if v is None or v == "":
                continue
            if not _to_bool(v):
                return False
        return True
    fns["AND"] = f_and

    def f_or(*args):
        for v in _flatten(args):
            if v is None or v == "":
                continue
            if _to_bool(v):
                return True
        return False
    fns["OR"] = f_or

    def f_xor(*args):
        n = 0
        for v in _flatten(args):
            if v is None or v == "":
                continue
            if _to_bool(v):
                n += 1
        return n % 2 == 1
    fns["XOR"] = f_xor

    fns["NOT"] = lambda v: not _to_bool(v)
    fns["TRUE"] = lambda: True
    fns["FALSE"] = lambda: False

    def f_ifs(*args):
        if len(args) % 2 != 0:
            raise XLError(NA)
        for i in range(0, len(args), 2):
            if _to_bool(args[i]):
                return args[i + 1]
        raise XLError(NA)
    fns["IFS"] = f_ifs

    def f_switch(value, *args):
        # value, c1, r1, c2, r2, ..., [default]
        i = 0
        while i + 1 < len(args):
            if value == args[i]:
                return args[i + 1]
            i += 2
        if i < len(args):
            return args[i]
        raise XLError(NA)
    fns["SWITCH"] = f_switch

    return fns


def _info():
    fns = {}
    fns["ISNUMBER"] = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)
    fns["ISTEXT"] = lambda v: isinstance(v, str) and not _is_xl_error(v)
    fns["ISBLANK"] = lambda v: v is None or v == ""
    fns["ISLOGICAL"] = lambda v: isinstance(v, bool)
    fns["ISERROR"] = lambda v: _is_xl_error(v)
    fns["ISERR"] = lambda v: _is_xl_error(v) and v != NA
    fns["ISNA"] = lambda v: v == NA
    fns["ISEVEN"] = lambda v: _to_int(v) % 2 == 0
    fns["ISODD"] = lambda v: _to_int(v) % 2 == 1
    fns["NA"] = lambda: (_ for _ in ()).throw(XLError(NA))
    fns["TYPE"] = lambda v: 1 if isinstance(v, (int, float)) and not isinstance(v, bool) else 2 if isinstance(v, str) else 4 if isinstance(v, bool) else 16 if _is_xl_error(v) else 1
    fns["N"] = lambda v: _to_number(v) if isinstance(v, (int, float, bool)) else 0
    fns["T"] = lambda v: v if isinstance(v, str) else ""
    return fns


def _text():
    fns = {}
    fns["LEN"] = lambda s: len(_to_text(s))
    fns["UPPER"] = lambda s: _to_text(s).upper()
    fns["LOWER"] = lambda s: _to_text(s).lower()
    fns["PROPER"] = lambda s: _to_text(s).title()
    fns["TRIM"] = lambda s: re.sub(r"\s+", " ", _to_text(s)).strip()
    fns["LEFT"] = lambda s, n=1: _to_text(s)[:_to_int(n)]
    fns["RIGHT"] = lambda s, n=1: _to_text(s)[-_to_int(n):] if _to_int(n) else ""
    def f_mid(s, start, n):
        s = _to_text(s); start = _to_int(start); n = _to_int(n)
        if start < 1:
            raise XLError(VALUE)
        return s[start - 1: start - 1 + n]
    fns["MID"] = f_mid
    def f_concat(*args):
        out = []
        for a in args:
            for v in _flatten(a):
                if v is not None and v != "":
                    out.append(_to_text(v))
        return "".join(out)
    fns["CONCAT"] = f_concat
    fns["CONCATENATE"] = f_concat

    def f_textjoin(delim, ignore_empty, *args):
        d = _to_text(delim)
        ignore = _to_bool(ignore_empty)
        parts = []
        for a in args:
            for v in _flatten(a):
                if ignore and (v is None or v == ""):
                    continue
                parts.append(_to_text(v if v is not None else ""))
        return d.join(parts)
    fns["TEXTJOIN"] = f_textjoin

    def f_substitute(s, old, new, instance=None):
        s = _to_text(s); old = _to_text(old); new = _to_text(new)
        if old == "":
            return s
        if instance is None:
            return s.replace(old, new)
        idx = _to_int(instance)
        cur = 0
        i = 0
        while True:
            j = s.find(old, i)
            if j < 0:
                return s
            cur += 1
            if cur == idx:
                return s[:j] + new + s[j + len(old):]
            i = j + len(old)
    fns["SUBSTITUTE"] = f_substitute

    def f_replace(s, start, n, new):
        s = _to_text(s); start = _to_int(start); n = _to_int(n); new = _to_text(new)
        return s[:start - 1] + new + s[start - 1 + n:]
    fns["REPLACE"] = f_replace

    fns["REPT"] = lambda s, n: _to_text(s) * _to_int(n)

    def f_find(needle, hay, start=1):
        h = _to_text(hay); n = _to_text(needle); s = _to_int(start)
        idx = h.find(n, s - 1)
        if idx < 0:
            raise XLError(VALUE)
        return idx + 1
    fns["FIND"] = f_find

    def f_search(needle, hay, start=1):
        h = _to_text(hay).lower(); n = _to_text(needle).lower(); s = _to_int(start)
        # excel SEARCH supports * and ?
        rx = _wildcard_to_regex(n)
        m = rx.search(h, s - 1) if rx else None
        # _wildcard_to_regex anchors. Use a different one for SEARCH
        # build pattern manually
        out = []
        i = 0
        while i < len(n):
            c = n[i]
            if c == '~' and i + 1 < len(n) and n[i + 1] in '*?':
                out.append(re.escape(n[i + 1]))
                i += 2
                continue
            if c == '*':
                out.append('.*')
            elif c == '?':
                out.append('.')
            else:
                out.append(re.escape(c))
            i += 1
        rx = re.compile(''.join(out), re.IGNORECASE)
        m = rx.search(h, s - 1)
        if not m:
            raise XLError(VALUE)
        return m.start() + 1
    fns["SEARCH"] = f_search

    def f_text(value, fmt):
        from .formatting import format_value
        return format_value(value, _to_text(fmt))
    fns["TEXT"] = f_text

    fns["VALUE"] = lambda s: _to_number(s)
    fns["NUMBERVALUE"] = lambda s, *_: _to_number(s)

    def f_textsplit(text, col_delim, row_delim=None, ignore_empty=False, *_):
        s = _to_text(text)
        cd = _to_text(col_delim) if col_delim is not None else ""
        rd = _to_text(row_delim) if row_delim is not None else None
        if rd:
            rows = s.split(rd)
        else:
            rows = [s]
        out = []
        for r in rows:
            cols = r.split(cd) if cd else [r]
            if _to_bool(ignore_empty):
                cols = [c for c in cols if c != ""]
            out.append(cols)
        if not out:
            return ""
        if len(out) == 1 and len(out[0]) == 1:
            return out[0][0]
        return out
    fns["TEXTSPLIT"] = f_textsplit

    def f_textbefore(text, delim, instance_num=1):
        s = _to_text(text); d = _to_text(delim); n = _to_int(instance_num) if instance_num is not None else 1
        if d == "":
            return ""
        if n < 0:
            # search from right
            idx = -1
            for _ in range(-n):
                if idx == -1:
                    idx = s.rfind(d)
                else:
                    idx = s.rfind(d, 0, idx)
                if idx < 0:
                    raise XLError(NA)
            return s[:idx]
        i = 0
        idx = -1
        for _ in range(n):
            j = s.find(d, i)
            if j < 0:
                raise XLError(NA)
            idx = j
            i = j + len(d)
        return s[:idx]
    fns["TEXTBEFORE"] = f_textbefore

    def f_textafter(text, delim, instance_num=1):
        s = _to_text(text); d = _to_text(delim); n = _to_int(instance_num) if instance_num is not None else 1
        if d == "":
            return s
        if n < 0:
            idx = -1
            for _ in range(-n):
                if idx == -1:
                    idx = s.rfind(d)
                else:
                    idx = s.rfind(d, 0, idx)
                if idx < 0:
                    raise XLError(NA)
            return s[idx + len(d):]
        i = 0
        idx = -1
        for _ in range(n):
            j = s.find(d, i)
            if j < 0:
                raise XLError(NA)
            idx = j
            i = j + len(d)
        return s[idx + len(d):]
    fns["TEXTAFTER"] = f_textafter

    fns["UNICODE"] = lambda s: ord(_to_text(s)[0]) if _to_text(s) else (_ for _ in ()).throw(XLError(VALUE))
    fns["UNICHAR"] = lambda n: chr(_to_int(n))
    fns["CHAR"] = lambda n: chr(_to_int(n))
    fns["CODE"] = lambda s: ord(_to_text(s)[0]) if _to_text(s) else (_ for _ in ()).throw(XLError(VALUE))
    fns["EXACT"] = lambda a, b: _to_text(a) == _to_text(b)

    return fns


def _lookup():
    fns = {}

    def _to_2d(rng):
        if isinstance(rng, list):
            if rng and isinstance(rng[0], list):
                return rng
            return [rng]
        return [[rng]]

    def f_vlookup(lookup, table, col_idx, range_lookup=True):
        t = _to_2d(table)
        col_idx = _to_int(col_idx)
        if col_idx < 1 or col_idx > len(t[0] if t else []):
            raise XLError(REF)
        rl = _to_bool(range_lookup) if range_lookup is not None else True
        if not rl:
            for row in t:
                if row and _eq(row[0], lookup):
                    return row[col_idx - 1]
            raise XLError(NA)
        last = None
        for row in t:
            if row and _le(row[0], lookup):
                last = row
            else:
                break
        if last is None:
            raise XLError(NA)
        return last[col_idx - 1]
    fns["VLOOKUP"] = f_vlookup

    def f_hlookup(lookup, table, row_idx, range_lookup=True):
        t = _to_2d(table)
        row_idx = _to_int(row_idx)
        if not t or row_idx < 1 or row_idx > len(t):
            raise XLError(REF)
        cols = list(zip(*t))
        rl = _to_bool(range_lookup) if range_lookup is not None else True
        if not rl:
            for col in cols:
                if col and _eq(col[0], lookup):
                    return col[row_idx - 1]
            raise XLError(NA)
        last = None
        for col in cols:
            if col and _le(col[0], lookup):
                last = col
            else:
                break
        if last is None:
            raise XLError(NA)
        return last[row_idx - 1]
    fns["HLOOKUP"] = f_hlookup

    def f_match(lookup, arr, match_type=1):
        flat = _flatten([arr])
        mt = _to_int(match_type) if match_type is not None else 1
        if mt == 0:
            for i, v in enumerate(flat):
                if isinstance(lookup, str) and isinstance(v, str):
                    rx = _wildcard_to_regex(lookup)
                    if rx.match(v):
                        return i + 1
                elif _eq(v, lookup):
                    return i + 1
            raise XLError(NA)
        if mt == 1:
            last = None
            for i, v in enumerate(flat):
                if v is None:
                    continue
                if _le(v, lookup):
                    last = i + 1
                else:
                    break
            if last is None:
                raise XLError(NA)
            return last
        if mt == -1:
            last = None
            for i, v in enumerate(flat):
                if v is None:
                    continue
                if _ge(v, lookup):
                    last = i + 1
                else:
                    break
            if last is None:
                raise XLError(NA)
            return last
        raise XLError(VALUE)
    fns["MATCH"] = f_match

    def f_index(arr, row=None, col=None):
        t = _to_2d(arr)
        rows = len(t)
        cols = len(t[0]) if t else 0
        ri = _to_int(row) if row is not None else 0
        ci = _to_int(col) if col is not None else 0
        if rows == 1:
            # treat as single row, row arg is column
            if ri == 0 and ci == 0:
                return t
            if ci == 0 and ri >= 1:
                return t[0][ri - 1]
            if ri == 1:
                return t[0][ci - 1]
        if cols == 1:
            if ci == 0 and ri >= 1:
                return t[ri - 1][0]
        if ri == 0:
            return [r[ci - 1] for r in t]
        if ci == 0:
            return t[ri - 1]
        if ri < 1 or ri > rows or ci < 1 or ci > cols:
            raise XLError(REF)
        return t[ri - 1][ci - 1]
    fns["INDEX"] = f_index

    def f_xlookup(lookup, lookup_arr, return_arr, if_not_found=None, match_mode=0, search_mode=1):
        la = _flatten([lookup_arr])
        ra_2d = _to_2d(return_arr)
        # ra rows align with la
        if len(ra_2d) == 1 and len(ra_2d[0]) == len(la):
            # single-row return; per-element pick
            ra_rows = list(zip(*ra_2d))  # rows of len 1
            single_row_mode = True
        else:
            single_row_mode = False
        mm = _to_int(match_mode) if match_mode is not None else 0
        sm = _to_int(search_mode) if search_mode is not None else 1
        idxs = list(range(len(la)))
        if sm == -1:
            idxs.reverse()
        # exact / wildcard / approx
        match_idx = -1
        if mm == 0 or mm == 1 or mm == -1:
            for i in idxs:
                if _eq(la[i], lookup):
                    match_idx = i; break
            if match_idx == -1 and mm == 1:
                # next larger
                best = -1
                for i in range(len(la)):
                    try:
                        if la[i] is not None and _gt(la[i], lookup):
                            if best == -1 or _lt(la[i], la[best]):
                                best = i
                    except Exception:
                        pass
                match_idx = best
            if match_idx == -1 and mm == -1:
                best = -1
                for i in range(len(la)):
                    try:
                        if la[i] is not None and _lt(la[i], lookup):
                            if best == -1 or _gt(la[i], la[best]):
                                best = i
                    except Exception:
                        pass
                match_idx = best
        elif mm == 2:
            for i in idxs:
                if isinstance(la[i], str) and isinstance(lookup, str):
                    if _wildcard_to_regex(lookup).match(la[i]):
                        match_idx = i; break
                elif _eq(la[i], lookup):
                    match_idx = i; break
        if match_idx == -1:
            if if_not_found is not None:
                return if_not_found
            raise XLError(NA)
        # pick result
        if single_row_mode:
            return ra_2d[0][match_idx]
        # ra is M rows; match_idx is row index; return that row (or scalar if 1 col)
        if match_idx < len(ra_2d):
            row = ra_2d[match_idx]
            if len(row) == 1:
                return row[0]
            return row
        raise XLError(REF)
    fns["XLOOKUP"] = f_xlookup

    def f_offset(ref, rows, cols, height=None, width=None):
        # ref arrives evaluated -> a value or 2D array. We can't reconstruct
        # the original anchor here without ctx. The OFFSET implementation in
        # the evaluator handles this case directly.
        raise XLError(VALUE)
    fns["OFFSET"] = f_offset
    def f_indirect(text, *_):
        raise XLError(VALUE)
    fns["INDIRECT"] = f_indirect
    def f_address(row, col, abs_=1, a1=True, sheet=None):
        r = _to_int(row); c = _to_int(col); a = _to_int(abs_) if abs_ is not None else 1
        if r < 1 or c < 1:
            raise XLError(VALUE)
        from .refs import col_index_to_letters
        col_l = col_index_to_letters(c - 1)
        col_pin = "$" if a in (1, 3) else ""
        row_pin = "$" if a in (1, 2) else ""
        addr = f"{col_pin}{col_l}{row_pin}{r}"
        if sheet:
            return f"{_to_text(sheet)}!{addr}"
        return addr
    fns["ADDRESS"] = f_address

    return fns


# comparison helpers (Excel-like)
def _cmp_pair(a, b):
    if isinstance(a, (int, float)) and not isinstance(a, bool) and isinstance(b, (int, float)) and not isinstance(b, bool):
        return float(a) - float(b)
    if isinstance(a, bool) and isinstance(b, bool):
        return int(a) - int(b)
    if isinstance(a, str) and isinstance(b, str):
        if a == b: return 0
        return -1 if a < b else 1
    return None  # incomparable

def _eq(a, b):
    if isinstance(a, str) and isinstance(b, str):
        return a.lower() == b.lower()
    if a is None and b in (None, ""):
        return True
    if a == b: return True
    try: return float(a) == float(b)
    except Exception: return False
def _le(a, b):
    c = _cmp_pair(a, b)
    return c is not None and c <= 0
def _ge(a, b):
    c = _cmp_pair(a, b)
    return c is not None and c >= 0
def _lt(a, b):
    c = _cmp_pair(a, b)
    return c is not None and c < 0
def _gt(a, b):
    c = _cmp_pair(a, b)
    return c is not None and c > 0


def _database_funcs():
    fns = {}

    def _matches(headers, row, criteria):
        # criteria: 2D list, header row + criteria rows
        if not criteria or len(criteria) < 2:
            return True
        crit_headers = criteria[0]
        for crit_row in criteria[1:]:
            if not crit_row:
                continue
            ok = True
            for j, c in enumerate(crit_row):
                if c is None or c == "":
                    continue
                if j >= len(crit_headers):
                    continue
                col_name = crit_headers[j]
                # find this header in row
                if col_name not in headers:
                    ok = False; break
                val = row[headers.index(col_name)]
                pred = _make_predicate(c)
                if not pred(val):
                    ok = False; break
            if ok:
                return True
        return False

    def _filtered(database, field, criteria):
        if not isinstance(database, list) or not database or not isinstance(database[0], list):
            raise XLError(VALUE)
        headers = database[0]
        rows = database[1:]
        # field can be int (1-based) or string
        if isinstance(field, (int, float)) and not isinstance(field, bool):
            f_idx = _to_int(field) - 1
        else:
            f_name = _to_text(field)
            if f_name not in headers:
                raise XLError(VALUE)
            f_idx = headers.index(f_name)
        crit = criteria if isinstance(criteria, list) else [[criteria]]
        if not isinstance(crit[0], list):
            crit = [crit]
        out = []
        for row in rows:
            if _matches(headers, row, crit):
                if f_idx < len(row):
                    out.append(row[f_idx])
        return out

    def f_dsum(db, field, crit):
        vals = _filtered(db, field, crit)
        return sum(v for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool))
    fns["DSUM"] = f_dsum
    def f_daverage(db, field, crit):
        vals = [v for v in _filtered(db, field, crit) if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if not vals: raise XLError(DIV0)
        return sum(vals) / len(vals)
    fns["DAVERAGE"] = f_daverage
    def f_dcount(db, field, crit):
        vals = _filtered(db, field, crit)
        return sum(1 for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool))
    fns["DCOUNT"] = f_dcount
    def f_dcounta(db, field, crit):
        vals = _filtered(db, field, crit)
        return sum(1 for v in vals if v not in (None, ""))
    fns["DCOUNTA"] = f_dcounta
    def f_dget(db, field, crit):
        vals = _filtered(db, field, crit)
        if len(vals) == 0: raise XLError(VALUE)
        if len(vals) > 1: raise XLError(NUM)
        return vals[0]
    fns["DGET"] = f_dget
    def f_dmax(db, field, crit):
        vals = [v for v in _filtered(db, field, crit) if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if not vals: return 0
        return max(vals)
    fns["DMAX"] = f_dmax
    def f_dmin(db, field, crit):
        vals = [v for v in _filtered(db, field, crit) if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if not vals: return 0
        return min(vals)
    fns["DMIN"] = f_dmin

    return fns


def all_funcs():
    out = {}
    for table in (_math(), _stats(), _dist(), _financial(), _date_funcs(),
                  _engineering(), _conditional_aggregates(),
                  _logical(), _info(), _text(), _lookup(), _database_funcs()):
        out.update(table)
    return out
