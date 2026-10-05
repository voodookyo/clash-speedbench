# -*- coding: utf-8 -*-
"""Private desktop suspend/resume integration tests.

These tests drive the real production reservation helper, POST acceptance and
runner finalization paths with a portable fake native clock.  They never sleep
or suspend the machine: an ABI/clock test is not OS suspend proof, but it does
falsify the wiring races the integration must close (arm-before-dispatch,
queued cancel before Popen, one-shot poll, terminal/finalization interleaving,
clock-failure fail-closed and UI explanations).
"""
import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import speedbench_power as power
import speedbench_web as web
from speedbench_jobs import TERMINAL, safe_counters, safe_metrics
from speedbench_progress import ProgressEmitter
from speedbench_tasks import resolve_config
from tests.web_server_case import WebServerCase

GAP = power.ResumeGuard.DEFAULT_THRESHOLD_NS + 1


class FakeClock:
    """Portable clock returning a controllable (low, high) interval."""

    def __init__(self, sample=(0, 0)):
        self.sample = sample
        self.reads = 0

    def __call__(self):
        self.reads += 1
        return self.sample

    def suspend(self, gap=GAP):
        self.sample = (gap, gap)

    def settle(self):
        self.sample = (0, 0)


class FlakyClock(FakeClock):
    def __init__(self):
        super().__init__()
        self.fail = False

    def __call__(self):
        if self.fail:
            raise power.PowerClockError("SECRET", 123456789)
        return super().__call__()


class FakeProc:
    def __init__(self, exit_code=130, stdout=()):
        self._exit = exit_code
        self.stdout = stdout
        self.signals = []
        self.waited = False
        self.pid = 4242

    def poll(self):
        return self._exit if self.waited else None

    def send_signal(self, sig):
        self.signals.append(sig)

    def wait(self, timeout=None):
        self.waited = True
        return self._exit

    def terminate(self):
        self.waited = True

    def kill(self):
        self.waited = True


class DesktopPowerTest(WebServerCase):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patch = mock.patch.object(web, 'HISTORY', Path(self.temp.name) / 'history.jsonl')
        patch.start()
        self.addCleanup(patch.stop)
        self.set_state(running=False, job_id=None, cancel_requested=False,
                       power_clock_failed=False, cancel_reason=None)

    def reserve(self):
        with web.STATE_LOCK:
            return web._reserve_power_cancellation_locked()

    def create(self):
        return web.JOBS.create(resolve_config({'mode': 'quick'}))

    def wait_terminal(self, job, timeout=5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if web.JOBS.snapshot(job)['status'] in TERMINAL:
                return web.JOBS.snapshot(job)
            time.sleep(0.01)
        self.fail('job did not reach a terminal state')

    # ------------------------------------------------------------------
    # arm before dispatch / fresh baseline
    # ------------------------------------------------------------------
    def test_post_arms_exact_job_before_worker_dispatch(self):
        clock = FakeClock()
        monitor = web.PowerMonitor(clock)
        seen = {}
        entered = threading.Event()

        def runner(params):
            with web.STATE_LOCK:
                seen['armed'] = monitor.guard.armed_job
                seen['job_id'] = web.STATE.get('job_id')
                entered.set()

        with mock.patch.object(web, 'DESKTOP_POWER', monitor), \
                mock.patch.object(web, 'run_benchmark', runner):
            status, body = self.post_authorized('/api/jobs', {'mode': 'quick'})
            self.assertTrue(entered.wait(2))
        self.assertEqual(status, 202)
        job = json.loads(body)['job_id']
        self.assertEqual(seen['armed'], job)
        self.assertEqual(seen['job_id'], job)
        self.assertEqual(web.JOBS.snapshot(job)['status'], 'queued')
        monitor.disarm(job)

    def test_fresh_arm_after_idle_suspend_does_not_trigger(self):
        clock = FakeClock()
        monitor = web.PowerMonitor(clock)
        clock.suspend()          # machine was asleep while no job was armed
        monitor.arm('job_' + 'a' * 32)
        self.assertIsNone(monitor.poll())

    # ------------------------------------------------------------------
    # queued cancel before Popen
    # ------------------------------------------------------------------
    def test_immediate_sleep_after_acceptance_cancels_queued_job_before_popen(self):
        clock = FakeClock()
        monitor = web.PowerMonitor(clock)
        release = threading.Event()
        entered = threading.Event()
        done = threading.Event()
        runner = web.run_benchmark
        old_raw = '{"ts":"2026-10-01T00:00:00Z", "results":[]}\n'
        web.HISTORY.write_text(old_raw, encoding='utf-8')

        def run(params):
            try:runner(params)
            finally:done.set()

        def connect(*args, **kwargs):
            entered.set()
            release.wait(5)

        with mock.patch.object(web, 'DESKTOP_POWER', monitor), \
                mock.patch.object(web, 'connect_controller', connect), \
                mock.patch.object(web, 'sync_db', lambda: 0), \
                mock.patch.object(web, 'run_benchmark', run), \
                mock.patch.object(web.subprocess, 'Popen') as popen:
            status, body = self.post_authorized('/api/jobs', {'mode': 'quick'})
            self.assertEqual(status, 202)
            job = json.loads(body)['job_id']
            self.assertTrue(entered.wait(5))
            # Old raw counters that must remain untouched.
            web.JOBS.publish(job, 'phase_finished', phase='probing',
                             payload={'metrics': {'intel_cache': {'counters': {'cache_hits': 7}}}})
            clock.suspend()
            reserved = self.reserve()   # watcher poll
            self.assertEqual(reserved, (job, 'system_resume'))
            result = web.cancel_benchmark(expected_job_id=job, reason='system_resume')
            self.assertTrue(result['ok'])
            release.set()
            self.assertTrue(done.wait(5))
            snapshot = web.JOBS.snapshot(job)
        popen.assert_not_called()
        self.assertEqual(snapshot['status'], 'cancelled')
        self.assertTrue(snapshot['partial'])
        counters = snapshot['metrics']['cleanup']['counters']
        self.assertEqual(counters['system_resumes'], 1)
        self.assertEqual(snapshot['metrics']['intel_cache']['counters']['cache_hits'], 7)
        self.assertNotIn('power_clock_errors', counters)
        saved = web.speedbench_db.task_snapshot(web.db_path(), job)
        self.assertTrue(saved['partial'])
        self.assertEqual(saved['metrics']['cleanup']['counters']['system_resumes'], 1)
        self.assertEqual(web.HISTORY.read_text(encoding='utf-8'), old_raw)

    def test_watcher_cancels_a_real_owned_child_and_checkpoints_partial_history(self):
        clock = FakeClock()
        monitor = web.PowerMonitor(clock, interval=.01)
        job = self.create()
        self.set_state(running=True, job_id=job, proc=None, cancel_requested=False)
        monitor.arm(job)
        old = '{"ts":"2026-10-01T00:00:00Z", "results":[]}\n'
        web.HISTORY.write_text(old, encoding='utf-8')
        script = '''import json, signal, sys, time
from pathlib import Path
job, history, sentinel = sys.argv[1:]
stopped = False
def interrupt(*_):
    global stopped
    stopped = True
signal.signal(signal.SIGINT, interrupt)
def emit(seq, kind, phase, node='', payload=None):
    record = dict(version=1, job_id=job, source_seq=seq, type=kind,
                  phase=phase, node_id=node, payload=payload or {})
    print('@speedbench-event '+json.dumps(record), flush=True)
emit(1, 'phase_started', 'probing')
emit(2, 'node_probe', 'probing', 'keep', {'result':{'name':'keep','latency_ms':12}})
deadline = time.monotonic()+10
while not stopped and not Path(sentinel).exists():
    if time.monotonic()>deadline:sys.exit(2)
    time.sleep(.01)
with Path(history).open('a', encoding='utf-8') as stream:
    stream.write(json.dumps(dict(ts='2026-10-04T00:00:00Z', results=[dict(name='keep',latency_ms=12)],
        partial=True, task=dict(job_id=job)))+'\\n')
sys.exit(130)
'''
        child = subprocess.Popen([sys.executable, '-u', '-c', script, job,
                                  str(web.HISTORY), str(web.CANCEL_FILE)],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, text=True)
        runner = threading.Thread(target=web.run_benchmark,
            args=({'mode':'quick','_job_id':job},), daemon=True)
        try:
            with mock.patch.object(web, 'DESKTOP_POWER', monitor), \
                 mock.patch.object(web, 'connect_controller'), \
                 mock.patch.object(web.subprocess, 'Popen', return_value=child):
                runner.start()
                deadline = time.monotonic()+3
                while time.monotonic()<deadline and not web.JOBS.snapshot(job)['results']:
                    time.sleep(.01)
                self.assertEqual(web.JOBS.snapshot(job)['results'][0]['name'], 'keep')
                monitor.start()
                clock.suspend()
                runner.join(5)
                self.assertFalse(runner.is_alive())
                self.assertEqual(child.wait(timeout=1), 130)
                saved = web.speedbench_db.task_snapshot(web.db_path(), job)
                self.assertEqual(saved['status'], 'cancelled')
                self.assertTrue(saved['partial'])
                self.assertEqual(saved['results'][0]['latency_ms'], 12)
                self.assertEqual(saved['metrics']['cleanup']['counters'], {'system_resumes':1})
                self.assertFalse(web.STATE['running'])
                lines = web.HISTORY.read_text(encoding='utf-8').splitlines(keepends=True)
                self.assertEqual(lines[0], old)
                self.assertTrue(json.loads(lines[1])['partial'])
                self.assertNotIn('手动', '\n'.join(web.STATE['lines']))
        finally:
            monitor.stop();monitor.join()
            if child.poll() is None:child.kill()
            child.wait(timeout=2)
            runner.join(2)
            if child.stdout is not None:child.stdout.close()

    # ------------------------------------------------------------------
    # active owned child cooperative cancellation
    # ------------------------------------------------------------------
    def test_resumed_active_child_receives_cooperative_cancel_and_keeps_results(self):
        clock = FakeClock()
        monitor = web.PowerMonitor(clock)
        job = self.create()
        web.JOBS.publish(job, 'node_probe', node_id='keep',
                         payload={'result': {'name': 'keep', 'latency_ms': 12}})
        self.set_state(running=True, job_id=job, cancel_requested=False, proc=None, exit_code=None)
        monitor.arm(job)
        observed = {}

        def stdout_lines():
            # Runs on the runner thread once the owned process handle exists.
            clock.suspend()
            observed['reserved'] = self.reserve()
            observed['cancel'] = web.cancel_benchmark(expected_job_id=job, reason='system_resume')
            yield from ()

        proc = FakeProc(exit_code=130, stdout=stdout_lines())
        with mock.patch.object(web, 'DESKTOP_POWER', monitor), \
                mock.patch.object(web, 'connect_controller'), \
                mock.patch.object(web, 'sync_db', lambda: 0), \
                mock.patch.object(web.subprocess, 'Popen', return_value=proc):
            web.run_benchmark({'mode': 'quick', '_job_id': job})
        snapshot = web.JOBS.snapshot(job)
        self.assertEqual(observed['reserved'], (job, 'system_resume'))
        self.assertTrue(observed['cancel']['ok'])
        self.assertEqual(snapshot['status'], 'cancelled')
        self.assertTrue(snapshot['partial'])
        self.assertEqual([r['name'] for r in snapshot['results']], ['keep'])
        self.assertEqual(snapshot['metrics']['cleanup']['counters']['system_resumes'], 1)
        if sys.platform == 'win32':
            self.assertTrue(web.CANCEL_FILE.exists())
        else:
            self.assertIn(signal.SIGINT, proc.signals)
        self.assertFalse(web.STATE['running'])

    # ------------------------------------------------------------------
    # stale resume identity
    # ------------------------------------------------------------------
    def test_stale_resume_id_does_not_touch_new_job_sentinel_or_process(self):
        new_job = self.create()
        proc = mock.Mock()
        proc.poll.return_value = None
        self.set_state(running=True, job_id=new_job, proc=proc, cancel_requested=False)
        stale = 'job_' + '0' * 32
        with mock.patch.object(web.sys, 'platform', 'win32'):
            result = web.cancel_benchmark(expected_job_id=stale, reason='system_resume')
        self.assertFalse(result['ok'])
        self.assertFalse(web.STATE['cancel_requested'])
        self.assertEqual(web.JOBS.snapshot(new_job)['status'], 'queued')
        self.assertFalse(web.CANCEL_FILE.exists())
        proc.send_signal.assert_not_called()

    # ------------------------------------------------------------------
    # duplicate / manual cancellation
    # ------------------------------------------------------------------
    def test_duplicate_power_reservation_counts_once(self):
        clock = FakeClock()
        monitor = web.PowerMonitor(clock)
        job = self.create()
        self.set_state(running=True, job_id=job, cancel_requested=False)
        monitor.arm(job)
        clock.suspend()
        with mock.patch.object(web, 'DESKTOP_POWER', monitor):
            first = self.reserve()
            second = self.reserve()
        self.assertEqual(first, (job, 'system_resume'))
        self.assertIsNone(second)
        self.assertEqual(web.JOBS.snapshot(job)['metrics']['cleanup']['counters'],
                         {'system_resumes': 1})

    def test_manual_cancel_never_sets_a_power_counter(self):
        job = self.create()
        proc = FakeProc(exit_code=130)
        self.set_state(running=True, job_id=job, proc=proc, cancel_requested=False)
        result = web.cancel_benchmark(reason='manual')
        self.assertTrue(result['ok'])
        self.assertTrue(web.STATE['cancel_requested'])
        self.assertEqual(web.JOBS.snapshot(job)['metrics'], {})
        self.assertEqual(web.STATE.get('cancel_reason'), 'manual')

    def test_unknown_reason_is_rejected_without_cancellation(self):
        job = self.create()
        proc = FakeProc(exit_code=130)
        self.set_state(running=True, job_id=job, proc=proc, cancel_requested=False)
        result = web.cancel_benchmark(reason='caller-controlled-canary')
        self.assertFalse(result['ok'])
        self.assertFalse(web.STATE['cancel_requested'])
        self.assertFalse(proc.signals)
        self.assertIsNone(web.STATE.get('cancel_reason'))
        self.assertNotIn('caller-controlled-canary', json.dumps(web.JOBS.snapshot(job)))

    # ------------------------------------------------------------------
    # detection/finalization and poll/terminal interleaving
    # ------------------------------------------------------------------
    def test_completed_child_with_clock_gap_before_poll_becomes_partial(self):
        clock = FakeClock()
        monitor = web.PowerMonitor(clock)
        job = self.create()
        self.set_state(running=True, job_id=job, cancel_requested=False, proc=None, exit_code=None)
        monitor.arm(job)
        clock.suspend()          # confirmed gap, but the periodic watcher never ran
        stream = io.StringIO()
        emitter = ProgressEmitter(job, stream)
        emitter.emit('phase_started', 'probing')
        emitter.emit('node_probe', 'probing', 'n', {'result': {'name': 'n', 'latency_ms': 3}})
        emitter.emit('phase_started', 'finalizing')
        proc = FakeProc(exit_code=0, stdout=stream.getvalue().splitlines())
        with mock.patch.object(web, 'DESKTOP_POWER', monitor), \
                mock.patch.object(web, 'connect_controller'), \
                mock.patch.object(web, 'sync_db', lambda: 0), \
                mock.patch.object(web.subprocess, 'Popen', return_value=proc):
            web.run_benchmark({'mode': 'quick', '_job_id': job})
        snapshot = web.JOBS.snapshot(job)
        self.assertEqual(snapshot['status'], 'cancelled')
        self.assertTrue(snapshot['partial'])
        self.assertEqual(snapshot['metrics']['cleanup']['counters']['system_resumes'], 1)
        self.assertFalse(web.STATE['running'])
        self.assertIsNone(web.STATE['proc'])

    def test_watcher_reservation_then_runner_finalize_stays_valid(self):
        clock = FakeClock()
        monitor = web.PowerMonitor(clock)
        job = self.create()
        self.set_state(running=True, job_id=job, cancel_requested=False)
        monitor.arm(job)
        clock.suspend()
        with mock.patch.object(web, 'DESKTOP_POWER', monitor):
            self.assertEqual(self.reserve(), (job, 'system_resume'))
        # Runner finalizes a normal exit after the watcher consumed the poll.
        self.set_state(exit_code=0)
        with mock.patch.object(web, 'DESKTOP_POWER', monitor):
            self.assertIsNone(self.reserve())
        self.assertTrue(web.STATE['cancel_requested'])

    def test_late_wake_after_completion_cannot_switch_terminal_state(self):
        clock = FakeClock()
        monitor = web.PowerMonitor(clock)
        job = self.create()
        web.JOBS.transition(job, 'preparing')
        web.JOBS.transition(job, 'probing')
        web.JOBS.transition(job, 'finalizing')
        web.JOBS.transition(job, 'completed')
        self.set_state(running=True, job_id=job, cancel_requested=False)
        monitor.arm(job)
        clock.suspend()
        with mock.patch.object(web, 'DESKTOP_POWER', monitor):
            self.assertIsNone(self.reserve())
        self.assertEqual(web.JOBS.snapshot(job)['status'], 'completed')
        self.assertFalse(web.STATE['cancel_requested'])

    # ------------------------------------------------------------------
    # clock failure fail-closed
    # ------------------------------------------------------------------
    def test_poll_clock_failure_marks_backend_and_rejects_new_jobs(self):
        clock = FlakyClock()
        monitor = web.PowerMonitor(clock)
        job = self.create()
        self.set_state(running=True, job_id=job, cancel_requested=False)
        monitor.arm(job)
        clock.fail = True
        with mock.patch.object(web, 'DESKTOP_POWER', monitor):
            reserved = self.reserve()
        self.assertEqual(reserved, (job, 'power_clock_error'))
        self.assertTrue(web.STATE['power_clock_failed'])
        status, body = self.post_authorized('/api/jobs', {'mode': 'quick'})
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body)['msg'], web.POWER_CLOCK_FAILED_MSG)
        self.assertIn('重启', web.POWER_CLOCK_FAILED_MSG)
        self.assertNotIn(b'SECRET', body)

    def test_arm_failure_fails_closed_before_dispatch(self):
        monitor = mock.Mock()
        monitor.arm.side_effect = power.PowerClockError('SECRET')
        with mock.patch.object(web, 'DESKTOP_POWER', monitor), \
                mock.patch.object(web, 'run_benchmark') as runner:
            status, body = self.post_authorized('/api/jobs', {'mode': 'quick'})
        self.assertEqual(status, 500)
        self.assertEqual(json.loads(body)['msg'], web.POWER_CLOCK_FAILED_MSG)
        self.assertTrue(web.STATE['power_clock_failed'])
        runner.assert_not_called()
        status, _ = self.post_authorized('/api/jobs', {'mode': 'quick'})
        self.assertEqual(status, 409)

    def test_startup_clock_failure_exits_with_fixed_diagnostic(self):
        import speedbench_desktop as desktop
        error = io.StringIO()
        with mock.patch.object(desktop.power, 'native_clock',
                               side_effect=power.PowerClockError('SECRET-native')), \
                contextlib.redirect_stderr(error):
            code = desktop.main()
        self.assertEqual(code, 2)
        self.assertEqual(error.getvalue().strip(), desktop.POWER_CLOCK_STARTUP_ERROR)
        self.assertNotIn('SECRET', error.getvalue())

    # ------------------------------------------------------------------
    # ordinary browser backend
    # ------------------------------------------------------------------
    def test_ordinary_web_backend_has_no_monitor_or_thread(self):
        self.assertIsNone(web.DESKTOP_POWER)
        self.assertIsNone(self.reserve())
        self.assertFalse(any(t.name == 'speedbench-power' for t in threading.enumerate()))

    # ------------------------------------------------------------------
    # metric contract
    # ------------------------------------------------------------------
    def test_power_counters_survive_the_safe_metric_contract(self):
        metrics = safe_metrics({'cleanup': {'counters': {
            'system_resumes': 1, 'power_clock_errors': 2, 'bogus': 9, 'api_key': 'CANARY'}}})
        self.assertEqual(metrics['cleanup']['counters'],
                         {'system_resumes': 1, 'power_clock_errors': 2})
        self.assertEqual(safe_counters({'system_resumes': 1, 'power_clock_errors': 2}),
                         {'system_resumes': 1, 'power_clock_errors': 2})
        self.assertEqual(safe_counters({'system_resumes': True}), {})

    def test_monitor_stop_joins_owned_thread(self):
        clock = FakeClock()
        monitor = web.PowerMonitor(clock, interval=0.01)
        monitor.start()
        self.assertTrue(monitor._thread.is_alive())
        monitor.stop()
        monitor.join(timeout=2)
        self.assertFalse(monitor._thread.is_alive())


if __name__ == '__main__':
    unittest.main()
