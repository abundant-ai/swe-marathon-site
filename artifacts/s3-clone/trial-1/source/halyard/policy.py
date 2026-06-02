"""Bucket policy evaluation."""
import json
import re

ALLOWED_ACTIONS = {
    "s3:GetObject",
    "s3:PutObject",
    "s3:ListBucket",
    "s3:DeleteObject",
    "s3:GetBucketLocation",
    "s3:*",
    "*",
}


def validate_policy(text):
    """Parse and validate; raises ValueError on bad policy. Returns parsed dict."""
    try:
        p = json.loads(text)
    except (ValueError, TypeError):
        raise ValueError("malformed JSON")
    if not isinstance(p, dict):
        raise ValueError("not an object")
    if "Statement" not in p:
        raise ValueError("missing Statement")
    stmts = p["Statement"]
    if isinstance(stmts, dict):
        stmts = [stmts]
    if not isinstance(stmts, list):
        raise ValueError("Statement must be array or object")
    for s in stmts:
        eff = s.get("Effect")
        if eff not in ("Allow", "Deny"):
            raise ValueError("invalid Effect")
        action = s.get("Action")
        if action is None:
            raise ValueError("missing Action")
        if isinstance(action, str):
            action = [action]
        for a in action:
            if a not in ALLOWED_ACTIONS:
                raise ValueError(f"unknown action {a}")
        resource = s.get("Resource")
        if resource is None:
            raise ValueError("missing Resource")
    return p


def _principal_matches(principal, tenant):
    """Tenant is None for anonymous. Returns True if principal matches."""
    if principal == "*":
        return True
    if isinstance(principal, dict):
        aws = principal.get("AWS")
        if aws is None:
            return False
        if isinstance(aws, str):
            aws = [aws]
        for entry in aws:
            if entry == "*":
                return True
            if tenant is None:
                continue
            # arn:aws:iam::<tenant>:root
            m = re.match(r"^arn:aws:iam::([^:]+):root$", entry)
            if m and m.group(1) == tenant:
                return True
        return False
    return False


def _resource_matches(resource, bucket, key):
    """resource is a string or list."""
    if isinstance(resource, str):
        resource = [resource]
    for r in resource:
        # arn:aws:s3:::<bucket>  or arn:aws:s3:::<bucket>/<prefix>*
        if not r.startswith("arn:aws:s3:::"):
            continue
        rest = r[len("arn:aws:s3:::"):]
        if "/" in rest:
            b, suffix = rest.split("/", 1)
        else:
            b = rest
            suffix = None
        if b != bucket:
            continue
        if suffix is None:
            # bucket-level
            if key is None:
                return True
        else:
            # object-level (key path)
            if key is None:
                continue
            if suffix.endswith("*"):
                prefix = suffix[:-1]
                if key.startswith(prefix):
                    return True
            else:
                if key == suffix:
                    return True
    return False


def _action_matches(actions, requested):
    if isinstance(actions, str):
        actions = [actions]
    for a in actions:
        if a == "*" or a == "s3:*":
            return True
        if a == requested:
            return True
    return False


def evaluate(policy_text, action, tenant, bucket, key):
    """Returns 'Allow', 'Deny', or 'NoMatch'."""
    if not policy_text:
        return "NoMatch"
    try:
        p = json.loads(policy_text)
    except Exception:
        return "NoMatch"
    stmts = p.get("Statement", [])
    if isinstance(stmts, dict):
        stmts = [stmts]

    matched_allow = False
    for s in stmts:
        if not _principal_matches(s.get("Principal", "*"), tenant):
            continue
        if not _action_matches(s.get("Action", []), action):
            continue
        if not _resource_matches(s.get("Resource", []), bucket, key):
            continue
        if s.get("Effect") == "Deny":
            return "Deny"
        if s.get("Effect") == "Allow":
            matched_allow = True
    return "Allow" if matched_allow else "NoMatch"
