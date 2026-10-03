"""OS-backed ownership, not PID-file existence. Uses temporary data only."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from speedbench_owner import BackendLease, LeaseError


class BackendOwnerTest(unittest.TestCase):
    def test_competing_port_process_cannot_own_same_data(self):
        with tempfile.TemporaryDirectory() as folder:
            with BackendLease(folder):
                code='from speedbench_owner import BackendLease,LeaseError\nimport sys\ntry:\n with BackendLease(sys.argv[1]): sys.exit(3)\nexcept LeaseError: sys.exit(0)'
                p=subprocess.run([sys.executable,'-c',code,folder],capture_output=True,timeout=10)
                self.assertEqual(p.returncode,0,p.stderr)

    def test_stale_metadata_does_not_block_and_release_keeps_lock_inode(self):
        with tempfile.TemporaryDirectory() as folder:
            with BackendLease(folder) as lease:
                path=lease.path
                self.assertTrue(path.exists())
            inode=path.stat().st_ino
            with BackendLease(folder):
                self.assertEqual(path.stat().st_ino,inode)
            value=path.read_text(encoding='utf-8')
            self.assertNotIn('token',value)
            self.assertNotIn('secret',value)
            self.assertEqual(json.loads(value[1:])['pid'],os.getpid())

    def test_history_never_changes_and_close_is_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            history=Path(folder)/'speedbench-history.jsonl';history.write_bytes(b'fixture original\n')
            lease=BackendLease(folder);lease.acquire();lease.close();lease.close()
            self.assertEqual(history.read_bytes(),b'fixture original\n')

    @unittest.skipUnless(os.name=='posix','POSIX permission fixture')
    def test_unsafe_existing_permissions_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'backend-owner.lock';path.write_bytes(b' ');path.chmod(0o666)
            with self.assertRaises(LeaseError):BackendLease(folder).acquire()

