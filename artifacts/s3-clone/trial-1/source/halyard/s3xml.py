"""XML helpers for S3 responses."""
from xml.sax.saxutils import escape as _esc
from datetime import datetime


def esc(s):
    if s is None:
        return ""
    return _esc(str(s))


def iso_for_s3(ts):
    # input ISO string with 'Z' suffix from DB; output for S3 listing format
    if ts is None:
        return ""
    if "T" in ts and ts.endswith("Z"):
        # already good; ensure ms or so
        try:
            t = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
            return t.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        except ValueError:
            return ts
    return ts


def http_iso_to_rfc1123(ts):
    if not ts:
        return ""
    if ts.endswith("Z") and "T" in ts:
        try:
            t = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
            return t.strftime("%a, %d %b %Y %H:%M:%S GMT")
        except ValueError:
            try:
                t = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ")
                return t.strftime("%a, %d %b %Y %H:%M:%S GMT")
            except ValueError:
                return ts
    return ts
