"""AWS Signature Version 2 verification for presigned URLs.

Used when boto3 falls back to SigV2 for presigned URLs against custom endpoints.
"""
import base64
import hashlib
import hmac
import time
from urllib.parse import quote, unquote


SUBRESOURCES = {
    "acl", "lifecycle", "location", "logging", "notification", "partNumber",
    "policy", "requestPayment", "torrent", "uploadId", "uploads", "versionId",
    "versioning", "versions", "website", "delete", "tagging", "cors",
    "restore", "encryption", "object-lock", "retention", "legal-hold",
    "publicAccessBlock", "ownershipControls", "intelligent-tiering",
    "inventory", "metrics", "analytics", "replication",
    "response-content-type", "response-content-language", "response-expires",
    "response-cache-control", "response-content-disposition", "response-content-encoding",
}


def parse_sigv2_query(qp):
    """Detect a SigV2 presigned request. Returns (access_key_id, signature, expires) or None."""
    ak = qp.get("AWSAccessKeyId")
    sig = qp.get("Signature")
    expires = qp.get("Expires")
    if ak and sig and expires:
        return ak, sig, expires
    return None


def is_expired_sigv2(expires):
    try:
        e = int(expires)
    except (ValueError, TypeError):
        return True
    return time.time() > e + 900  # allow 15 min skew


def verify_sigv2(method, raw_path, query_string, headers, secret_key, expected_signature):
    """Verify SigV2 (S3 REST). Returns True/False.

    Canonical string:
      METHOD\n
      Content-MD5\n
      Content-Type\n
      Date or Expires\n
      CanonicalizedAmzHeaders
      CanonicalizedResource
    """
    headers_lc = {k.lower(): v for k, v in headers.items()}
    qp = {}
    for p in query_string.split("&"):
        if not p:
            continue
        if "=" in p:
            k, v = p.split("=", 1)
            qp[unquote(k)] = unquote(v)
        else:
            qp[unquote(p)] = ""

    content_md5 = headers_lc.get("content-md5", "")
    content_type = headers_lc.get("content-type", "")

    # For presigned: use Expires; for header-signed: use Date
    expires = qp.get("Expires", "")
    date_str = expires if expires else headers_lc.get("date", "")

    # Canonicalized AMZ headers
    amz = sorted([(k, v) for k, v in headers_lc.items() if k.startswith("x-amz-")])
    canonical_amz = ""
    for k, v in amz:
        canonical_amz += f"{k}:{v}\n"

    # Canonicalized Resource
    # raw_path is the URL-decoded path (boto3 sends it path-encoded; we need to decode then keep path).
    # For S3 path-style: /<bucket>/<key>
    # We use the path as-is from the request.
    decoded_path = unquote(raw_path)
    canonical_resource = decoded_path
    # Sub-resources from query
    sub_pairs = []
    for k in sorted(qp.keys()):
        if k in SUBRESOURCES or k.startswith("x-amz-") or k.startswith("response-"):
            v = qp[k]
            if v:
                sub_pairs.append(f"{k}={v}")
            else:
                sub_pairs.append(k)
    if sub_pairs:
        canonical_resource += "?" + "&".join(sub_pairs)

    string_to_sign = (
        f"{method}\n"
        f"{content_md5}\n"
        f"{content_type}\n"
        f"{date_str}\n"
        f"{canonical_amz}"
        f"{canonical_resource}"
    )
    digest = hmac.new(secret_key.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha1).digest()
    computed = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(computed, expected_signature)
