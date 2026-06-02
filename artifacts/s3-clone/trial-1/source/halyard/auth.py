"""Authentication: SigV4 + SigV2 (presigned) verification for S3 requests."""
from flask import request

from . import sigv4
from . import sigv2 as sigv2mod
from .errors import S3Error
from .models import lookup_access_key


def get_raw_query_string():
    """Get the raw query string as sent."""
    qs = request.environ.get("QUERY_STRING", "")
    return qs


def authenticate_s3():
    """Authenticate an S3 request. Returns (access_key_id, tenant) or raises S3Error.
    Returns (None, None) if anonymous (no auth at all)."""
    headers = {k: v for k, v in request.headers.items()}
    qp = request.args
    qs = get_raw_query_string()

    # Try SigV2 presigned first (AWSAccessKeyId in query)
    v2 = sigv2mod.parse_sigv2_query(qp)
    if v2 is not None:
        ak, sig, expires = v2
        if sigv2mod.is_expired_sigv2(expires):
            raise S3Error("AccessDenied", "Request has expired")
        res = lookup_access_key(ak)
        if res is None:
            raise S3Error("InvalidAccessKeyId")
        tenant, secret = res
        method = request.method
        path = request.environ.get("RAW_URI", request.path)
        if "?" in path:
            path = path.split("?", 1)[0]
        ok = sigv2mod.verify_sigv2(method, path, qs, headers, secret, sig)
        if not ok:
            raise S3Error("SignatureDoesNotMatch")
        return ak, tenant

    # SigV4
    try:
        info, presigned = sigv4.parse_authorization(headers, qp)
    except sigv4.SigV4Error as e:
        raise S3Error(e.code if e.code in ("AuthorizationHeaderMalformed", "AuthorizationQueryParametersError") else "AccessDenied", e.message)

    if info is None:
        return None, None

    ok, err = sigv4.check_clock_skew(headers, qp)
    if not ok:
        if err == "RequestTimeTooSkewed":
            raise S3Error("RequestTimeTooSkewed")
        raise S3Error("AccessDenied", "Request has expired")

    res = lookup_access_key(info.access_key_id)
    if res is None:
        raise S3Error("InvalidAccessKeyId")
    tenant, secret = res

    body = request.get_data(cache=True) or b""
    method = request.method
    path = request.environ.get("RAW_URI", request.path)
    if "?" in path:
        path = path.split("?", 1)[0]

    ok = sigv4.verify(method, path, qs, headers, body, secret, info)
    if not ok:
        raise S3Error("SignatureDoesNotMatch")

    return info.access_key_id, tenant
