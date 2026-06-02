"""S3 XML error response helper."""
from xml.sax.saxutils import escape


# code -> default HTTP status
ERROR_STATUS = {
    "NoSuchBucket": 404,
    "NoSuchKey": 404,
    "NoSuchVersion": 404,
    "NoSuchUpload": 404,
    "NoSuchCORSConfiguration": 404,
    "NoSuchLifecycleConfiguration": 404,
    "NoSuchBucketPolicy": 404,
    "BucketAlreadyOwnedByYou": 409,
    "BucketAlreadyExists": 409,
    "BucketNotEmpty": 409,
    "SignatureDoesNotMatch": 403,
    "InvalidAccessKeyId": 403,
    "AccessDenied": 403,
    "CORSForbidden": 403,
    "QuotaExceeded": 403,
    "TooManyBuckets": 400,
    "InvalidArgument": 400,
    "InvalidBucketName": 400,
    "InvalidPart": 400,
    "InvalidPartOrder": 400,
    "InvalidTag": 400,
    "EntityTooLarge": 400,
    "EntityTooSmall": 400,
    "PreconditionFailed": 412,
    "InvalidRange": 416,
    "MalformedXML": 400,
    "MalformedPolicy": 400,
    "MethodNotAllowed": 405,
    "NotImplemented": 501,
    "RequestTimeTooSkewed": 403,
    "AuthorizationHeaderMalformed": 400,
    "AuthorizationQueryParametersError": 400,
    "MissingContentLength": 411,
    "InvalidRequest": 400,
}

ERROR_MESSAGES = {
    "NoSuchBucket": "The specified bucket does not exist",
    "NoSuchKey": "The specified key does not exist.",
    "NoSuchVersion": "The specified version does not exist.",
    "NoSuchUpload": "The specified multipart upload does not exist.",
    "NoSuchCORSConfiguration": "The CORS configuration does not exist",
    "NoSuchLifecycleConfiguration": "The lifecycle configuration does not exist",
    "NoSuchBucketPolicy": "The bucket policy does not exist",
    "BucketAlreadyOwnedByYou": "Your previous request to create the named bucket succeeded and you already own it.",
    "BucketAlreadyExists": "The requested bucket name is not available.",
    "BucketNotEmpty": "The bucket you tried to delete is not empty",
    "SignatureDoesNotMatch": "The request signature we calculated does not match the signature you provided.",
    "InvalidAccessKeyId": "The AWS Access Key Id you provided does not exist in our records.",
    "AccessDenied": "Access Denied",
    "CORSForbidden": "CORS forbidden",
    "QuotaExceeded": "Tenant quota exceeded",
    "TooManyBuckets": "You have attempted to create more buckets than allowed",
    "InvalidArgument": "Invalid Argument",
    "InvalidBucketName": "The specified bucket is not valid.",
    "InvalidPart": "One or more of the specified parts could not be found.",
    "InvalidPartOrder": "The list of parts was not in ascending order.",
    "InvalidTag": "The TagSet is invalid",
    "EntityTooLarge": "Your proposed upload exceeds the maximum allowed size",
    "EntityTooSmall": "Your proposed upload is smaller than the minimum allowed size",
    "PreconditionFailed": "At least one of the preconditions you specified did not hold.",
    "InvalidRange": "The requested range is not satisfiable",
    "MalformedXML": "The XML you provided was not well-formed",
    "MalformedPolicy": "The policy is malformed",
    "MethodNotAllowed": "The specified method is not allowed against this resource.",
    "NotImplemented": "Not implemented",
    "RequestTimeTooSkewed": "The difference between the request time and the current time is too large.",
    "AuthorizationHeaderMalformed": "The authorization header is malformed",
    "AuthorizationQueryParametersError": "Error parsing the X-Amz-Credential parameter",
    "MissingContentLength": "Missing Content-Length",
    "InvalidRequest": "Invalid request",
}


class S3Error(Exception):
    def __init__(self, code, message=None, status=None, **extra):
        self.code = code
        self.message = message or ERROR_MESSAGES.get(code, code)
        self.status = status if status is not None else ERROR_STATUS.get(code, 400)
        self.extra = extra
        super().__init__(self.message)


def s3_error_xml(code, message, request_id, resource="", **extra):
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<Error>',
        f'<Code>{escape(code)}</Code>',
        f'<Message>{escape(message)}</Message>',
    ]
    if resource:
        parts.append(f'<Resource>{escape(resource)}</Resource>')
    for k, v in extra.items():
        parts.append(f'<{k}>{escape(str(v))}</{k}>')
    parts.append(f'<RequestId>{escape(request_id)}</RequestId>')
    parts.append('</Error>')
    return "".join(parts)
