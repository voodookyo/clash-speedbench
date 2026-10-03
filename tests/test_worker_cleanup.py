import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# Platform tests reload the module during full discovery. Resolve exception
# classes through the module so assertions don't retain a pre-reload class.
import speedbench_workers as workers


class WorkerCleanupTest(unittest.TestCase):
    def test_cleanup_error_must_not_be_a_worker_unavailable_fallback(self):
        self.assertFalse(issubclass(workers.WorkerCleanupError, workers.WorkerUnavailable))

    def worker(self):
        worker = workers.Worker('fixture', [], {}, None)
        worker.proc = mock.Mock()
        worker.proc.poll.return_value = None
        worker.dir = mock.Mock()
        return worker

    def test_kill_escalation_waits_before_removing_configuration(self):
        worker = self.worker()
        worker.proc.wait.side_effect = [subprocess.TimeoutExpired('fixture', 3), 0]
        ordered = mock.Mock()
        ordered.attach_mock(worker.proc, 'proc')
        ordered.attach_mock(worker.dir, 'dir')
        worker.stop()
        names = [c[0] for c in ordered.mock_calls]
        self.assertEqual(names, ['proc.poll', 'proc.terminate', 'proc.wait',
                                'proc.kill', 'proc.wait', 'dir.cleanup'])
        worker.stop()
        worker.dir.cleanup.assert_called_once()

    def test_failed_reap_does_not_claim_cleanup_or_delete_running_config(self):
        worker = self.worker()
        worker.proc.wait.side_effect = subprocess.TimeoutExpired('CANARY-secret', 3)
        with self.assertRaises(workers.WorkerCleanupError) as caught:
            worker.stop()
        self.assertNotIn('CANARY', str(caught.exception))
        worker.dir.cleanup.assert_not_called()
        worker.proc.wait.side_effect = None
        worker.proc.wait.return_value = 0
        worker.stop()
        worker.dir.cleanup.assert_called_once()

    def test_failed_directory_cleanup_is_retryable_after_process_reap(self):
        worker = self.worker()
        worker.proc.wait.return_value = 0
        worker.dir.cleanup.side_effect = [PermissionError('CANARY-private-path'), None]
        with self.assertRaises(workers.WorkerCleanupError) as caught:
            worker.stop()
        self.assertNotIn('CANARY', str(caught.exception))
        worker.stop()
        self.assertEqual(worker.dir.cleanup.call_count, 2)
        worker.proc.terminate.assert_called_once()

    def test_shutdown_marker_still_blocks_start_when_cleanup_is_incomplete(self):
        worker = self.worker()
        worker.dir.cleanup.side_effect = PermissionError('private path')
        with self.assertRaises(workers.WorkerCleanupError):
            worker.stop()
        with mock.patch.object(worker, '_initialize_unlocked') as initialize:
            with self.assertRaises(workers.WorkerCleanupError):
                worker.start()
            initialize.assert_not_called()

    def test_real_owned_process_is_reaped_before_exact_temp_directory_disappears(self):
        worker = workers.Worker('fixture', [], {}, None)
        worker.dir = tempfile.TemporaryDirectory(prefix='speedbench-cleanup-fixture-')
        folder = Path(worker.dir.name)
        worker.proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            worker.stop()
            self.assertIsNotNone(worker.proc.poll())
            self.assertFalse(folder.exists())
            worker.stop()
        finally:
            if worker.proc.poll() is None:
                worker.proc.kill()
                worker.proc.wait(timeout=3)
            worker.dir.cleanup()
