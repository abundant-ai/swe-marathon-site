import os
import hashlib
import tempfile
import shutil

DATA_DIR = os.environ.get('HALYARD_DATA_DIR', '/app/data')
BLOB_DIR = os.path.join(DATA_DIR, 'blobs')
TMP_DIR = os.path.join(DATA_DIR, 'tmp')

def _ensure_dirs():
    os.makedirs(BLOB_DIR, exist_ok=True)
    os.makedirs(TMP_DIR, exist_ok=True)

def store_bytes(data: bytes):
    _ensure_dirs()
    digest = hashlib.sha256(data).hexdigest()
    sub = os.path.join(BLOB_DIR, digest[:2], digest[2:4])
    os.makedirs(sub, exist_ok=True)
    path = os.path.join(sub, digest)
    if not os.path.exists(path):
        fd, tmp = tempfile.mkstemp(dir=TMP_DIR)
        try:
            with os.fdopen(fd, 'wb') as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except Exception:
            try: os.unlink(tmp)
            except: pass
            raise
    return path

def store_stream_to_path(stream, path=None):
    """Stream to a tmp file and return (path, size, md5_hex, sha256_hex). Final path is content-addressed."""
    _ensure_dirs()
    fd, tmp = tempfile.mkstemp(dir=TMP_DIR)
    md5 = hashlib.md5()
    sha = hashlib.sha256()
    size = 0
    with os.fdopen(fd, 'wb') as f:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            f.write(chunk)
            md5.update(chunk)
            sha.update(chunk)
            size += len(chunk)
        f.flush()
        os.fsync(f.fileno())
    digest = sha.hexdigest()
    sub = os.path.join(BLOB_DIR, digest[:2], digest[2:4])
    os.makedirs(sub, exist_ok=True)
    final_path = os.path.join(sub, digest)
    if os.path.exists(final_path):
        os.unlink(tmp)
    else:
        os.replace(tmp, final_path)
    return final_path, size, md5.hexdigest(), digest

def read_bytes(path, offset=0, length=None):
    with open(path, 'rb') as f:
        if offset:
            f.seek(offset)
        if length is None:
            return f.read()
        return f.read(length)

def open_for_read(path):
    return open(path, 'rb')

def file_size(path):
    return os.path.getsize(path)
