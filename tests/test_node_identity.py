import concurrent.futures
import ctypes
import os
import tempfile
import unittest
import subprocess
import sys
from pathlib import Path
from unittest import mock

from speedbench_identity import IdentityError, load_seed, opaque_id


class IdentityTest(unittest.TestCase):
    def test_keyed_ids_not_plain_hashes(self):
        self.assertEqual(opaque_id(b'a' * 32, 'node', {'x': 1}), opaque_id(b'a' * 32, 'node', {'x': 1}))
        self.assertNotEqual(opaque_id(b'a' * 32, 'node', {'x': 1}), opaque_id(b'b' * 32, 'node', {'x': 1}))
        self.assertNotEqual(opaque_id(b'a' * 32, 'node', {'x': 1}), opaque_id(b'a' * 32, 'subscription', {'x': 1}))

    def test_seed_created_once_and_private(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'identity-seed'
            first = load_seed(path)
            self.assertEqual(len(first), 32)
            self.assertEqual(first, load_seed(path))
            if os.name != 'nt':
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_seed_concurrent_first_creation_is_one_identity(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'identity-seed'
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                seeds = list(pool.map(lambda _: load_seed(path), range(8)))
            self.assertEqual(len(set(seeds)), 1)

    def test_seed_multiple_process_first_creation_is_one_identity(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'identity-seed'
            code = ('import sys; from speedbench_identity import load_seed,opaque_id; '
                    'print(opaque_id(load_seed(sys.argv[1]),"test",{}))')
            children = [subprocess.Popen([sys.executable,'-c',code,str(path)],
                        stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0)) for _ in range(4)]
            try:
                outputs = [p.communicate(timeout=15) for p in children]
                self.assertTrue(all(p.returncode == 0 for p in children), [x[1] for x in outputs])
                self.assertEqual(len({x[0] for x in outputs}), 1)
            finally:
                for p in children:
                    if p.poll() is None:
                        p.kill()
                        p.communicate()

    def test_corrupt_seed_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'identity-seed'
            path.write_bytes(b'CANARY-secret-invalid')
            with self.assertRaises(IdentityError) as caught:
                load_seed(path)
            self.assertNotIn('CANARY', str(caught.exception))
            self.assertEqual(path.read_bytes(), b'CANARY-secret-invalid')

    @unittest.skipUnless(os.name == 'nt', 'Windows ACL check')
    def test_broad_windows_acl_is_rejected(self):
        import subprocess
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'identity-seed'
            load_seed(path)
            result = subprocess.run(['icacls.exe',str(path),'/grant','*S-1-1-0:(R)'],
                                    capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 0)
            with self.assertRaises(IdentityError):
                load_seed(path)

    @unittest.skipIf(os.name == 'nt', 'POSIX permission check')
    def test_world_readable_seed_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'identity-seed'
            path.write_bytes(b'a' * 32)
            path.chmod(0o644)
            with self.assertRaises(IdentityError):
                load_seed(path)

    def test_new_file_owner_is_set_before_dacl_and_reverified(self):
        import speedbench_identity as identity
        order = []
        def set_owner(path, sid): order.append('owner')
        def run(*args, **kwargs):
            order.append('dacl')
            return subprocess.CompletedProcess(args, 0)
        def verify(path, sid): order.append('verify')
        with mock.patch.object(identity, '_windows_set_owner', set_owner), \
             mock.patch.object(identity.subprocess, 'run', run), \
             mock.patch.object(identity, '_windows_private', verify):
            identity._secure_new_windows_file('C:\\tmp\\identity-seed', 'S-1-5-21-1')
        self.assertEqual(order, ['owner', 'dacl', 'verify'])

    def test_windows_owner_alias_is_accepted_only_for_the_actual_current_user(self):
        import speedbench_identity as identity
        from ctypes import wintypes
        current = 'S-1-5-21-100-200-300-500'
        cases = (
            (current, 'O:LAD:P(A;;FA;;;LA)', True),
            (current, 'O:' + current + 'D:P(A;;FA;;;' + current + ')', True),
            ('S-1-5-32-544', 'O:BAD:P(A;;FA;;;BA)', False),
            (current, 'O:LAD:P(A;;FA;;;LA)(A;;FR;;;WD)', False),
        )
        for actual, sddl, safe in cases:
            with self.subTest(safe=safe, owner_matches=actual == current):
                owner_text = ctypes.create_unicode_buffer(actual)
                descriptor_text = ctypes.create_unicode_buffer(sddl)
                advapi, kernel = mock.Mock(), mock.Mock()
                def get_info(path, kind, flags, owner, group, dacl, sacl, descriptor):
                    ctypes.cast(owner, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.c_void_p(1)
                    ctypes.cast(descriptor, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.c_void_p(2)
                    return 0
                def convert_owner(owner, output):
                    ctypes.cast(output, ctypes.POINTER(wintypes.LPWSTR))[0] = ctypes.cast(owner_text, wintypes.LPWSTR)
                    return True
                def convert_descriptor(descriptor, revision, flags, output, size):
                    ctypes.cast(output, ctypes.POINTER(wintypes.LPWSTR))[0] = ctypes.cast(descriptor_text, wintypes.LPWSTR)
                    return True
                advapi.GetNamedSecurityInfoW.side_effect = get_info
                advapi.ConvertSidToStringSidW.side_effect = convert_owner
                advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW.side_effect = convert_descriptor
                libraries = {'advapi32': advapi, 'kernel32': kernel}
                with mock.patch.object(ctypes, 'WinDLL', create=True,
                                       side_effect=lambda name, **kwargs: libraries[name]):
                    if safe:
                        identity._windows_private('fixture.identity', current)
                    else:
                        with self.assertRaises(IdentityError):
                            identity._windows_private('fixture.identity', current)

    def test_new_file_owner_failure_never_touches_dacl_or_reverifies(self):
        import speedbench_identity as identity
        with mock.patch.object(identity, '_windows_set_owner',
                               side_effect=IdentityError('owner denied')), \
             mock.patch.object(identity.subprocess, 'run',
                               side_effect=AssertionError('icacls must not run')), \
             mock.patch.object(identity, '_windows_private',
                               side_effect=AssertionError('verification must not run')):
            with self.assertRaises(IdentityError):
                identity._secure_new_windows_file('C:\\tmp\\identity-seed', 'S-1-5-21-1')

    def test_new_file_dacl_failure_never_reverifies(self):
        import speedbench_identity as identity
        def run(*args, **kwargs): return subprocess.CompletedProcess(args, 1)
        with mock.patch.object(identity, '_windows_set_owner'), \
             mock.patch.object(identity.subprocess, 'run', run), \
             mock.patch.object(identity, '_windows_private',
                               side_effect=AssertionError('verification must not run')):
            with self.assertRaises(IdentityError):
                identity._secure_new_windows_file('C:\\tmp\\identity-seed', 'S-1-5-21-1')

    @unittest.skipUnless(os.name == 'nt', 'Windows owner boundary')
    def test_windows_seed_round_trip_in_unicode_space_path(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td) / '速度 bench 数据'
            folder.mkdir()
            path = folder / 'identity-seed'
            first = load_seed(path)
            self.assertEqual(len(first), 32)
            self.assertEqual(first, load_seed(path))


if __name__ == '__main__':
    unittest.main()
