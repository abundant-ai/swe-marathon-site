from flask import Response
import xml.sax.saxutils as sx

ERROR_STATUS = {
    'NoSuchBucket': 404,
    'NoSuchKey': 404,
    'NoSuchVersion': 404,
    'NoSuchUpload': 404,
    'NoSuchCORSConfiguration': 404,
    'NoSuchLifecycleConfiguration': 404,
    'NoSuchBucketPolicy': 404,
    'BucketAlreadyOwnedByYou': 409,
    'BucketAlreadyExists': 409,
    'BucketNotEmpty': 409,
    'SignatureDoesNotMatch': 403,
    'InvalidAccessKeyId': 403,
    'AccessDenied': 403,
    'CORSForbidden': 403,
    'QuotaExceeded': 403,
    'TooManyBuckets': 400,
    'InvalidArgument': 400,
    'InvalidBucketName': 400,
    'InvalidPart': 400,
    'InvalidPartOrder': 400,
    'InvalidTag': 400,
    'EntityTooLarge': 400,
    'EntityTooSmall': 400,
    'PreconditionFailed': 412,
    'InvalidRange': 416,
    'MalformedXML': 400,
    'MalformedPolicy': 400,
    'NotImplemented': 501,
    'InternalError': 500,
    'MethodNotAllowed': 405,
    'NoSuchTagSet': 404,
}

class S3Error(Exception):
    def __init__(self, code, message=None, status=None, **extra):
        self.code = code
        self.message = message or code
        self.status = status or ERROR_STATUS.get(code, 400)
        self.extra = extra

def s3_error_response(err, request_id='', resource=''):
    parts = [f'<Code>{sx.escape(err.code)}</Code>',
             f'<Message>{sx.escape(err.message)}</Message>']
    for k, v in err.extra.items():
        parts.append(f'<{k}>{sx.escape(str(v))}</{k}>')
    parts.append(f'<RequestId>{sx.escape(request_id)}</RequestId>')
    parts.append(f'<HostId>{sx.escape(request_id)}</HostId>')
    if resource:
        parts.append(f'<Resource>{sx.escape(resource)}</Resource>')
    body = '<?xml version="1.0" encoding="UTF-8"?>\n<Error>' + ''.join(parts) + '</Error>'
    resp = Response(body, status=err.status, mimetype='application/xml')
    resp.headers['x-amz-request-id'] = request_id
    return resp
