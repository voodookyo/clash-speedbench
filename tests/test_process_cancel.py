import subprocess
import sys
import threading
import unittest
from unittest import mock
from speedbench_process import run_cancellable


class ProcessCancellationTest(unittest.TestCase):
    def test_without_cancel_keeps_legacy_subprocess_run_contract(self):
        expected = subprocess.CompletedProcess(['fixture'],0,'ok','')
        with mock.patch('subprocess.run',return_value=expected) as run:
            actual = run_cancellable(['fixture'],text=True,capture_output=True,timeout=3)
        self.assertIs(actual,expected)
        run.assert_called_once_with(['fixture'],text=True,capture_output=True,timeout=3)

    def test_pre_cancel_does_not_start_any_process(self):
        with mock.patch('subprocess.Popen') as popen:
            with self.assertRaises(KeyboardInterrupt):
                run_cancellable(['fixture'],cancel=lambda:True)
            popen.assert_not_called()

    def test_real_child_cancelled_and_reaped_without_touching_other_processes(self):
        cancelled = threading.Event()
        captured = []
        real_popen = subprocess.Popen
        def popen(*args,**kwargs):
            p = real_popen(*args,**kwargs)
            captured.append(p)
            cancelled.set()
            return p
        with mock.patch('subprocess.Popen',side_effect=popen):
            with self.assertRaises(KeyboardInterrupt):
                run_cancellable([sys.executable,'-c','import time; time.sleep(20)'],
                                cancel=cancelled.is_set,capture_output=True,text=True,timeout=3)
        self.assertEqual(len(captured),1)
        self.assertIsNotNone(captured[0].poll())

    def test_normal_completion_preserves_text_and_code(self):
        p = run_cancellable([sys.executable,'-c','print("value")'],cancel=lambda:False,
                            capture_output=True,text=True,encoding='utf-8',timeout=3)
        self.assertEqual(p.returncode,0)
        self.assertEqual(p.stdout.strip(),'value')

    def test_timeout_reaps_child_and_raises_standard_timeout(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            run_cancellable([sys.executable,'-c','import time; time.sleep(20)'],cancel=lambda:False,
                            capture_output=True,timeout=.05)

    def test_checked_nonzero_exit_preserves_standard_error(self):
        with self.assertRaises(subprocess.CalledProcessError):
            run_cancellable([sys.executable,'-c','raise SystemExit(2)'],cancel=lambda:False,
                            capture_output=True,check=True,timeout=3)
