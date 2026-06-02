"""Built-in spreadsheet functions."""
import math, re, datetime as _dt, statistics as _stats
from values import err, is_err, is_num, coerce_num, coerce_str, coerce_bool, Err, EPOCH, date_to_serial, serial_to_date, serial_to_datetime

FUNCS = {}

def register(*names):
    def deco(fn):
        for n in names:
            FUNCS[n.upper()] = fn
        return fn
    return deco

def is_array(v): return isinstance(v, list)

def flatten(v):
    if not is_array(v): return [v]
    out = []
    for row in v:
        if isinstance(row, list):
            for c in row: out.append(c)
        else: out.append(row)
    return out

def as_2d(v):
    if not is_array(v): return [[v]]
    if v and not isinstance(v[0], list): return [v]
    return v

def _n(v): return coerce_num(v)
def _i(v):
    n = coerce_num(v)
    if is_err(n): return n
    return int(n)
def _s(v): return coerce_str(v)
def _b(v): return coerce_bool(v)

# ---------- math/aggregates ----------
@register('SUM')
def _f(ctx, args):
    t = 0
    for a in args:
        for x in flatten(a):
            if x is None or x == '' or isinstance(x, bool): continue
            if is_err(x): return x
            if isinstance(x, (int, float)): t += x
    return t

@register('AVERAGE')
def _f(ctx, args):
    nums = []
    for a in args:
        for x in flatten(a):
            if isinstance(x, bool) or isinstance(x, str) or x is None: continue
            if is_err(x): return x
            if isinstance(x, (int, float)): nums.append(x)
    if not nums: return err('#DIV/0!')
    return sum(nums) / len(nums)

@register('COUNT')
def _f(ctx, args):
    n = 0
    for a in args:
        for x in flatten(a):
            if isinstance(x, (int, float)) and not isinstance(x, bool): n += 1
    return n

@register('COUNTA')
def _f(ctx, args):
    n = 0
    for a in args:
        for x in flatten(a):
            if x is None or x == '': continue
            n += 1
    return n

@register('COUNTBLANK')
def _f(ctx, args):
    n = 0
    for a in args:
        for x in flatten(a):
            if x is None or x == '': n += 1
    return n

@register('MIN')
def _f(ctx, args):
    nums = []
    for a in args:
        for x in flatten(a):
            if is_err(x): return x
            if isinstance(x, (int, float)) and not isinstance(x, bool): nums.append(x)
    return min(nums) if nums else 0

@register('MAX')
def _f(ctx, args):
    nums = []
    for a in args:
        for x in flatten(a):
            if is_err(x): return x
            if isinstance(x, (int, float)) and not isinstance(x, bool): nums.append(x)
    return max(nums) if nums else 0

@register('PRODUCT')
def _f(ctx, args):
    p = 1; seen = False
    for a in args:
        for x in flatten(a):
            if is_err(x): return x
            if isinstance(x, (int, float)) and not isinstance(x, bool):
                p *= x; seen = True
    return p if seen else 0

@register('POWER')
def _f(ctx, args):
    a = _n(args[0]); b = _n(args[1])
    if is_err(a): return a
    if is_err(b): return b
    try:
        r = math.pow(a, b)
        if math.isnan(r) or math.isinf(r): return err('#NUM!')
        return r
    except: return err('#NUM!')

@register('SQRT')
def _f(ctx, args):
    a = _n(args[0])
    if is_err(a): return a
    if a < 0: return err('#NUM!')
    return math.sqrt(a)

@register('SQRTPI')
def _f(ctx, args):
    a = _n(args[0])
    if is_err(a): return a
    if a < 0: return err('#NUM!')
    return math.sqrt(a * math.pi)

@register('MOD')
def _f(ctx, args):
    a = _n(args[0]); b = _n(args[1])
    if is_err(a): return a
    if is_err(b): return b
    if b == 0: return err('#DIV/0!')
    return a - b * math.floor(a / b)

@register('ABS')
def _f(ctx, args):
    a = _n(args[0])
    if is_err(a): return a
    return abs(a)

@register('ROUND')
def _f(ctx, args):
    a = _n(args[0]); k = _i(args[1]) if len(args) > 1 else 0
    if is_err(a): return a
    if is_err(k): return k
    f = 10.0 ** k
    if a >= 0:
        v = math.floor(a * f + 0.5) / f
    else:
        v = -math.floor(-a * f + 0.5) / f
    if isinstance(v, float) and v.is_integer() and k <= 0: return int(v)
    return v

@register('ROUNDUP')
def _f(ctx, args):
    a = _n(args[0]); k = _i(args[1]) if len(args) > 1 else 0
    if is_err(a): return a
    f = 10.0 ** k
    return (math.ceil(a * f) if a >= 0 else -math.ceil(-a * f)) / f

@register('ROUNDDOWN', 'TRUNC')
def _f(ctx, args):
    a = _n(args[0]); k = _i(args[1]) if len(args) > 1 else 0
    if is_err(a): return a
    f = 10.0 ** k
    return (math.floor(a * f) if a >= 0 else -math.floor(-a * f)) / f

@register('CEILING')
def _f(ctx, args):
    a = _n(args[0]); s = _n(args[1]) if len(args) > 1 else 1
    if is_err(a): return a
    if is_err(s): return s
    if s == 0: return 0
    return math.ceil(a / s) * s

@register('FLOOR')
def _f(ctx, args):
    a = _n(args[0]); s = _n(args[1]) if len(args) > 1 else 1
    if is_err(a): return a
    if is_err(s): return s
    if s == 0: return err('#DIV/0!')
    return math.floor(a / s) * s

@register('INT')
def _f(ctx, args):
    a = _n(args[0])
    if is_err(a): return a
    return math.floor(a)

@register('SIGN')
def _f(ctx, args):
    a = _n(args[0])
    if is_err(a): return a
    return (a > 0) - (a < 0)

@register('PI')
def _f(ctx, args): return math.pi

@register('EXP')
def _f(ctx, args):
    a = _n(args[0])
    if is_err(a): return a
    return math.exp(a)

@register('LN')
def _f(ctx, args):
    a = _n(args[0])
    if is_err(a) or a <= 0: return err('#NUM!') if not is_err(a) else a
    return math.log(a)

@register('LOG')
def _f(ctx, args):
    a = _n(args[0])
    if is_err(a): return a
    if a <= 0: return err('#NUM!')
    if len(args) > 1:
        b = _n(args[1])
        if is_err(b): return b
        if b <= 0 or b == 1: return err('#NUM!')
        return math.log(a, b)
    return math.log10(a)

@register('LOG10')
def _f(ctx, args):
    a = _n(args[0])
    if is_err(a) or a <= 0: return err('#NUM!') if not is_err(a) else a
    return math.log10(a)

for _nm, _fnref in [('SIN', math.sin), ('COS', math.cos), ('TAN', math.tan),
                    ('ASIN', math.asin), ('ACOS', math.acos), ('ATAN', math.atan),
                    ('SINH', math.sinh), ('COSH', math.cosh), ('TANH', math.tanh)]:
    def _mk(f):
        def fn(ctx, args):
            a = _n(args[0])
            if is_err(a): return a
            try: return f(a)
            except: return err('#NUM!')
        return fn
    FUNCS[_nm] = _mk(_fnref)

@register('ATAN2')
def _f(ctx, args):
    x = _n(args[0]); y = _n(args[1])
    if is_err(x): return x
    if is_err(y): return y
    return math.atan2(y, x)

@register('RADIANS')
def _f(ctx, args):
    a = _n(args[0])
    if is_err(a): return a
    return math.radians(a)

@register('DEGREES')
def _f(ctx, args):
    a = _n(args[0])
    if is_err(a): return a
    return math.degrees(a)

@register('FACT')
def _f(ctx, args):
    a = _i(args[0])
    if is_err(a): return a
    if a < 0: return err('#NUM!')
    return math.factorial(a)

@register('COMBIN')
def _f(ctx, args):
    n = _i(args[0]); k = _i(args[1])
    if is_err(n): return n
    if is_err(k): return k
    if n < 0 or k < 0 or k > n: return err('#NUM!')
    return math.comb(n, k)

@register('PERMUT')
def _f(ctx, args):
    n = _i(args[0]); k = _i(args[1])
    if is_err(n): return n
    if is_err(k): return k
    if n < 0 or k < 0 or k > n: return err('#NUM!')
    return math.perm(n, k)

@register('GCD')
def _f(ctx, args):
    nums = []
    for a in args:
        for x in flatten(a):
            if x is None or x == '': continue
            v = _i(x)
            if is_err(v): return v
            if v < 0: return err('#NUM!')
            nums.append(v)
    if not nums: return 0
    g = nums[0]
    for x in nums[1:]: g = math.gcd(g, x)
    return g

@register('LCM')
def _f(ctx, args):
    nums = []
    for a in args:
        for x in flatten(a):
            if x is None or x == '': continue
            v = _i(x)
            if is_err(v): return v
            if v < 0: return err('#NUM!')
            nums.append(v)
    if not nums: return 0
    r = 1
    for x in nums: r = r * x // math.gcd(r, x) if x else 0
    return r

@register('RAND')
def _f(ctx, args):
    import random
    return random.random()

@register('RANDBETWEEN')
def _f(ctx, args):
    import random
    a = _i(args[0]); b = _i(args[1])
    if is_err(a): return a
    if is_err(b): return b
    return random.randint(a, b)

# ---------- Wildcard helper for SUMIF/COUNTIF/etc ----------
def _criterion_match(crit, val):
    """Excel-style criterion matching (op + literal, wildcards)."""
    if is_err(val): return False
    op = '='
    rest = crit
    if isinstance(crit, str):
        for o in ('>=', '<=', '<>', '>', '<', '='):
            if crit.startswith(o):
                op = o; rest = crit[len(o):]; break
        # rest might be a number string
        try:
            rn = float(rest)
            if rn == int(rn): rn = int(rn)
            crit_num = rn
            crit_is_num = True
        except (ValueError, TypeError):
            crit_num = None
            crit_is_num = False
    else:
        crit_num = crit
        crit_is_num = isinstance(crit, (int, float)) and not isinstance(crit, bool)
        rest = crit
    if crit_is_num:
        try:
            v = float(val) if isinstance(val, (int, float, str)) and not isinstance(val, bool) else None
        except (ValueError, TypeError):
            v = None
        if v is None: return False
        if op == '=': return v == crit_num
        if op == '<>': return v != crit_num
        if op == '>': return v > crit_num
        if op == '<': return v < crit_num
        if op == '>=': return v >= crit_num
        if op == '<=': return v <= crit_num
    # text matching with wildcards
    sval = coerce_str(val)
    spat = coerce_str(rest) if rest is not None else ''
    if op in ('=', '<>'):
        # wildcard match
        pat = re.escape(spat).replace(r'\*', '.*').replace(r'\?', '.')
        m = re.fullmatch(pat, sval, re.IGNORECASE) is not None
        return m if op == '=' else not m
    # ordering for strings
    if op == '>': return sval > spat
    if op == '<': return sval < spat
    if op == '>=': return sval >= spat
    if op == '<=': return sval <= spat
    return False

def _flatten_with_shape(v):
    a = as_2d(v)
    flat = []
    for row in a:
        for c in row: flat.append(c)
    return flat

@register('SUMIF')
def _f(ctx, args):
    rng = _flatten_with_shape(args[0])
    crit = args[1]
    sum_rng = _flatten_with_shape(args[2]) if len(args) > 2 else rng
    total = 0
    for i, v in enumerate(rng):
        if _criterion_match(crit, v):
            sv = sum_rng[i] if i < len(sum_rng) else None
            if isinstance(sv, (int, float)) and not isinstance(sv, bool):
                total += sv
    return total

@register('COUNTIF')
def _f(ctx, args):
    rng = _flatten_with_shape(args[0])
    crit = args[1]
    return sum(1 for v in rng if _criterion_match(crit, v))

@register('AVERAGEIF')
def _f(ctx, args):
    rng = _flatten_with_shape(args[0])
    crit = args[1]
    avg_rng = _flatten_with_shape(args[2]) if len(args) > 2 else rng
    nums = []
    for i, v in enumerate(rng):
        if _criterion_match(crit, v):
            sv = avg_rng[i] if i < len(avg_rng) else None
            if isinstance(sv, (int, float)) and not isinstance(sv, bool):
                nums.append(sv)
    if not nums: return err('#DIV/0!')
    return sum(nums) / len(nums)

def _ifs_iter(args, value_first=False):
    if value_first:
        sum_rng = _flatten_with_shape(args[0])
        pairs = args[1:]
    else:
        sum_rng = None
        pairs = args
    crit_rngs = []
    for k in range(0, len(pairs), 2):
        crit_rngs.append((_flatten_with_shape(pairs[k]), pairs[k+1]))
    n = max((len(r) for r, _ in crit_rngs), default=0)
    for i in range(n):
        ok = True
        for r, c in crit_rngs:
            if i >= len(r) or not _criterion_match(c, r[i]):
                ok = False; break
        if ok:
            yield i, (sum_rng[i] if sum_rng and i < len(sum_rng) else None)

@register('SUMIFS')
def _f(ctx, args):
    total = 0
    for i, v in _ifs_iter(args, value_first=True):
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            total += v
    return total

@register('COUNTIFS')
def _f(ctx, args):
    n = 0
    for _ in _ifs_iter(args, value_first=False): n += 1
    return n

@register('AVERAGEIFS')
def _f(ctx, args):
    nums = []
    for i, v in _ifs_iter(args, value_first=True):
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            nums.append(v)
    if not nums: return err('#DIV/0!')
    return sum(nums) / len(nums)

@register('MAXIFS')
def _f(ctx, args):
    nums = []
    for i, v in _ifs_iter(args, value_first=True):
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            nums.append(v)
    return max(nums) if nums else 0

@register('MINIFS')
def _f(ctx, args):
    nums = []
    for i, v in _ifs_iter(args, value_first=True):
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            nums.append(v)
    return min(nums) if nums else 0

# ---------- statistics ----------
def _nums(args, skip_text=True, skip_bool=True):
    out = []
    for a in args:
        for x in flatten(a):
            if x is None or x == '': continue
            if is_err(x): return x
            if isinstance(x, bool):
                if not skip_bool: out.append(int(x))
                continue
            if isinstance(x, (int, float)):
                out.append(x); continue
            if isinstance(x, str):
                if skip_text: continue
                try: out.append(float(x))
                except: pass
    return out

@register('MEDIAN')
def _f(ctx, args):
    n = _nums(args)
    if is_err(n): return n
    if not n: return err('#NUM!')
    return _stats.median(n)

@register('MODE')
def _f(ctx, args):
    n = _nums(args)
    if is_err(n): return n
    if not n: return err('#N/A')
    try: return _stats.mode(n)
    except: return err('#N/A')

@register('STDEV', 'STDEV.S')
def _f(ctx, args):
    n = _nums(args)
    if is_err(n): return n
    if len(n) < 2: return err('#DIV/0!')
    return _stats.stdev(n)

@register('STDEVP', 'STDEV.P')
def _f(ctx, args):
    n = _nums(args)
    if is_err(n): return n
    if not n: return err('#DIV/0!')
    return _stats.pstdev(n)

@register('VAR', 'VAR.S')
def _f(ctx, args):
    n = _nums(args)
    if is_err(n): return n
    if len(n) < 2: return err('#DIV/0!')
    return _stats.variance(n)

@register('VARP', 'VAR.P')
def _f(ctx, args):
    n = _nums(args)
    if is_err(n): return n
    if not n: return err('#DIV/0!')
    return _stats.pvariance(n)

@register('PERCENTILE', 'PERCENTILE.INC')
def _f(ctx, args):
    n = sorted(_nums([args[0]]))
    if is_err(n): return n
    if not n: return err('#NUM!')
    p = _n(args[1])
    if is_err(p): return p
    if p < 0 or p > 1: return err('#NUM!')
    if len(n) == 1: return n[0]
    pos = p * (len(n) - 1)
    lo = int(math.floor(pos)); hi = int(math.ceil(pos))
    if lo == hi: return n[lo]
    return n[lo] + (n[hi] - n[lo]) * (pos - lo)

@register('QUARTILE', 'QUARTILE.INC')
def _f(ctx, args):
    q = _i(args[1])
    if is_err(q): return q
    if q not in (0, 1, 2, 3, 4): return err('#NUM!')
    return FUNCS['PERCENTILE'](ctx, [args[0], q / 4])

@register('RANK', 'RANK.EQ')
def _f(ctx, args):
    v = _n(args[0])
    if is_err(v): return v
    nums = _nums([args[1]])
    if is_err(nums): return nums
    order = _i(args[2]) if len(args) > 2 else 0
    if is_err(order): return order
    asc = order != 0
    if v not in nums: return err('#N/A')
    nums_sorted = sorted(nums) if asc else sorted(nums, reverse=True)
    return nums_sorted.index(v) + 1

@register('CORREL', 'PEARSON')
def _f(ctx, args):
    x = _nums([args[0]]); y = _nums([args[1]])
    if is_err(x): return x
    if is_err(y): return y
    if len(x) != len(y) or len(x) < 2: return err('#DIV/0!')
    mx = sum(x)/len(x); my = sum(y)/len(y)
    num = sum((xi-mx)*(yi-my) for xi, yi in zip(x, y))
    dx = math.sqrt(sum((xi-mx)**2 for xi in x))
    dy = math.sqrt(sum((yi-my)**2 for yi in y))
    if dx == 0 or dy == 0: return err('#DIV/0!')
    return num / (dx * dy)

@register('COVAR', 'COVARIANCE.P')
def _f(ctx, args):
    x = _nums([args[0]]); y = _nums([args[1]])
    if is_err(x): return x
    if is_err(y): return y
    if len(x) != len(y) or not x: return err('#DIV/0!')
    mx = sum(x)/len(x); my = sum(y)/len(y)
    return sum((xi-mx)*(yi-my) for xi, yi in zip(x, y)) / len(x)

def _linreg(x, y):
    n = len(x)
    mx = sum(x)/n; my = sum(y)/n
    num = sum((xi-mx)*(yi-my) for xi, yi in zip(x, y))
    den = sum((xi-mx)**2 for xi in x)
    return num, den, mx, my

@register('SLOPE')
def _f(ctx, args):
    y = _nums([args[0]]); x = _nums([args[1]])
    if is_err(x): return x
    if is_err(y): return y
    if len(x) != len(y) or len(x) < 2: return err('#DIV/0!')
    num, den, _, _ = _linreg(x, y)
    if den == 0: return err('#DIV/0!')
    return num / den

@register('INTERCEPT')
def _f(ctx, args):
    y = _nums([args[0]]); x = _nums([args[1]])
    if is_err(x): return x
    if is_err(y): return y
    if len(x) != len(y) or len(x) < 2: return err('#DIV/0!')
    num, den, mx, my = _linreg(x, y)
    if den == 0: return err('#DIV/0!')
    return my - (num/den) * mx

@register('FORECAST', 'FORECAST.LINEAR')
def _f(ctx, args):
    xv = _n(args[0])
    if is_err(xv): return xv
    y = _nums([args[1]]); x = _nums([args[2]])
    if is_err(x): return x
    if is_err(y): return y
    if len(x) != len(y) or len(x) < 2: return err('#DIV/0!')
    num, den, mx, my = _linreg(x, y)
    if den == 0: return err('#DIV/0!')
    slope = num / den
    return my + slope * (xv - mx)

# ---------- distributions ----------
def _norm_pdf(x, mu, sd):
    return math.exp(-0.5*((x-mu)/sd)**2) / (sd * math.sqrt(2*math.pi))
def _norm_cdf(x, mu, sd):
    return 0.5 * (1 + math.erf((x - mu) / (sd * math.sqrt(2))))

@register('NORM.DIST', 'NORMDIST')
def _f(ctx, args):
    x = _n(args[0]); mu = _n(args[1]); sd = _n(args[2])
    cum = _b(args[3]) if len(args) > 3 else True
    if is_err(x) or is_err(mu) or is_err(sd): return err('#NUM!')
    if sd <= 0: return err('#NUM!')
    return _norm_cdf(x, mu, sd) if cum else _norm_pdf(x, mu, sd)

@register('NORM.S.DIST', 'NORMSDIST')
def _f(ctx, args):
    x = _n(args[0])
    cum = _b(args[1]) if len(args) > 1 else True
    if is_err(x): return x
    return _norm_cdf(x, 0, 1) if cum else _norm_pdf(x, 0, 1)

def _norm_inv(p):
    # Beasley-Springer-Moro
    if p <= 0 or p >= 1: return err('#NUM!')
    a = [-3.969683028665376e+01,2.209460984245205e+02,-2.759285104469687e+02,1.383577518672690e+02,-3.066479806614716e+01,2.506628277459239e+00]
    b = [-5.447609879822406e+01,1.615858368580409e+02,-1.556989798598866e+02,6.680131188771972e+01,-1.328068155288572e+01]
    c = [-7.784894002430293e-03,-3.223964580411365e-01,-2.400758277161838e+00,-2.549732539343734e+00,4.374664141464968e+00,2.938163982698783e+00]
    d = [7.784695709041462e-03,3.224671290700398e-01,2.445134137142996e+00,3.754408661907416e+00]
    plow = 0.02425; phigh = 1 - plow
    if p < plow:
        q = math.sqrt(-2*math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p <= phigh:
        q = p - 0.5; r = q*q
        return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)
    q = math.sqrt(-2*math.log(1-p))
    return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)

@register('NORM.INV', 'NORMINV')
def _f(ctx, args):
    p = _n(args[0]); mu = _n(args[1]); sd = _n(args[2])
    if is_err(p) or is_err(mu) or is_err(sd): return err('#NUM!')
    if sd <= 0 or p <= 0 or p >= 1: return err('#NUM!')
    return mu + sd * _norm_inv(p)

@register('BINOM.DIST', 'BINOMDIST')
def _f(ctx, args):
    k = _i(args[0]); n = _i(args[1]); p = _n(args[2])
    cum = _b(args[3]) if len(args) > 3 else True
    if is_err(k) or is_err(n) or is_err(p): return err('#NUM!')
    if k < 0 or n < 0 or p < 0 or p > 1: return err('#NUM!')
    if cum:
        return sum(math.comb(n, i) * p**i * (1-p)**(n-i) for i in range(k+1))
    return math.comb(n, k) * p**k * (1-p)**(n-k)

@register('POISSON.DIST', 'POISSON')
def _f(ctx, args):
    k = _i(args[0]); lam = _n(args[1])
    cum = _b(args[2]) if len(args) > 2 else True
    if is_err(k) or is_err(lam): return err('#NUM!')
    if k < 0 or lam < 0: return err('#NUM!')
    if cum:
        return sum(math.exp(-lam) * lam**i / math.factorial(i) for i in range(k+1))
    return math.exp(-lam) * lam**k / math.factorial(k)

@register('EXPON.DIST', 'EXPONDIST')
def _f(ctx, args):
    x = _n(args[0]); lam = _n(args[1])
    cum = _b(args[2]) if len(args) > 2 else True
    if is_err(x) or is_err(lam): return err('#NUM!')
    if x < 0 or lam <= 0: return err('#NUM!')
    if cum: return 1 - math.exp(-lam*x)
    return lam * math.exp(-lam*x)

@register('GAMMA')
def _f(ctx, args):
    a = _n(args[0])
    if is_err(a): return a
    try: return math.gamma(a)
    except: return err('#NUM!')

@register('GAMMALN')
def _f(ctx, args):
    a = _n(args[0])
    if is_err(a): return a
    if a <= 0: return err('#NUM!')
    return math.lgamma(a)

# ---------- financial ----------
@register('PMT')
def _f(ctx, args):
    rate = _n(args[0]); nper = _n(args[1]); pv = _n(args[2])
    fv = _n(args[3]) if len(args) > 3 else 0
    typ = _n(args[4]) if len(args) > 4 else 0
    if is_err(rate) or is_err(nper) or is_err(pv): return err('#NUM!')
    if rate == 0: return -(pv + fv) / nper
    pmt = -(pv * rate * (1+rate)**nper + fv * rate) / ((1+rate)**nper - 1)
    if typ: pmt /= (1 + rate)
    return pmt

@register('FV')
def _f(ctx, args):
    rate = _n(args[0]); nper = _n(args[1]); pmt = _n(args[2])
    pv = _n(args[3]) if len(args) > 3 else 0
    typ = _n(args[4]) if len(args) > 4 else 0
    if rate == 0: return -(pv + pmt * nper)
    return -(pv * (1+rate)**nper + pmt * (1 + rate * typ) * ((1+rate)**nper - 1) / rate)

@register('PV')
def _f(ctx, args):
    rate = _n(args[0]); nper = _n(args[1]); pmt = _n(args[2])
    fv = _n(args[3]) if len(args) > 3 else 0
    typ = _n(args[4]) if len(args) > 4 else 0
    if rate == 0: return -(pmt * nper + fv)
    return -(pmt * (1 + rate * typ) * ((1+rate)**nper - 1) / rate + fv) / (1+rate)**nper

@register('NPV')
def _f(ctx, args):
    rate = _n(args[0])
    if is_err(rate): return rate
    vals = []
    for a in args[1:]:
        for x in flatten(a):
            if isinstance(x, (int, float)) and not isinstance(x, bool): vals.append(x)
    return sum(v / (1+rate)**(i+1) for i, v in enumerate(vals))

@register('IRR')
def _f(ctx, args):
    vals = []
    for x in flatten(args[0]):
        if isinstance(x, (int, float)) and not isinstance(x, bool): vals.append(x)
    guess = _n(args[1]) if len(args) > 1 else 0.1
    if is_err(guess): guess = 0.1
    r = guess
    for _ in range(100):
        f = sum(v / (1+r)**i for i, v in enumerate(vals))
        df = sum(-i * v / (1+r)**(i+1) for i, v in enumerate(vals))
        if df == 0: return err('#NUM!')
        nr = r - f / df
        if abs(nr - r) < 1e-9: return nr
        r = nr
    return err('#NUM!')

@register('RATE')
def _f(ctx, args):
    nper = _n(args[0]); pmt = _n(args[1]); pv = _n(args[2])
    fv = _n(args[3]) if len(args) > 3 else 0
    typ = _n(args[4]) if len(args) > 4 else 0
    guess = _n(args[5]) if len(args) > 5 else 0.1
    r = guess
    for _ in range(100):
        if r == 0:
            f = pv + pmt * nper + fv
            df = nper * pmt
        else:
            f = pv * (1+r)**nper + pmt * (1 + r * typ) * ((1+r)**nper - 1) / r + fv
            df = (pv * nper * (1+r)**(nper-1) + pmt * (1 + r*typ) * (nper*(1+r)**(nper-1)*r - ((1+r)**nper - 1))/(r*r) + pmt * typ * ((1+r)**nper - 1) / r)
        if df == 0: return err('#NUM!')
        nr = r - f / df
        if abs(nr - r) < 1e-10: return nr
        r = nr
    return err('#NUM!')

@register('NPER')
def _f(ctx, args):
    rate = _n(args[0]); pmt = _n(args[1]); pv = _n(args[2])
    fv = _n(args[3]) if len(args) > 3 else 0
    typ = _n(args[4]) if len(args) > 4 else 0
    if rate == 0:
        if pmt == 0: return err('#NUM!')
        return -(pv + fv) / pmt
    a = pmt * (1 + rate * typ)
    try:
        return math.log((a - fv * rate) / (a + pv * rate)) / math.log(1 + rate)
    except: return err('#NUM!')

@register('SLN')
def _f(ctx, args):
    cost = _n(args[0]); salv = _n(args[1]); life = _n(args[2])
    if life == 0: return err('#DIV/0!')
    return (cost - salv) / life

@register('CUMIPMT')
def _f(ctx, args):
    rate = _n(args[0]); nper = _n(args[1]); pv = _n(args[2])
    s = _i(args[3]); e = _i(args[4])
    typ = _i(args[5]) if len(args) > 5 else 0
    if rate <= 0 or nper <= 0 or pv <= 0 or s < 1 or e < s or e > nper: return err('#NUM!')
    pmt = FUNCS['PMT'](ctx, [rate, nper, pv, 0, typ])
    if is_err(pmt): return pmt
    total = 0
    bal = pv
    for p in range(1, int(nper)+1):
        if typ == 0:
            interest = bal * rate
            principal = pmt + interest
            interest = -interest
        else:
            if p == 1:
                interest = 0
                principal = pmt
            else:
                interest = (bal - pmt) * rate
                principal = pmt + interest
                interest = -interest
        bal = bal + principal
        if s <= p <= e: total += interest
    return total

# ---------- date / time ----------
@register('DATE')
def _f(ctx, args):
    y = _i(args[0]); m = _i(args[1]); d = _i(args[2])
    if is_err(y) or is_err(m) or is_err(d): return err('#NUM!')
    if y < 100: y += 1900
    try:
        # Excel allows month/day overflow
        first = _dt.date(y, 1, 1)
        # months: add m-1 months
        ty = y + (m - 1) // 12
        tm = (m - 1) % 12 + 1
        if tm <= 0:
            ty -= 1; tm += 12
        # day overflow handled by timedelta
        base = _dt.date(ty, tm, 1)
        target = base + _dt.timedelta(days=d - 1)
        return (target - EPOCH).days
    except: return err('#NUM!')

@register('YEAR')
def _f(ctx, args):
    n = _n(args[0])
    if is_err(n): return n
    return serial_to_date(n).year

@register('MONTH')
def _f(ctx, args):
    n = _n(args[0])
    if is_err(n): return n
    return serial_to_date(n).month

@register('DAY')
def _f(ctx, args):
    n = _n(args[0])
    if is_err(n): return n
    return serial_to_date(n).day

@register('WEEKDAY')
def _f(ctx, args):
    n = _n(args[0])
    if is_err(n): return n
    typ = _i(args[1]) if len(args) > 1 else 1
    d = serial_to_date(n)
    # Python: Mon=0..Sun=6
    py = d.weekday()
    if typ == 1: return ((py + 1) % 7) + 1  # Sun=1..Sat=7
    if typ == 2: return py + 1  # Mon=1..Sun=7
    if typ == 3: return py
    return ((py + 1) % 7) + 1

@register('DAYS')
def _f(ctx, args):
    e = _n(args[0]); s = _n(args[1])
    if is_err(e) or is_err(s): return err('#NUM!')
    return int(e) - int(s)

@register('TIME')
def _f(ctx, args):
    h = _i(args[0]); m = _i(args[1]); s = _i(args[2])
    if is_err(h) or is_err(m) or is_err(s): return err('#NUM!')
    return ((h*3600 + m*60 + s) % 86400) / 86400

@register('HOUR')
def _f(ctx, args):
    n = _n(args[0])
    if is_err(n): return n
    frac = n - int(n)
    return int(frac * 24) % 24

@register('MINUTE')
def _f(ctx, args):
    n = _n(args[0])
    if is_err(n): return n
    secs = round((n - int(n)) * 86400)
    return (secs // 60) % 60

@register('SECOND')
def _f(ctx, args):
    n = _n(args[0])
    if is_err(n): return n
    secs = round((n - int(n)) * 86400)
    return secs % 60

@register('NOW')
def _f(ctx, args):
    return date_to_serial(_dt.datetime.now())

@register('TODAY')
def _f(ctx, args):
    return date_to_serial(_dt.date.today())

@register('EOMONTH')
def _f(ctx, args):
    s = _n(args[0]); m = _i(args[1])
    if is_err(s) or is_err(m): return err('#NUM!')
    d = serial_to_date(s)
    ny = d.year + (d.month + m) // 12
    nm = (d.month + m) % 12
    if nm == 0: nm = 12; ny -= 1
    if nm == 12: last = _dt.date(ny + 1, 1, 1) - _dt.timedelta(days=1)
    else: last = _dt.date(ny, nm + 1, 1) - _dt.timedelta(days=1)
    return (last - EPOCH).days

@register('EDATE')
def _f(ctx, args):
    s = _n(args[0]); m = _i(args[1])
    if is_err(s) or is_err(m): return err('#NUM!')
    d = serial_to_date(s)
    ty = d.year + (d.month - 1 + m) // 12
    tm = (d.month - 1 + m) % 12 + 1
    import calendar
    last = calendar.monthrange(ty, tm)[1]
    td = min(d.day, last)
    return (_dt.date(ty, tm, td) - EPOCH).days

@register('NETWORKDAYS')
def _f(ctx, args):
    s = _n(args[0]); e = _n(args[1])
    if is_err(s) or is_err(e): return err('#NUM!')
    holidays = set()
    if len(args) > 2:
        for x in flatten(args[2]):
            if isinstance(x, (int, float)): holidays.add(int(x))
    a, b = int(s), int(e)
    if a > b: a, b = b, a; sign = -1
    else: sign = 1
    cnt = 0
    for d in range(a, b+1):
        wk = serial_to_date(d).weekday()
        if wk < 5 and d not in holidays: cnt += 1
    return cnt * sign

@register('YEARFRAC')
def _f(ctx, args):
    s = _n(args[0]); e = _n(args[1])
    if is_err(s) or is_err(e): return err('#NUM!')
    return abs(int(e) - int(s)) / 365.0

@register('WORKDAY')
def _f(ctx, args):
    s = _n(args[0]); n = _i(args[1])
    if is_err(s) or is_err(n): return err('#NUM!')
    holidays = set()
    if len(args) > 2:
        for x in flatten(args[2]):
            if isinstance(x, (int, float)): holidays.add(int(x))
    d = int(s)
    step = 1 if n >= 0 else -1
    remaining = abs(n)
    while remaining > 0:
        d += step
        if serial_to_date(d).weekday() < 5 and d not in holidays:
            remaining -= 1
    return d

# ---------- text ----------
@register('LEN')
def _f(ctx, args):
    s = _s(args[0])
    if is_err(s): return s
    return len(s)

@register('LEFT')
def _f(ctx, args):
    s = _s(args[0]); n = _i(args[1]) if len(args) > 1 else 1
    if is_err(s): return s
    if is_err(n): return n
    return s[:max(0, n)]

@register('RIGHT')
def _f(ctx, args):
    s = _s(args[0]); n = _i(args[1]) if len(args) > 1 else 1
    if is_err(s): return s
    if is_err(n): return n
    return s[-n:] if n > 0 else ''

@register('MID')
def _f(ctx, args):
    s = _s(args[0]); start = _i(args[1]); n = _i(args[2])
    if is_err(s) or is_err(start) or is_err(n): return err('#VALUE!')
    if start < 1 or n < 0: return err('#VALUE!')
    return s[start-1:start-1+n]

@register('UPPER')
def _f(ctx, args):
    s = _s(args[0])
    if is_err(s): return s
    return s.upper()

@register('LOWER')
def _f(ctx, args):
    s = _s(args[0])
    if is_err(s): return s
    return s.lower()

@register('PROPER')
def _f(ctx, args):
    s = _s(args[0])
    if is_err(s): return s
    out = []
    new = True
    for c in s:
        if c.isalpha():
            out.append(c.upper() if new else c.lower())
            new = False
        else:
            out.append(c); new = True
    return ''.join(out)

@register('TRIM')
def _f(ctx, args):
    s = _s(args[0])
    if is_err(s): return s
    return ' '.join(s.split())

@register('CONCAT', 'CONCATENATE')
def _f(ctx, args):
    out = []
    for a in args:
        for x in flatten(a):
            if x is None or x == '': continue
            if is_err(x): return x
            out.append(_s(x))
    return ''.join(out)

@register('TEXTJOIN')
def _f(ctx, args):
    sep = _s(args[0])
    skip = _b(args[1]) if len(args) > 1 else True
    parts = []
    for a in args[2:]:
        for x in flatten(a):
            if (x is None or x == '') and skip: continue
            if is_err(x): return x
            parts.append(_s(x))
    return sep.join(parts)

@register('SUBSTITUTE')
def _f(ctx, args):
    s = _s(args[0]); old = _s(args[1]); new = _s(args[2])
    inst = _i(args[3]) if len(args) > 3 else None
    if is_err(s) or is_err(old) or is_err(new): return err('#VALUE!')
    if inst is None: return s.replace(old, new)
    if old == '': return s
    out = s; pos = 0; cnt = 0
    res = []
    i = 0
    while i < len(s):
        if s[i:i+len(old)] == old:
            cnt += 1
            if cnt == inst:
                res.append(s[:i] + new + s[i+len(old):])
                return res[0]
            i += len(old)
        else:
            i += 1
    return s

@register('REPLACE')
def _f(ctx, args):
    s = _s(args[0]); start = _i(args[1]); n = _i(args[2]); new = _s(args[3])
    if is_err(s): return s
    return s[:start-1] + new + s[start-1+n:]

@register('REPT')
def _f(ctx, args):
    s = _s(args[0]); n = _i(args[1])
    if is_err(s) or is_err(n): return err('#VALUE!')
    if n < 0: return err('#VALUE!')
    return s * n

@register('FIND')
def _f(ctx, args):
    f = _s(args[0]); s = _s(args[1])
    start = _i(args[2]) if len(args) > 2 else 1
    if is_err(f) or is_err(s): return err('#VALUE!')
    i = s.find(f, start - 1)
    if i < 0: return err('#VALUE!')
    return i + 1

@register('SEARCH')
def _f(ctx, args):
    f = _s(args[0]); s = _s(args[1])
    start = _i(args[2]) if len(args) > 2 else 1
    if is_err(f) or is_err(s): return err('#VALUE!')
    pat = re.escape(f).replace(r'\*', '.*').replace(r'\?', '.')
    m = re.search(pat, s[start-1:], re.IGNORECASE)
    if m is None: return err('#VALUE!')
    return m.start() + start

@register('VALUE')
def _f(ctx, args):
    s = args[0]
    if isinstance(s, (int, float)) and not isinstance(s, bool): return s
    if is_err(s): return s
    try:
        f = float(_s(s).replace(',', '').replace('%', ''))
        if '%' in _s(s): f /= 100
        return f if not f.is_integer() else int(f)
    except: return err('#VALUE!')

@register('TEXT')
def _f(ctx, args):
    v = args[0]; fmt = _s(args[1])
    from formatx import apply_format
    return apply_format(v, fmt)

@register('CHAR')
def _f(ctx, args):
    n = _i(args[0])
    if is_err(n): return n
    if n < 1 or n > 255: return err('#VALUE!')
    return chr(n)

@register('CODE')
def _f(ctx, args):
    s = _s(args[0])
    if not s: return err('#VALUE!')
    return ord(s[0])

@register('UNICODE')
def _f(ctx, args):
    s = _s(args[0])
    if not s: return err('#VALUE!')
    return ord(s[0])

@register('UNICHAR')
def _f(ctx, args):
    n = _i(args[0])
    if is_err(n): return n
    if n < 1: return err('#VALUE!')
    return chr(n)

@register('TEXTSPLIT')
def _f(ctx, args):
    s = _s(args[0])
    col_sep = _s(args[1]) if len(args) > 1 else ''
    row_sep = _s(args[2]) if len(args) > 2 else ''
    rows = [s]
    if row_sep: rows = s.split(row_sep)
    if col_sep: result = [r.split(col_sep) for r in rows]
    else: result = [[r] for r in rows]
    return result

@register('TEXTBEFORE')
def _f(ctx, args):
    s = _s(args[0]); d = _s(args[1])
    inst = _i(args[2]) if len(args) > 2 else 1
    if not d: return s
    if inst >= 0:
        idx = -1
        for _k in range(inst):
            idx = s.find(d, idx+1)
            if idx < 0: return err('#N/A')
        return s[:idx]
    else:
        idx = len(s)
        for _k in range(-inst):
            idx = s.rfind(d, 0, idx)
            if idx < 0: return err('#N/A')
        return s[:idx]

@register('TEXTAFTER')
def _f(ctx, args):
    s = _s(args[0]); d = _s(args[1])
    inst = _i(args[2]) if len(args) > 2 else 1
    if not d: return s
    if inst >= 0:
        idx = -1
        for _k in range(inst):
            idx = s.find(d, idx+1)
            if idx < 0: return err('#N/A')
        return s[idx + len(d):]
    else:
        idx = len(s)
        for _k in range(-inst):
            idx = s.rfind(d, 0, idx)
            if idx < 0: return err('#N/A')
        return s[idx + len(d):]

@register('EXACT')
def _f(ctx, args):
    return _s(args[0]) == _s(args[1])

# ---------- logical ----------
@register('IF')
def _f(ctx, args):
    cond = args[0]
    if is_err(cond): return cond
    b = _b(cond)
    if is_err(b): return b
    if b:
        return args[1] if len(args) > 1 else True
    return args[2] if len(args) > 2 else False

@register('IFS')
def _f(ctx, args):
    for i in range(0, len(args), 2):
        c = args[i]
        if is_err(c): return c
        if _b(c): return args[i+1] if i+1 < len(args) else err('#N/A')
    return err('#N/A')

@register('SWITCH')
def _f(ctx, args):
    val = args[0]
    i = 1
    while i < len(args) - 1:
        if val == args[i]: return args[i+1]
        i += 2
    if i < len(args): return args[i]
    return err('#N/A')

@register('AND')
def _f(ctx, args):
    for a in args:
        for x in flatten(a):
            if x is None or x == '': continue
            if is_err(x): return x
            b = _b(x)
            if is_err(b): return b
            if not b: return False
    return True

@register('OR')
def _f(ctx, args):
    for a in args:
        for x in flatten(a):
            if x is None or x == '': continue
            if is_err(x): return x
            b = _b(x)
            if is_err(b): return b
            if b: return True
    return False

@register('XOR')
def _f(ctx, args):
    n = 0
    for a in args:
        for x in flatten(a):
            if x is None or x == '': continue
            if is_err(x): return x
            if _b(x): n += 1
    return n % 2 == 1

@register('NOT')
def _f(ctx, args):
    b = _b(args[0])
    if is_err(b): return b
    return not b

@register('TRUE')
def _f(ctx, args): return True

@register('FALSE')
def _f(ctx, args): return False

@register('IFERROR')
def _f(ctx, args):
    if is_err(args[0]): return args[1] if len(args) > 1 else ''
    return args[0]

@register('IFNA')
def _f(ctx, args):
    if args[0] == '#N/A': return args[1] if len(args) > 1 else ''
    return args[0]

# ---------- info ----------
@register('ISNUMBER')
def _f(ctx, args):
    return isinstance(args[0], (int, float)) and not isinstance(args[0], bool)

@register('ISTEXT')
def _f(ctx, args):
    return isinstance(args[0], str) and not is_err(args[0])

@register('ISBLANK')
def _f(ctx, args):
    return args[0] is None or args[0] == ''

@register('ISERROR')
def _f(ctx, args):
    return is_err(args[0])

@register('ISERR')
def _f(ctx, args):
    return is_err(args[0]) and args[0] != '#N/A'

@register('ISNA')
def _f(ctx, args):
    return args[0] == '#N/A'

@register('ISLOGICAL')
def _f(ctx, args):
    return isinstance(args[0], bool)

@register('ISEVEN')
def _f(ctx, args):
    n = _i(args[0])
    if is_err(n): return n
    return n % 2 == 0

@register('ISODD')
def _f(ctx, args):
    n = _i(args[0])
    if is_err(n): return n
    return n % 2 != 0

@register('N')
def _f(ctx, args):
    v = args[0]
    if isinstance(v, bool): return 1 if v else 0
    if isinstance(v, (int, float)): return v
    if is_err(v): return v
    return 0

@register('NA')
def _f(ctx, args):
    return err('#N/A')

@register('TYPE')
def _f(ctx, args):
    v = args[0]
    if isinstance(v, (int, float)) and not isinstance(v, bool): return 1
    if isinstance(v, str) and not is_err(v): return 2
    if isinstance(v, bool): return 4
    if is_err(v): return 16
    if isinstance(v, list): return 64
    return 1

# ---------- lookup ----------
def _excel_lookup_match(needle, hay, mtype=0):
    """mtype: 0 exact (wildcards), 1 less-or-equal (sorted asc), -1 greater-or-equal (sorted desc)."""
    if mtype == 0:
        if isinstance(needle, str) and not is_err(needle):
            pat = re.escape(needle).replace(r'\*', '.*').replace(r'\?', '.')
            for i, h in enumerate(hay):
                if isinstance(h, str) and re.fullmatch(pat, h, re.IGNORECASE):
                    return i
        for i, h in enumerate(hay):
            if h == needle: return i
            if isinstance(h, (int, float)) and isinstance(needle, (int, float)) and h == needle: return i
        return -1
    if mtype == 1:
        last = -1
        for i, h in enumerate(hay):
            try:
                if h <= needle: last = i
                else: break
            except TypeError:
                if isinstance(h, type(needle)) and h <= needle: last = i
                else: break
        return last
    if mtype == -1:
        last = -1
        for i, h in enumerate(hay):
            try:
                if h >= needle: last = i
                else: break
            except TypeError:
                pass
        return last
    return -1

@register('VLOOKUP')
def _f(ctx, args):
    needle = args[0]; tbl = as_2d(args[1]); col = _i(args[2])
    exact = True
    if len(args) > 3:
        ev = args[3]
        if isinstance(ev, bool): exact = not ev
        else: exact = (_n(ev) == 0) if not is_err(ev) else True
    if is_err(col): return col
    if not tbl: return err('#N/A')
    keys = [r[0] if r else None for r in tbl]
    idx = _excel_lookup_match(needle, keys, 0 if exact else 1)
    if idx < 0: return err('#N/A')
    if col < 1 or col > len(tbl[idx]): return err('#REF!')
    return tbl[idx][col-1]

@register('HLOOKUP')
def _f(ctx, args):
    needle = args[0]; tbl = as_2d(args[1]); row = _i(args[2])
    exact = True
    if len(args) > 3:
        ev = args[3]
        if isinstance(ev, bool): exact = not ev
        else: exact = (_n(ev) == 0)
    if is_err(row): return row
    if not tbl: return err('#N/A')
    keys = list(tbl[0])
    idx = _excel_lookup_match(needle, keys, 0 if exact else 1)
    if idx < 0: return err('#N/A')
    if row < 1 or row > len(tbl): return err('#REF!')
    return tbl[row-1][idx]

@register('MATCH')
def _f(ctx, args):
    needle = args[0]; rng = flatten(args[1])
    mtype = _i(args[2]) if len(args) > 2 else 1
    if is_err(mtype): return mtype
    idx = _excel_lookup_match(needle, rng, mtype)
    if idx < 0: return err('#N/A')
    return idx + 1

@register('INDEX')
def _f(ctx, args):
    arr = as_2d(args[0])
    if not arr: return err('#REF!')
    row = _i(args[1]) if len(args) > 1 else 0
    col = _i(args[2]) if len(args) > 2 else 0
    if is_err(row): return row
    if is_err(col): return col
    nrows = len(arr); ncols = max((len(r) for r in arr), default=0)
    if row == 0 and col == 0: return arr
    if row == 0:
        if col < 1 or col > ncols: return err('#REF!')
        return [[r[col-1]] for r in arr]
    if col == 0:
        if row < 1 or row > nrows: return err('#REF!')
        if ncols == 1:
            return arr[row-1][0]
        return [arr[row-1]]
    if row < 1 or row > nrows or col < 1 or col > ncols: return err('#REF!')
    return arr[row-1][col-1]

@register('XLOOKUP')
def _f(ctx, args):
    needle = args[0]
    lookup = flatten(args[1])
    ret_arr = as_2d(args[2])
    not_found = args[3] if len(args) > 3 else err('#N/A')
    match_mode = _i(args[4]) if len(args) > 4 else 0
    search_mode = _i(args[5]) if len(args) > 5 else 1
    # simple: 0 exact (wildcards if -1?). 1 exact or next larger. -1 exact or next smaller. 2 wildcard.
    if search_mode == -1:
        indices = list(range(len(lookup)-1, -1, -1))
    else:
        indices = list(range(len(lookup)))
    found = -1
    for i in indices:
        h = lookup[i]
        if match_mode == 2 and isinstance(needle, str):
            pat = re.escape(needle).replace(r'\*', '.*').replace(r'\?', '.')
            if isinstance(h, str) and re.fullmatch(pat, h, re.IGNORECASE):
                found = i; break
        elif h == needle:
            found = i; break
    if found < 0:
        if match_mode == 1:
            best = -1; bv = None
            for i, h in enumerate(lookup):
                try:
                    if h >= needle and (bv is None or h < bv):
                        bv = h; best = i
                except TypeError: pass
            found = best
        elif match_mode == -1:
            best = -1; bv = None
            for i, h in enumerate(lookup):
                try:
                    if h <= needle and (bv is None or h > bv):
                        bv = h; best = i
                except TypeError: pass
            found = best
    if found < 0:
        return not_found
    # return row from ret_arr if 2D else element
    nrows = len(ret_arr)
    ncols = len(ret_arr[0]) if ret_arr else 0
    # lookup is 1D (column or row)
    if nrows == len(lookup) and ncols == 1:
        return ret_arr[found][0]
    if ncols == len(lookup) and nrows == 1:
        return ret_arr[0][found]
    if nrows == len(lookup):
        return ret_arr[found]
    if ncols == len(lookup):
        return [r[found] for r in ret_arr]
    return ret_arr[found][0] if ret_arr else err('#N/A')

@register('CHOOSE')
def _f(ctx, args):
    n = _i(args[0])
    if is_err(n): return n
    if n < 1 or n >= len(args): return err('#VALUE!')
    return args[n]

@register('OFFSET')
def _f(ctx, args):
    # ctx.eval has __ref__ original cell? we'll handle in evaluator
    return err('#N/A')

@register('INDIRECT')
def _f(ctx, args):
    # handled in evaluator
    return err('#N/A')

@register('ADDRESS')
def _f(ctx, args):
    from refs import idx_to_col
    row = _i(args[0]); col = _i(args[1])
    abs_type = _i(args[2]) if len(args) > 2 else 1
    a1 = _b(args[3]) if len(args) > 3 else True
    if is_err(row) or is_err(col): return err('#VALUE!')
    cl = idx_to_col(col-1)
    if abs_type == 1: return f"${cl}${row}"
    if abs_type == 2: return f"{cl}${row}"
    if abs_type == 3: return f"${cl}{row}"
    return f"{cl}{row}"

@register('ROW')
def _f(ctx, args):
    if not args:
        return ctx.row + 1 if hasattr(ctx, 'row') else 1
    return ctx.row + 1 if hasattr(ctx, 'row') else 1

@register('COLUMN')
def _f(ctx, args):
    if not args:
        return ctx.col + 1 if hasattr(ctx, 'col') else 1
    return ctx.col + 1 if hasattr(ctx, 'col') else 1

@register('ROWS')
def _f(ctx, args):
    a = as_2d(args[0])
    return len(a)

@register('COLUMNS')
def _f(ctx, args):
    a = as_2d(args[0])
    return len(a[0]) if a else 0

# ---------- engineering ----------
@register('HEX2DEC')
def _f(ctx, args):
    s = _s(args[0])
    try: return int(s, 16) if not s.startswith('-') else -int(s[1:], 16)
    except: return err('#NUM!')
@register('DEC2HEX')
def _f(ctx, args):
    n = _i(args[0])
    if is_err(n): return n
    if n < 0: return format(n & 0xFFFFFFFFFF, 'X')
    return format(n, 'X')
@register('BIN2DEC')
def _f(ctx, args):
    s = _s(args[0])
    try: return int(s, 2)
    except: return err('#NUM!')
@register('DEC2BIN')
def _f(ctx, args):
    n = _i(args[0])
    if is_err(n): return n
    if n < 0: return format(n & 0x3FF, 'b')
    return format(n, 'b')
@register('OCT2DEC')
def _f(ctx, args):
    s = _s(args[0])
    try: return int(s, 8)
    except: return err('#NUM!')
@register('DEC2OCT')
def _f(ctx, args):
    n = _i(args[0])
    if is_err(n): return n
    return format(n, 'o') if n >= 0 else format(n & 0x3FFFFFFFF, 'o')
@register('BITAND')
def _f(ctx, args):
    a = _i(args[0]); b = _i(args[1])
    return a & b
@register('BITOR')
def _f(ctx, args):
    a = _i(args[0]); b = _i(args[1])
    return a | b
@register('BITXOR')
def _f(ctx, args):
    a = _i(args[0]); b = _i(args[1])
    return a ^ b
@register('BITLSHIFT')
def _f(ctx, args):
    a = _i(args[0]); b = _i(args[1])
    return a << b
@register('BITRSHIFT')
def _f(ctx, args):
    a = _i(args[0]); b = _i(args[1])
    return a >> b

# ---------- database ----------
def _dmatch(db_2d, criteria_2d):
    headers = db_2d[0]
    crit_headers = criteria_2d[0]
    crit_rows = criteria_2d[1:]
    matched = []
    for row in db_2d[1:]:
        for crow in crit_rows:
            ok = True
            for j, ch in enumerate(crit_headers):
                if j >= len(crow): continue
                cv = crow[j]
                if cv is None or cv == '': continue
                # find column in db
                if ch not in headers: ok = False; break
                col_i = headers.index(ch)
                v = row[col_i] if col_i < len(row) else None
                if not _criterion_match(cv, v):
                    ok = False; break
            if ok:
                matched.append(row); break
    return headers, matched

def _dvalues(args):
    db = as_2d(args[0]); fld = args[1]; crit = as_2d(args[2])
    headers, rows = _dmatch(db, crit)
    if isinstance(fld, str):
        if fld not in headers: return err('#VALUE!'), None
        col = headers.index(fld)
    else:
        col = _i(fld) - 1
        if col < 0 or col >= len(headers): return err('#VALUE!'), None
    return [r[col] if col < len(r) else None for r in rows], rows

@register('DSUM')
def _f(ctx, args):
    vs, _ = _dvalues(args)
    if is_err(vs): return vs
    return sum(v for v in vs if isinstance(v, (int, float)) and not isinstance(v, bool))
@register('DAVERAGE')
def _f(ctx, args):
    vs, _ = _dvalues(args)
    if is_err(vs): return vs
    nums = [v for v in vs if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if not nums: return err('#DIV/0!')
    return sum(nums)/len(nums)
@register('DCOUNT')
def _f(ctx, args):
    vs, _ = _dvalues(args)
    if is_err(vs): return vs
    return sum(1 for v in vs if isinstance(v, (int, float)) and not isinstance(v, bool))
@register('DCOUNTA')
def _f(ctx, args):
    vs, _ = _dvalues(args)
    if is_err(vs): return vs
    return sum(1 for v in vs if v is not None and v != '')
@register('DGET')
def _f(ctx, args):
    vs, _ = _dvalues(args)
    if is_err(vs): return vs
    if len(vs) == 0: return err('#VALUE!')
    if len(vs) > 1: return err('#NUM!')
    return vs[0]
@register('DMAX')
def _f(ctx, args):
    vs, _ = _dvalues(args)
    if is_err(vs): return vs
    nums = [v for v in vs if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return max(nums) if nums else 0
@register('DMIN')
def _f(ctx, args):
    vs, _ = _dvalues(args)
    if is_err(vs): return vs
    nums = [v for v in vs if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return min(nums) if nums else 0

# ---------- dynamic array ----------
@register('SEQUENCE')
def _f(ctx, args):
    rows = _i(args[0]) if len(args) > 0 else 1
    cols = _i(args[1]) if len(args) > 1 else 1
    start = _n(args[2]) if len(args) > 2 else 1
    step = _n(args[3]) if len(args) > 3 else 1
    if is_err(rows) or is_err(cols) or is_err(start) or is_err(step): return err('#VALUE!')
    if rows < 1 or cols < 1: return err('#NUM!')
    out = []
    v = start
    for r in range(rows):
        row = []
        for c in range(cols):
            row.append(v if isinstance(v, int) or (isinstance(v, float) and not v.is_integer()) else (int(v) if isinstance(v, float) else v))
            v += step
        out.append(row)
    return out

class _LambdaVal:
    def __init__(self, params, body, env):
        self.params = params; self.body = body; self.env = env

@register('MAP')
def _f(ctx, args):
    *arrs, lam = args
    arrs = [as_2d(a) for a in arrs]
    if not isinstance(lam, _LambdaVal): return err('#VALUE!')
    nrows = len(arrs[0]); ncols = len(arrs[0][0]) if arrs[0] else 0
    out = []
    for r in range(nrows):
        row = []
        for c in range(ncols):
            vals = [a[r][c] if r < len(a) and c < len(a[0]) else None for a in arrs]
            row.append(ctx.apply_lambda(lam, vals))
        out.append(row)
    return out

@register('BYROW')
def _f(ctx, args):
    arr = as_2d(args[0]); lam = args[1]
    if not isinstance(lam, _LambdaVal): return err('#VALUE!')
    out = []
    for row in arr:
        out.append([ctx.apply_lambda(lam, [[row]])])
    return out

@register('BYCOL')
def _f(ctx, args):
    arr = as_2d(args[0]); lam = args[1]
    if not isinstance(lam, _LambdaVal): return err('#VALUE!')
    if not arr: return []
    ncols = len(arr[0])
    out_row = []
    for c in range(ncols):
        col = [[r[c]] for r in arr]
        out_row.append(ctx.apply_lambda(lam, [col]))
    return [out_row]

@register('REDUCE')
def _f(ctx, args):
    init = args[0]; arr = as_2d(args[1]); lam = args[2]
    if not isinstance(lam, _LambdaVal): return err('#VALUE!')
    acc = init
    for row in arr:
        for v in row:
            acc = ctx.apply_lambda(lam, [acc, v])
    return acc

@register('SCAN')
def _f(ctx, args):
    init = args[0]; arr = as_2d(args[1]); lam = args[2]
    if not isinstance(lam, _LambdaVal): return err('#VALUE!')
    out = []
    acc = init
    for row in arr:
        outr = []
        for v in row:
            acc = ctx.apply_lambda(lam, [acc, v])
            outr.append(acc)
        out.append(outr)
    return out

@register('FILTER')
def _f(ctx, args):
    arr = as_2d(args[0]); mask = as_2d(args[1])
    if_empty = args[2] if len(args) > 2 else err('#CALC!')
    out = []
    if mask and len(mask) == len(arr) and len(mask[0]) == 1:
        for r, row in enumerate(arr):
            if _b(mask[r][0]): out.append(list(row))
    elif mask and len(mask) == 1 and len(mask[0]) == len(arr[0] if arr else []):
        sel = [c for c, v in enumerate(mask[0]) if _b(v)]
        for row in arr: out.append([row[c] for c in sel])
    else:
        return err('#VALUE!')
    if not out: return if_empty
    return out

@register('SORT')
def _f(ctx, args):
    arr = as_2d(args[0])
    by = _i(args[1]) if len(args) > 1 else 1
    order = _i(args[2]) if len(args) > 2 else 1
    by_col = _b(args[3]) if len(args) > 3 else False
    rev = order == -1
    def key(r):
        v = r[by-1] if by-1 < len(r) else None
        return (v is None, v)
    if by_col:
        # transpose, sort columns, transpose back
        cols = list(zip(*arr))
        cols_sorted = sorted(cols, key=lambda c: c[by-1] if by-1 < len(c) else None, reverse=rev)
        return [list(r) for r in zip(*cols_sorted)]
    return sorted([list(r) for r in arr], key=lambda r: (r[by-1] is None, r[by-1]) if by-1 < len(r) else (True, None), reverse=rev)

@register('UNIQUE')
def _f(ctx, args):
    arr = as_2d(args[0])
    seen = []
    out = []
    for row in arr:
        t = tuple(row)
        if t not in seen:
            seen.append(t)
            out.append(list(row))
    return out

@register('TRANSPOSE')
def _f(ctx, args):
    arr = as_2d(args[0])
    if not arr: return arr
    return [list(r) for r in zip(*arr)]

@register('SUMPRODUCT')
def _f(ctx, args):
    if not args: return 0
    arrs = [as_2d(a) for a in args]
    rows = max(len(a) for a in arrs)
    cols = max((len(a[0]) if a else 0) for a in arrs)
    total = 0
    for r in range(rows):
        for c in range(cols):
            p = 1
            for a in arrs:
                if r < len(a) and c < len(a[r]):
                    v = a[r][c]
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        p *= v
                    else:
                        p = 0; break
                else:
                    p = 0; break
            total += p
    return total

@register('LARGE')
def _f(ctx, args):
    nums = sorted(_nums([args[0]]), reverse=True)
    if is_err(nums): return nums
    k = _i(args[1])
    if is_err(k): return k
    if k < 1 or k > len(nums): return err('#NUM!')
    return nums[k-1]

@register('SMALL')
def _f(ctx, args):
    nums = sorted(_nums([args[0]]))
    if is_err(nums): return nums
    k = _i(args[1])
    if is_err(k): return k
    if k < 1 or k > len(nums): return err('#NUM!')
    return nums[k-1]

@register('FIXED')
def _f(ctx, args):
    n = _n(args[0])
    d = _i(args[1]) if len(args) > 1 else 2
    nc = _b(args[2]) if len(args) > 2 else False
    if is_err(n): return n
    if nc: return f'{n:.{d}f}'
    return f'{n:,.{d}f}'

@register('DOLLAR')
def _f(ctx, args):
    n = _n(args[0])
    d = _i(args[1]) if len(args) > 1 else 2
    if is_err(n): return n
    if n < 0: return f'-${abs(n):,.{d}f}'
    return f'${n:,.{d}f}'

@register('NUMBERVALUE')
def _f(ctx, args):
    s = _s(args[0])
    try: return float(s)
    except: return err('#VALUE!')

@register('CLEAN')
def _f(ctx, args):
    s = _s(args[0])
    return ''.join(c for c in s if ord(c) >= 32)

@register('T')
def _f(ctx, args):
    v = args[0]
    return v if isinstance(v, str) and not is_err(v) else ''

@register('SUBTOTAL')
def _f(ctx, args):
    code = _i(args[0])
    if is_err(code): return code
    rest = args[1:]
    fnmap = {1:'AVERAGE', 2:'COUNT', 3:'COUNTA', 4:'MAX', 5:'MIN', 6:'PRODUCT',
             7:'STDEV', 8:'STDEVP', 9:'SUM', 10:'VAR', 11:'VARP'}
    code = code % 100
    fname = fnmap.get(code)
    if not fname: return err('#VALUE!')
    return FUNCS[fname](ctx, rest)

@register('AGGREGATE')
def _f(ctx, args):
    code = _i(args[0])
    rest = args[2:]
    fnmap = {1:'AVERAGE', 2:'COUNT', 3:'COUNTA', 4:'MAX', 5:'MIN', 6:'PRODUCT',
             7:'STDEV', 8:'STDEVP', 9:'SUM', 10:'VAR', 11:'VARP', 14:'LARGE', 15:'SMALL'}
    fname = fnmap.get(code)
    if not fname: return err('#VALUE!')
    return FUNCS[fname](ctx, rest)

@register('SUMSQ')
def _f(ctx, args):
    nums = _nums(args)
    if is_err(nums): return nums
    return sum(x*x for x in nums)

@register('AVEDEV')
def _f(ctx, args):
    nums = _nums(args)
    if is_err(nums): return nums
    if not nums: return err('#NUM!')
    m = sum(nums)/len(nums)
    return sum(abs(x-m) for x in nums)/len(nums)

@register('GEOMEAN')
def _f(ctx, args):
    nums = _nums(args)
    if is_err(nums): return nums
    if not nums: return err('#NUM!')
    p = 1
    for x in nums:
        if x <= 0: return err('#NUM!')
        p *= x
    return p ** (1.0/len(nums))

@register('HARMEAN')
def _f(ctx, args):
    nums = _nums(args)
    if is_err(nums): return nums
    if not nums: return err('#NUM!')
    s = 0
    for x in nums:
        if x == 0: return err('#NUM!')
        s += 1.0/x
    return len(nums)/s
