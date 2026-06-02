import json
import fnmatch
from .errors import S3Error

VALID_ACTIONS = {'s3:GetObject', 's3:PutObject', 's3:ListBucket', 's3:*'}

def parse_policy(text):
    try:
        p = json.loads(text)
    except Exception:
        raise S3Error('MalformedPolicy', 'Policy is not valid JSON')
    if not isinstance(p, dict):
        raise S3Error('MalformedPolicy', 'Policy must be a JSON object')
    if 'Version' not in p or 'Statement' not in p:
        raise S3Error('MalformedPolicy', 'Policy missing Version or Statement')
    stmts = p['Statement']
    if isinstance(stmts, dict):
        stmts = [stmts]
    if not isinstance(stmts, list):
        raise S3Error('MalformedPolicy', 'Statement must be array or object')
    for s in stmts:
        if not isinstance(s, dict):
            raise S3Error('MalformedPolicy', 'Statement entry must be object')
        if s.get('Effect') not in ('Allow', 'Deny'):
            raise S3Error('MalformedPolicy', 'Effect must be Allow or Deny')
        actions = s.get('Action')
        if isinstance(actions, str):
            actions = [actions]
        if not isinstance(actions, list):
            raise S3Error('MalformedPolicy', 'Action missing or invalid')
        for a in actions:
            if a not in VALID_ACTIONS:
                raise S3Error('MalformedPolicy', f'Unsupported action: {a}')
        res = s.get('Resource')
        if isinstance(res, str):
            res = [res]
        if not isinstance(res, list):
            raise S3Error('MalformedPolicy', 'Resource missing or invalid')
        for r in res:
            if not r.startswith('arn:aws:s3:::'):
                raise S3Error('MalformedPolicy', f'Invalid resource ARN: {r}')
    return p

def _principal_matches(stmt_principal, requester_tenant):
    """requester_tenant=None means anonymous."""
    if stmt_principal == '*':
        return True
    if isinstance(stmt_principal, dict):
        aws = stmt_principal.get('AWS')
        if aws == '*':
            return True
        if isinstance(aws, str):
            aws = [aws]
        if isinstance(aws, list):
            for a in aws:
                if a == '*':
                    return True
                if requester_tenant and a == f'arn:aws:iam::{requester_tenant}:root':
                    return True
    return False

def _action_matches(stmt_actions, action):
    if isinstance(stmt_actions, str):
        stmt_actions = [stmt_actions]
    for a in stmt_actions:
        if a == action or a == 's3:*':
            return True
        if a.endswith('*') and action.startswith(a[:-1]):
            return True
    return False

def _resource_matches(stmt_resources, bucket, key):
    if isinstance(stmt_resources, str):
        stmt_resources = [stmt_resources]
    bucket_arn = f'arn:aws:s3:::{bucket}'
    obj_arn = f'arn:aws:s3:::{bucket}/{key}' if key else None
    for r in stmt_resources:
        if r == bucket_arn and key is None:
            return True
        if obj_arn and r == obj_arn:
            return True
        if obj_arn and r.endswith('*'):
            prefix = r[:-1]
            if obj_arn.startswith(prefix):
                return True
        if key is None and r.endswith('*') and bucket_arn.startswith(r[:-1]):
            return True
    return False

def evaluate(policy, action, bucket, key, requester_tenant):
    """Returns 'Allow', 'Deny', or 'NoMatch'. requester_tenant=None for anonymous."""
    if not policy:
        return 'NoMatch'
    stmts = policy.get('Statement', [])
    if isinstance(stmts, dict):
        stmts = [stmts]
    matched_allow = False
    for s in stmts:
        principal = s.get('Principal')
        if not _principal_matches(principal, requester_tenant):
            continue
        if not _action_matches(s.get('Action'), action):
            continue
        if not _resource_matches(s.get('Resource'), bucket, key):
            continue
        if s.get('Effect') == 'Deny':
            return 'Deny'
        if s.get('Effect') == 'Allow':
            matched_allow = True
    return 'Allow' if matched_allow else 'NoMatch'
