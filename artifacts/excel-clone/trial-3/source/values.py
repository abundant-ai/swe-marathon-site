"""Cell value normalization, error sentinels."""
import datetime as _dt

ERRORS = {'#DIV/0!', '#NAME?', '#N/A', '#NUM!', '#VALUE!', '#REF!', '#NULL!', '#SPILL!', '#CIRC!', '#CALC!'}

class Err(str):
    """Marker for error values - subclass of str."""
    pass

def err(s):
    return Err(s)

def is_err(v):
    return isinstance(v, Err) or (isinstance(v, str) and v in ERRORS)

def is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)

def coerce_num(v):
    if v is None or v == '':
        return 0
    if isinstance(v, bool):
        return 1 if v else 0
    if isinstance(v, (int, float)):
        return v
    if is_err(v):
        return v
    if isinstance(v, str):
        try:
            f = float(v)
            if f.is_integer():
                return int(f)
            return f
        except ValueError:
            return err('#VALUE!')
    return err('#VALUE!')

def coerce_str(v):
    if v is None:
        return ''
    if isinstance(v, bool):
        return 'TRUE' if v else 'FALSE'
    if is_err(v):
        return v
    if isinstance(v, float):
        if v.is_integer():
            return str(int(v))
        return repr(v)
    return str(v)

def coerce_bool(v):
    if isinstance(v, bool):
        return v
    if is_err(v):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        u = v.strip().upper()
        if u == 'TRUE':
            return True
        if u == 'FALSE':
            return False
        return err('#VALUE!')
    return False

def value_kind(v):
    if is_err(v):
        return 'error'
    if isinstance(v, bool):
        return 'bool'
    if isinstance(v, (int, float)):
        return 'number'
    if isinstance(v, str):
        return 'string'
    if v is None:
        return 'empty'
    return 'string'

EPOCH = _dt.date(1899, 12, 30)  # Excel 1900 system

def date_to_serial(d):
    if isinstance(d, _dt.datetime):
        delta = (d.date() - EPOCH).days
        secs = (d.hour * 3600 + d.minute * 60 + d.second) / 86400.0
        return delta + secs
    if isinstance(d, _dt.date):
        return (d - EPOCH).days
    return d

def serial_to_date(n):
    n = float(n)
    days = int(n)
    return EPOCH + _dt.timedelta(days=days)

def serial_to_datetime(n):
    n = float(n)
    days = int(n)
    frac = n - days
    secs = int(round(frac * 86400))
    return _dt.datetime.combine(EPOCH + _dt.timedelta(days=days), _dt.time()) + _dt.timedelta(seconds=secs)
