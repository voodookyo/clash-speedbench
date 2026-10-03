import concurrent.futures
import os
import tempfile
import unittest
import subprocess
import sys
from pathlib import Path

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


if __name__ == '__main__':
    unittest.main()
