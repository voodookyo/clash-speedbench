import subprocess
import sys
import tempfile
import threading
import gc
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

    def test_failed_reap_does_not_implicitly_remove_live_configuration_on_gc(self):
        worker=workers.Worker('fixture',[],{},None)
        proc=mock.Mock();proc.poll.return_value=None
        proc.wait.side_effect=subprocess.TimeoutExpired('fixture',3)
        with mock.patch.object(workers.subprocess,'Popen',return_value=proc):
            worker._initialize_unlocked()
        folder=Path(worker.dir.name)
        try:
            with self.assertRaises(workers.WorkerCleanupError):worker.stop()
            del worker;gc.collect()
            self.assertTrue(folder.exists(),'Unreaped process config must not be removed by object finalization')
        finally:
            # Exact fixture-only layout; no recursive deletion or real process.
            if (folder/'config.json').exists():(folder/'config.json').unlink()
            if folder.exists():folder.rmdir()


class WorkerGroupCleanupTest(unittest.TestCase):
    def test_all_registered_workers_enter_stop_before_any_wait_finishes(self):
        count=16;barrier=threading.Barrier(count);stopped=[];lock=threading.Lock()
        class FixtureWorker:
            def stop(self):
                barrier.wait(timeout=2)
                with lock:stopped.append(self)
        group=[FixtureWorker() for _ in range(count)]
        workers.stop_workers(group)
        self.assertEqual({id(w) for w in group},{id(w) for w in stopped})

    def test_failures_are_collected_after_every_worker_is_attempted_without_raw_error(self):
        group=[mock.Mock(),mock.Mock(),mock.Mock()]
        group[0].stop.side_effect=workers.WorkerCleanupError('CANARY private path')
        group[1].stop.side_effect=PermissionError('CANARY credential')
        with self.assertRaises(workers.WorkerCleanupError) as caught:workers.stop_workers(group)
        for worker in group:worker.stop.assert_called_once_with()
        self.assertNotIn('CANARY',str(caught.exception))

    def test_duplicate_registrations_are_stopped_once_and_empty_group_is_safe(self):
        worker=mock.Mock();workers.stop_workers([worker,worker]);worker.stop.assert_called_once_with()
        workers.stop_workers([])

    def test_real_registered_group_reaps_only_owned_handles_and_leaves_other_process_alive(self):
        group=[];other=None
        def process():
            return subprocess.Popen([sys.executable,'-c','import time; time.sleep(20)'],
                stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        try:
            other=process()
            for _ in range(2):
                worker=workers.Worker('fixture',[],{},None)
                worker.dir=workers._WorkerDirectory();group.append(worker);worker.proc=process()
            directories=[Path(w.dir.name) for w in group]
            workers.stop_workers(group)
            self.assertTrue(all(w.proc.poll() is not None for w in group))
            self.assertTrue(all(not p.exists() for p in directories))
            self.assertIsNone(other.poll())
        finally:
            for proc in [w.proc for w in group]+[other]:
                if proc is not None and proc.poll() is None:proc.kill();proc.wait(timeout=3)
            for worker in group:worker.dir.cleanup()
