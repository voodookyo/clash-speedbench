"""Owned temporary Windows fixture: owner already correct, WRITE_OWNER denied.
Only disables privileges in this short-lived process; never modifies an account.
Run with Python and a source checkout argument. Baseline must fail before fix.
"""
import ctypes
from ctypes import wintypes as w
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
sys.path.insert(0, sys.argv[1])
import speedbench_identity as identity

sid = identity._windows_sid()
icacls = str(Path(os.environ['SystemRoot'])/'System32'/'icacls.exe')
advapi = ctypes.WinDLL('advapi32', use_last_error=True)
kernel = ctypes.WinDLL('kernel32', use_last_error=True)
kernel.GetCurrentProcess.restype = w.HANDLE
kernel.CloseHandle.argtypes = [w.HANDLE]
advapi.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)]
advapi.OpenProcessToken.restype = w.BOOL
advapi.AdjustTokenPrivileges.argtypes = [w.HANDLE, w.BOOL, ctypes.c_void_p, w.DWORD, ctypes.c_void_p, ctypes.c_void_p]
advapi.AdjustTokenPrivileges.restype = w.BOOL

with tempfile.TemporaryDirectory(prefix='speedbench-owner-regression-') as folder:
    path = Path(folder)/'private-fixture.txt'
    path.write_bytes(b'fixture raw')
    identity._secure_new_file(path, sid)
    subprocess.run([icacls, str(path), '/deny', '*'+sid+':(WO)'], capture_output=True, check=True)
    identity._windows_private(path, sid)
    token = w.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x28, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not advapi.AdjustTokenPrivileges(token, True, None, 0, None, None):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.CloseHandle(token)
    result = {'current_owner_and_private_acl_verified_before': True}
    try:
        identity._secure_new_file(path, sid)
        identity._windows_private(path, sid)
        assert path.read_bytes() == b'fixture raw'
        result['protected_without_reassigning_owner'] = True
        print(json.dumps(result))
    finally:
        subprocess.run([icacls, str(path), '/remove:d', '*'+sid], capture_output=True, check=True)
