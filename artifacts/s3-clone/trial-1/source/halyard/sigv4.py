"""AWS Signature Version 4 verification compatible with boto3."""
import datetime
import hashlib
import hmac
import re
from urllib.parse import quote, unquote


REGION = "us-east-1"
SERVICE = "s3"
ALGORITHM = "AWS4-HMAC-SHA256"
EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
UNSIGNED_PAYLOAD = "UNSIGNED-PAYLOAD"
STREAMING_PAYLOAD = "STREAMING-AWS4-HMAC-SHA256-PAYLOAD"


def _sign(key, msg):
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def signing_key(secret, datestamp, region, service):
    k_date = _sign(("AWS4" + secret).encode("utf-8"), datestamp)
    k_region = hmac.new(k_date, region.encode("utf-8"), hashlib.sha256).digest()
    k_service = hmac.new(k_region, service.encode("utf-8"), hashlib.sha256).digest()
    k_signing = hmac.new(k_service, b"aws4_request", hashlib.sha256).digest()
    return k_signing


_RESERVED = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.~"


def _aws_uri_encode(s, encode_slash=True):
    out = []
    for ch in s:
        if ch in _RESERVED:
            out.append(ch)
        elif ch == "/" and not encode_slash:
            out.append(ch)
        else:
            for b in ch.encode("utf-8"):
                out.append(f"%{b:02X}")
    return "".join(out)


def _canonical_uri(path):
    # path comes URL-decoded? boto3 sends raw path. We canonicalize per AWS rules.
    # For S3, we should NOT double-encode; we use the raw path and re-encode each segment once.
    # AWS S3 sigv4: do NOT normalize the path (i.e., do not collapse //).
    # We apply: split on /, encode each segment with encode_slash=True (since '/' is separator).
    # But path already contains '/', we want to keep them.
    if not path:
        return "/"
    # The incoming raw path - re-encode it. boto3 percent-encodes the key already in the URI.
    # We need: take the raw URI as sent, and re-encode it according to RFC 3986
    # The simplest: decode and re-encode
    decoded = unquote(path)
    encoded = _aws_uri_encode(decoded, encode_slash=False)
    return encoded


def _canonical_query(query_string):
    if not query_string:
        return ""
    pairs = []
    for part in query_string.split("&"):
        if not part:
            continue
        if "=" in part:
            k, v = part.split("=", 1)
        else:
            k, v = part, ""
        # decode then re-encode each
        k_dec = unquote(k)
        v_dec = unquote(v)
        k_enc = _aws_uri_encode(k_dec, encode_slash=True)
        v_enc = _aws_uri_encode(v_dec, encode_slash=True)
        pairs.append((k_enc, v_enc))
    pairs.sort(key=lambda x: (x[0], x[1]))
    return "&".join(f"{k}={v}" for k, v in pairs)


def _canonical_headers(headers, signed_headers_list):
    # headers: dict of lower-case header name -> value
    # signed_headers_list: lower-case names sorted
    lines = []
    for name in signed_headers_list:
        val = headers.get(name, "")
        # collapse whitespace per AWS spec (only sequential whitespace inside value)
        # In practice strip + collapse multiple spaces
        if isinstance(val, bytes):
            val = val.decode("latin-1")
        val = val.strip()
        val = re.sub(r"\s+", " ", val)
        lines.append(f"{name}:{val}\n")
    return "".join(lines)


class SigV4Error(Exception):
    def __init__(self, code, message=""):
        self.code = code
        self.message = message
        super().__init__(message)


class AuthInfo:
    def __init__(self, access_key_id, signed_headers, signature, datestamp, credential_scope, payload_hash, presigned=False, region=REGION, service=SERVICE):
        self.access_key_id = access_key_id
        self.signed_headers = signed_headers  # list
        self.signature = signature
        self.datestamp = datestamp
        self.credential_scope = credential_scope
        self.payload_hash = payload_hash
        self.presigned = presigned
        self.region = region
        self.service = service


def parse_authorization(headers, query_params):
    """Returns (AuthInfo, presigned_bool) or raises SigV4Error.

    Returns None if no auth was provided at all (caller can decide whether anonymous is allowed).
    """
    auth_header = headers.get("authorization") or headers.get("Authorization")

    # check presigned
    qp = query_params
    if qp.get("X-Amz-Algorithm") == ALGORITHM:
        # presigned
        try:
            credential = qp["X-Amz-Credential"]
            signed_headers_str = qp["X-Amz-SignedHeaders"]
            signature = qp["X-Amz-Signature"]
            date = qp["X-Amz-Date"]
        except KeyError as e:
            raise SigV4Error("AuthorizationQueryParametersError", f"Missing {e}")
        parts = credential.split("/")
        if len(parts) < 5:
            raise SigV4Error("AuthorizationQueryParametersError", "bad credential")
        access_key_id = parts[0]
        datestamp = parts[1]
        region = parts[2]
        service = parts[3]
        credential_scope = "/".join(parts[1:5])
        signed_headers = sorted(s.lower() for s in signed_headers_str.split(";"))
        return AuthInfo(access_key_id, signed_headers, signature, datestamp, credential_scope, UNSIGNED_PAYLOAD, presigned=True, region=region, service=service), True

    if not auth_header:
        return None, False

    if not auth_header.startswith(ALGORITHM + " "):
        raise SigV4Error("AuthorizationHeaderMalformed", "wrong algorithm")
    rest = auth_header[len(ALGORITHM) + 1:]
    parts = {}
    for chunk in rest.split(","):
        chunk = chunk.strip()
        if "=" in chunk:
            k, v = chunk.split("=", 1)
            parts[k.strip()] = v.strip()
    try:
        credential = parts["Credential"]
        signed_headers_str = parts["SignedHeaders"]
        signature = parts["Signature"]
    except KeyError as e:
        raise SigV4Error("AuthorizationHeaderMalformed", f"missing {e}")

    cred_parts = credential.split("/")
    if len(cred_parts) < 5:
        raise SigV4Error("AuthorizationHeaderMalformed", "bad credential")
    access_key_id = cred_parts[0]
    datestamp = cred_parts[1]
    region = cred_parts[2]
    service = cred_parts[3]
    credential_scope = "/".join(cred_parts[1:5])
    signed_headers = sorted(s.lower() for s in signed_headers_str.split(";"))

    payload_hash = headers.get("x-amz-content-sha256") or UNSIGNED_PAYLOAD
    return AuthInfo(access_key_id, signed_headers, signature, datestamp, credential_scope, payload_hash, presigned=False, region=region, service=service), False


def verify(req_method, req_path, req_query_string, req_headers, body_bytes, secret_key, auth_info: AuthInfo):
    """Returns True/False. req_query_string is the raw '?...' part WITHOUT the leading '?'.
    For presigned requests, X-Amz-Signature must be removed from the canonical query."""
    headers_lc = {}
    for k, v in req_headers.items():
        headers_lc[k.lower()] = v

    # Build canonical query
    if auth_info.presigned:
        # remove X-Amz-Signature from canonical query
        parts = []
        for p in req_query_string.split("&"):
            if not p:
                continue
            k = p.split("=", 1)[0]
            if k == "X-Amz-Signature":
                continue
            parts.append(p)
        cq = _canonical_query("&".join(parts))
    else:
        cq = _canonical_query(req_query_string)

    # canonical headers
    canonical_headers = _canonical_headers(headers_lc, auth_info.signed_headers)
    signed_headers_str = ";".join(auth_info.signed_headers)

    # payload hash
    if auth_info.presigned:
        payload_hash = UNSIGNED_PAYLOAD
    else:
        payload_hash = headers_lc.get("x-amz-content-sha256") or UNSIGNED_PAYLOAD

    canonical_uri = _canonical_uri(req_path)

    canonical_request = "\n".join([
        req_method,
        canonical_uri,
        cq,
        canonical_headers,
        signed_headers_str,
        payload_hash,
    ])

    # date for string-to-sign: use X-Amz-Date or date header
    if auth_info.presigned:
        amz_date = req_query_string  # not used; we'll get from query
        # actually we need the X-Amz-Date from query
        amz_date = None
        for p in req_query_string.split("&"):
            if p.startswith("X-Amz-Date="):
                amz_date = unquote(p.split("=", 1)[1])
                break
    else:
        amz_date = headers_lc.get("x-amz-date") or headers_lc.get("date")

    if not amz_date:
        return False

    cred_scope = auth_info.credential_scope
    string_to_sign = "\n".join([
        ALGORITHM,
        amz_date,
        cred_scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])

    sk = signing_key(secret_key, auth_info.datestamp, auth_info.region, auth_info.service)
    expected = hmac.new(sk, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, auth_info.signature)


def check_clock_skew(headers, query_params, max_seconds=900):
    """Verify the X-Amz-Date is within the skew window, and for presigned, not expired.
    Returns (ok, error_code)."""
    qp = query_params
    is_presigned = qp.get("X-Amz-Algorithm") == ALGORITHM
    if is_presigned:
        amz_date = qp.get("X-Amz-Date")
        expires = qp.get("X-Amz-Expires")
    else:
        amz_date = headers.get("x-amz-date") or headers.get("X-Amz-Date") or headers.get("date") or headers.get("Date")
        expires = None

    if not amz_date:
        return False, "AccessDenied"

    try:
        if "T" in amz_date and amz_date.endswith("Z"):
            t = datetime.datetime.strptime(amz_date, "%Y%m%dT%H%M%SZ")
        else:
            # Date header format
            t = datetime.datetime.strptime(amz_date, "%a, %d %b %Y %H:%M:%S GMT")
    except ValueError:
        return False, "AccessDenied"

    now = datetime.datetime.utcnow()
    skew = abs((now - t).total_seconds())

    if is_presigned and expires:
        try:
            exp_seconds = int(expires)
        except ValueError:
            return False, "AccessDenied"
        if (now - t).total_seconds() > exp_seconds:
            return False, "AccessDenied"
        # also allow some early skew (15 min)
        if (t - now).total_seconds() > 900:
            return False, "RequestTimeTooSkewed"
        return True, None

    if skew > max_seconds:
        return False, "RequestTimeTooSkewed"
    return True, None
