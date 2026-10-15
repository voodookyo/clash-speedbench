"""Owned temporary Windows fixture: mkdir(0o700) under a default-owner token.

A short-lived child sets only ITS OWN process token's default owner to the
current user (SetTokenInformation TokenOwner); no account, service or desktop
process is touched.  Reproduces the limited GUI token whose mkdir(mode=0o700)
backup directories carry an explicit OWNER RIGHTS (OW, S-1-3-4) ACE.
Run with Python and a source checkout argument. Baseline must fail before fix;
a missing OW ACE or failed API call must fail loudly, never skip.
"""
import ctypes
from ctypes import wintypes as w
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
from contextlib import closing

sys.dont_write_bytecode = True
sys.path.insert(0, sys.argv[1])
import speedbench_identity as identity

TOKEN_QUERY = 0x0008
TOKEN_ADJUST_DEFAULT = 0x0080
TOKEN_OWNER_INFORMATION = 4
OWNER_RIGHTS_ACE = re.compile(r'\(A;[^;]*;[^;]*;[^;]*;[^;]*;(?:OW|S-1-3-4)\)')

sid = identity._windows_sid()
advapi = ctypes.WinDLL('advapi32', use_last_error=True)
kernel = ctypes.WinDLL('kernel32', use_last_error=True)
kernel.GetCurrentProcess.restype = w.HANDLE
kernel.CloseHandle.argtypes = [w.HANDLE]
kernel.LocalFree.argtypes = [ctypes.c_void_p]
kernel.LocalFree.restype = ctypes.c_void_p
advapi.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)]
advapi.OpenProcessToken.restype = w.BOOL
advapi.ConvertStringSidToSidW.argtypes = [w.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
advapi.ConvertStringSidToSidW.restype = w.BOOL
advapi.SetTokenInformation.argtypes = [w.HANDLE, w.DWORD, ctypes.c_void_p, w.DWORD]
advapi.SetTokenInformation.restype = w.BOOL


class TOKEN_OWNER(ctypes.Structure):
    _fields_ = [('Owner', ctypes.c_void_p)]


def real_sddl(path):
    """Real (not mocked) SDDL of an existing path, owner plus DACL."""
    get_info = advapi.GetNamedSecurityInfoW
    get_info.argtypes = [w.LPWSTR, ctypes.c_int, w.DWORD,
                         ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
                         ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
                         ctypes.POINTER(ctypes.c_void_p)]
    get_info.restype = w.DWORD
    convert = advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW
    convert.argtypes = [ctypes.c_void_p, w.DWORD, w.DWORD,
                        ctypes.POINTER(w.LPWSTR), ctypes.POINTER(w.DWORD)]
    convert.restype = w.BOOL
    descriptor = ctypes.c_void_p()
    owner_sid = ctypes.c_void_p()
    output = w.LPWSTR()
    try:
        code = get_info(str(path), 1, 5, ctypes.byref(owner_sid), None, None, None,
                        ctypes.byref(descriptor))
        if code:
            raise ctypes.WinError(code)
        if not convert(descriptor, 1, 5, ctypes.byref(output), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return output.value or ''
    finally:
        if output:
            kernel.LocalFree(ctypes.cast(output, ctypes.c_void_p))
        if descriptor:
            kernel.LocalFree(descriptor)


token = w.HANDLE()
if not advapi.OpenProcessToken(kernel.GetCurrentProcess(),
                               TOKEN_QUERY | TOKEN_ADJUST_DEFAULT,
                               ctypes.byref(token)):
    raise ctypes.WinError(ctypes.get_last_error())
try:
    owner = ctypes.c_void_p()
    if not advapi.ConvertStringSidToSidW(sid, ctypes.byref(owner)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        default_owner = TOKEN_OWNER(owner)
        if not advapi.SetTokenInformation(token, TOKEN_OWNER_INFORMATION,
                                          ctypes.byref(default_owner),
                                          ctypes.sizeof(default_owner)):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.LocalFree(owner)
finally:
    kernel.CloseHandle(token)

result = {'default_owner_token_applied': True}
with tempfile.TemporaryDirectory(prefix='speedbench-owner-rights-') as folder:
    directory = Path(folder)/'history-import-backups'
    directory.mkdir(mode=0o700)
    result['numeric_owner_verified_before_secure'] = identity._windows_owner(directory) == sid
    assert result['numeric_owner_verified_before_secure'], 'mkdir(0o700) owner mismatch'
    sddl = real_sddl(directory)
    assert OWNER_RIGHTS_ACE.search(sddl), 'no OWNER RIGHTS ACE in real SDDL'
    result['owner_rights_ace_in_real_sddl'] = True
    identity._secure_new_file(directory, sid)
    identity._windows_private(directory, sid)
    result['secure_private_accepted_owner_rights'] = True

with tempfile.TemporaryDirectory(prefix='speedbench-owner-rights-home-') as home_root, \
     tempfile.TemporaryDirectory(prefix='speedbench-owner-rights-source-') as source_root:
    home = Path(home_root)
    source = Path(source_root)
    raw = json.dumps(dict(ts='2026-10-14T01:00:00', mb=10, rounds=1, results=[dict(
        name='fixture', proto='ss', latency_ms=20, median_mbps=12, status='ok')]))
    (source/'speedbench-history.jsonl').write_text(raw+'\n', encoding='utf-8')
    from speedbench_owner import BackendLease
    from speedbench_transfer import HistoryTransfer
    owner = BackendLease(home).acquire()
    try:
        service = HistoryTransfer(home)
        preview = service.preview(str(source), owner)
        assert preview['can_apply'], preview
        applied = service.apply(preview['token'], owner)
        assert applied['imported_runs'] == 1, applied
        backup = home/'history-import-backups'/applied['backup_id']
        assert OWNER_RIGHTS_ACE.search(real_sddl(backup)), 'apply() backup lost OWNER RIGHTS ACE'
        with closing(sqlite3.connect(home/'speedbench-history.db')) as conn:
            stored = conn.execute('SELECT raw FROM runs LIMIT 1').fetchone()[0]
        result['history_sql_raw_preserved'] = stored == raw
        assert result['history_sql_raw_preserved'], stored
        assert (source/'speedbench-history.jsonl').read_bytes() == (raw+'\n').encode('utf-8')
        result['source_bytes_unchanged'] = True
        assert service.rollback(applied['backup_id'], owner)['ok']
        assert not (home/'speedbench-history.jsonl').exists()
        assert not (home/'speedbench-history.db').exists()
        receipt = json.loads((backup/'receipt.json').read_text(encoding='utf-8'))
        assert receipt['state'] == 'rolled_back', receipt
        result['history_import_applied_and_rolled_back'] = True
    finally:
        owner.close()
print(json.dumps(result))
