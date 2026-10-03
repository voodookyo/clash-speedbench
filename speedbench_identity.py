"""Private local identity seed and domain-separated opaque IDs (stdlib only)."""
import csv
import ctypes
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import threading


class IdentityError(ValueError):
    """Safe, non-secret error suitable for a UI status message."""


_LOCK = threading.RLock()


def opaque_id(seed, domain, value):
    if not isinstance(seed, bytes) or len(seed) < 32:
        raise IdentityError('Identity seed unavailable')
    body = json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False).encode('utf-8')
    digest = hmac.new(seed, domain.encode('ascii') + b'\0' + body, hashlib.sha256).hexdigest()
    return domain + '_v2_' + digest[:32]


def _windows_sid():
    try:
        result = subprocess.run(['whoami.exe', '/user', '/fo', 'csv', '/nh'],
                                capture_output=True, text=True, timeout=5,
                                encoding='utf-8', errors='replace',
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        for row in csv.reader(io.StringIO(result.stdout)):
            for field in row:
                if re.fullmatch(r'S-1-(?:\d+-)*\d+', field.strip()):
                    return field.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    raise IdentityError('Cannot determine identity file owner')


def _windows_private(path, sid):
    """Check actual owner/DACL, not localized icacls human-readable output."""
    from ctypes import wintypes
    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    get_info = advapi.GetNamedSecurityInfoW
    get_info.argtypes = [wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD,
                         ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
                         ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
                         ctypes.POINTER(ctypes.c_void_p)]
    get_info.restype = wintypes.DWORD
    convert = advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW
    convert.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                        ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(wintypes.DWORD)]
    convert.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    descriptor = ctypes.c_void_p()
    output = wintypes.LPWSTR()
    try:
        if get_info(str(path), 1, 5, None, None, None, None, ctypes.byref(descriptor)):
            raise IdentityError('Cannot inspect identity file permissions')
        if not convert(descriptor, 1, 5, ctypes.byref(output), None):
            raise IdentityError('Cannot inspect identity file permissions')
        sddl = output.value or ''
        owner = re.search(r'O:(.*?)(?=[GDS]:|$)', sddl)
        allowed = {sid, 'SY', 'BA', 'S-1-5-18', 'S-1-5-32-544'}
        entries = re.findall(r'\(([^()]*)\)', sddl)
        if not owner or owner.group(1) != sid or 'D:' not in sddl or not entries:
            raise IdentityError('Identity file owner or permissions are unsafe')
        for entry in entries:
            fields = entry.split(';')
            if len(fields) != 6 or fields[0] not in ('A', 'D'):
                raise IdentityError('Identity file permissions are unsupported')
            if fields[0] == 'A' and fields[5] not in allowed:
                raise IdentityError('Identity file permissions are too broad')
    finally:
        if output:
            kernel.LocalFree(ctypes.cast(output, ctypes.c_void_p))
        if descriptor:
            kernel.LocalFree(descriptor)


def _secure_new_file(path, sid):
    if os.name != 'nt':
        os.chmod(path, 0o600)
        return
    try:
        result = subprocess.run(['icacls.exe', str(path), '/inheritance:r',
                                 '/grant:r', '*' + sid + ':(F)'],
                                capture_output=True, timeout=5,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, subprocess.SubprocessError):
        raise IdentityError('Cannot restrict identity file permissions') from None
    if result.returncode:
        raise IdentityError('Cannot restrict identity file permissions')
    _windows_private(path, sid)


def _read_seed(path, sid):
    try:
        before = path.lstat()
        # Atomic installation briefly has both the prepared and final hardlink.
        # Security comes from ownership/ACL and inode validation, not link count.
        if not stat.S_ISREG(before.st_mode):
            raise IdentityError('Identity file must be a private regular file')
        if os.name == 'nt':
            _windows_private(path, sid)
        elif before.st_uid != os.getuid() or before.st_mode & 0o077:
            raise IdentityError('Identity file permissions are too broad')
        descriptor = os.open(str(path), os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(descriptor, 'rb') as stream:
            actual = os.fstat(stream.fileno())
            if (actual.st_ino, actual.st_dev) != (before.st_ino, before.st_dev):
                raise IdentityError('Identity file changed during inspection')
            value = stream.read(33)
        if len(value) != 32:
            raise IdentityError('Identity file is invalid; restore a private backup')
        return value
    except IdentityError:
        raise
    except OSError:
        raise IdentityError('Cannot read private identity file') from None


def load_seed(path):
    """Atomic first creation, refusing to overwrite corrupt/unsafe old seeds.

    A prepared private temporary file is linked without replacing an existing
    destination.  A concurrent process either wins once or reads that winner.
    Only this function's own temporary file is removed.
    """
    path = Path(path).absolute()
    with _LOCK:
        sid = _windows_sid() if os.name == 'nt' else None
        if path.exists() or path.is_symlink():
            return _read_seed(path, sid)
        temporary = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(prefix='.identity-', dir=str(path.parent))
            with os.fdopen(descriptor, 'wb') as stream:
                _secure_new_file(temporary, sid)
                stream.write(os.urandom(32))
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass
            os.unlink(temporary)
            temporary = None
            return _read_seed(path, sid)
        except IdentityError:
            raise
        except OSError:
            raise IdentityError('Cannot create private identity file') from None
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
