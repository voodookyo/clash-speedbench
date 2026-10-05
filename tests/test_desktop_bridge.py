import contextlib
import io
import os
import json
import sys
import time
import tempfile
import subprocess
import http.client
from pathlib import Path
from unittest import mock
import unittest
import threading
import queue
import speedbench_web as web
from tests.web_server_case import WebServerCase

import speedbench_desktop as desktop


class DesktopBridgeTest(unittest.TestCase):
    def test_explicit_os_actions_are_bounded_aged_and_acknowledged(self):
        actions=desktop.DesktopActions()
        for bad in ({'action':'https://evil.example'},{'action':'releases','key':'CANARY'},[],{'action':'cmd.exe'}):
            with self.assertRaises(desktop.DesktopError):actions.request(bad)
        key=actions.request({'action':'browser_audit'})
        self.assertEqual(actions.result(key)['status'],'queued')
        self.assertEqual(actions.drain(),[{'action':'browser_audit','request_id':key}])
        self.assertEqual(actions.drain(),[])
        actions.finish(key,True);self.assertEqual(actions.result(key)['status'],'opened')
        for _ in range(8):actions.request({'action':'releases'})
        with self.assertRaises(desktop.DesktopError):actions.request({'action':'releases'})
        with mock.patch('speedbench_desktop.time.monotonic',return_value=10**15):
            self.assertEqual(actions.drain(),[])

    def test_control_is_bounded_and_unknown_commands_have_no_authority(self):
        self.assertEqual(desktop.read_control(io.BytesIO(b'{"command":"cancel"}\n')),'cancel')
        self.assertEqual(desktop.read_control(io.BytesIO(b'{"command":"exit"}\n')),'exit')
        self.assertIsNone(desktop.read_control(io.BytesIO()))
        acknowledged={'command':'action_result','request_id':'a'*32,'ok':False}
        self.assertEqual(desktop.read_control(io.BytesIO(json.dumps(acknowledged).encode()+b'\n')),acknowledged)
        for raw in (b'not-json\n',b'{"command":"exec","key":"CANARY"}\n',b'{"command":"exit","extra":1}\n'):
            self.assertEqual(desktop.read_control(io.BytesIO(raw)),'ignore')
        for raw in (b'x'*1_000_000,b'{"command":"exit"}'):
            stream=io.BytesIO(raw)
            with self.assertRaises(desktop.DesktopError):desktop.read_control(stream)
            self.assertLessEqual(stream.tell(),257)

    def test_unbuffered_reader_decodes_frames_and_parent_eof(self):
        read_fd,write_fd=os.pipe()
        self.addCleanup(os.close,read_fd)
        reader=desktop.UnbufferedControlReader(read_fd)
        acknowledged={'command':'action_result','request_id':'a'*32,'ok':True}
        os.write(write_fd,b'{"command":"cancel"}\n{"command":"exit"}\n'
                 +json.dumps(acknowledged).encode()+b'\nnot-json\n')
        os.close(write_fd)
        self.assertEqual(desktop.read_control(reader),'cancel')
        self.assertEqual(desktop.read_control(reader),'exit')
        self.assertEqual(desktop.read_control(reader),acknowledged)
        self.assertEqual(desktop.read_control(reader),'ignore')
        self.assertIsNone(desktop.read_control(reader))

    def test_unbuffered_reader_bounds_malformed_and_oversized_frames(self):
        for raw in (b'{"command":"exit"}',b'x'*4096+b'\n'):
            with self.subTest(size=len(raw)):
                read_fd,write_fd=os.pipe()
                try:
                    reader=desktop.UnbufferedControlReader(read_fd)
                    os.write(write_fd,raw);os.close(write_fd)
                    with self.assertRaises(desktop.DesktopError):desktop.read_control(reader)
                finally:
                    os.close(read_fd)

    def test_real_private_bootstrap_dynamic_port_guard_and_parent_eof(self):
        with tempfile.TemporaryDirectory() as folder:
            env=dict(os.environ,SPEEDBENCH_HOME=folder)
            # No inherited third-party credentials belong in a CI fixture.
            for key in list(env):
                if key.startswith('SPEEDBENCH_IP') or key.startswith('SPEEDBENCH_SCAMALYTICS'):env.pop(key)
            proc=subprocess.Popen([sys.executable,'-u','speedbench_desktop.py'],stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env,text=True,encoding='utf-8')
            try:
                proc.stdin.write(json.dumps({'protocol':1,'parent_pid':os.getpid(),'nonce':'b'*64})+'\n');proc.stdin.flush()
                # Owned process is killed/reaped on every test exit, including
                # failed readiness assertions. No real controller is queried.
                received=queue.Queue(maxsize=1)
                threading.Thread(target=lambda:received.put(proc.stdout.readline(4097)),daemon=True).start()
                frame=json.loads(received.get(timeout=10))
                self.assertEqual(frame['nonce'],'b'*64)
                self.assertGreater(frame['port'],0)
                self.assertEqual(frame['pid'],proc.pid)
                # A separate legacy HTTP port is not a second task owner.
                competing=subprocess.run([sys.executable,'speedbench_web.py','--no-browser','--port','0'],
                    env=env,capture_output=True,timeout=10)
                self.assertEqual(competing.returncode,2)
                self.assertNotIn(frame['token'].encode(),competing.stdout+competing.stderr)
                conn=http.client.HTTPConnection('127.0.0.1',frame['port'],timeout=3)
                self.addCleanup(conn.close)
                conn.request('GET','/api/desktop/identity')
                response=conn.getresponse();self.assertEqual(response.status,403);response.read()
                conn.request('GET','/api/desktop/identity',headers={'X-SpeedBench-Token':frame['token']})
                response=conn.getresponse();self.assertEqual(response.status,200);body=response.read()
                self.assertNotIn(frame['token'].encode(),body);self.assertNotIn(frame['nonce'].encode(),body)
                conn.request('GET','/',headers={'Host':'evil.example'})
                response=conn.getresponse();self.assertEqual(response.status,403);response.read()
                proc.stdin.close();proc.stdin=None
                out,err=proc.communicate(timeout=10)
                self.assertEqual(proc.returncode,0,err)
                self.assertNotIn(frame['token'],out+err)
                self.assertNotIn(frame['nonce'],out+err)
                self.assertFalse((Path(folder)/'speedbench-history.jsonl').exists())
            finally:
                if proc.poll() is None:proc.kill();proc.communicate(timeout=5)
                for stream in (proc.stdin,proc.stdout,proc.stderr):
                    if stream:stream.close()

    def test_bootstrap_and_exit_in_one_pipe_write_do_not_lose_the_control_frame(self):
        with tempfile.TemporaryDirectory() as folder:
            env=dict(os.environ,SPEEDBENCH_HOME=folder)
            for key in list(env):
                if key.startswith(('SPEEDBENCH_IP','SPEEDBENCH_SCAMALYTICS')):env.pop(key)
            proc=subprocess.Popen([sys.executable,'-u','speedbench_desktop.py'],stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env)
            try:
                bootstrap=json.dumps({'protocol':1,'parent_pid':os.getpid(),'nonce':'e'*64}).encode()+b'\n'
                proc.stdin.write(bootstrap+b'{"command":"exit"}\n');proc.stdin.flush()
                # Keep stdin open: EOF must not disguise losing the queued exit.
                self.assertEqual(proc.wait(timeout=5),0)
                self.assertEqual(json.loads(proc.stdout.read())['protocol'],1)
                self.assertNotIn(b'Fatal Python error',proc.stderr.read())
            finally:
                if proc.poll() is None:proc.kill();proc.wait(timeout=5)
                for stream in (proc.stdin,proc.stdout,proc.stderr):stream.close()

    def test_authenticated_http_quit_with_parent_stdin_open_exits_cleanly(self):
        # Regression: a daemon control thread blocked in BufferedReader.readline
        # on a still-open parent stdin held the buffered-reader lock and aborted
        # finalization (Fatal Python error _enter_buffered_busy, exit -6) after
        # an authenticated quit. Parent stdin stays open until the child exits.
        with tempfile.TemporaryDirectory() as folder:
            env=dict(os.environ,SPEEDBENCH_HOME=folder)
            for key in list(env):
                if key.startswith('SPEEDBENCH_IP') or key.startswith('SPEEDBENCH_SCAMALYTICS'):env.pop(key)
            proc=subprocess.Popen([sys.executable,'-u','speedbench_desktop.py'],stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env,text=True,encoding='utf-8')
            frame=None
            try:
                proc.stdin.write(json.dumps({'protocol':1,'parent_pid':os.getpid(),'nonce':'d'*64})+'\n')
                proc.stdin.flush()
                received=queue.Queue(maxsize=1)
                threading.Thread(target=lambda:received.put(proc.stdout.readline(4097)),daemon=True).start()
                frame=json.loads(received.get(timeout=10))
                conn=http.client.HTTPConnection('127.0.0.1',frame['port'],timeout=3)
                self.addCleanup(conn.close)
                conn.request('POST','/api/quit',json.dumps({}),headers={
                    'X-SpeedBench-Token':frame['token'],'Host':f"127.0.0.1:{frame['port']}",
                    'Origin':f"http://127.0.0.1:{frame['port']}",'Content-Length':'2'})
                response=conn.getresponse();self.assertEqual(response.status,200);response.read()
                # Parent stdin is deliberately not closed; poll the owned child.
                deadline=time.time()+15
                while time.time()<deadline and proc.poll() is None:time.sleep(.05)
                self.assertIsNotNone(proc.poll(),'backend did not exit after authenticated quit')
                out,err=proc.stdout.read(),proc.stderr.read()
            finally:
                if proc.poll() is None:proc.kill();proc.wait(timeout=5)
                if proc.stdin is not None:
                    try:proc.stdin.close()
                    except OSError:pass
                    proc.stdin=None
                for stream in (proc.stdout,proc.stderr):
                    if stream:stream.close()
            self.assertEqual(proc.returncode,0,err)
            self.assertNotIn('_enter_buffered_busy',err)
            self.assertNotIn('Fatal Python error',err)
            self.assertNotIn(frame['token'],out+err)
            self.assertNotIn(frame['nonce'],out+err)

    def test_handshake_requires_parent_nonce_and_bounded_fields(self):
        data={'protocol':1,'nonce':'a'*64,'parent_pid':os.getppid()}
        self.assertEqual(desktop.validate_bootstrap(data)['nonce'],'a'*64)
        for bad in ({**data,'parent_pid':-1},{**data,'nonce':'bad'},{**data,'protocol':2},
                    {**data,'key':'CANARY-key'},[],{**data,'parent_pid':True}):
            with self.subTest(bad=bad),self.assertRaises(desktop.DesktopError):desktop.validate_bootstrap(bad)

    def test_real_backend_reconciles_pending_history_before_bootstrap_and_db_writes(self):
        from speedbench_owner import BackendLease
        from speedbench_transfer import HistoryTransfer, PENDING
        import speedbench_transfer as transfer
        from tests.test_history_transfer import ledger
        for changed in (False,True):
            with self.subTest(changed=changed),tempfile.TemporaryDirectory() as folder:
                home=Path(folder)/'中文 home';source=Path(folder)/'old';home.mkdir();source.mkdir()
                old=ledger(home,'2026-10-01T01:00:00',whitespace=True)
                ledger(source,'2026-10-02T01:00:00')
                service=HistoryTransfer(home);original=transfer._atomic
                def crash(path,data):
                    if path==home.resolve()/'speedbench-history.db':raise SystemExit('fixture crash')
                    return original(path,data)
                with BackendLease(home) as owner:
                    token=service.preview(str(source),owner)['token']
                    with mock.patch.object(transfer,'_atomic',side_effect=crash),self.assertRaises(SystemExit):
                        service.apply(token,owner)
                if changed:ledger(home,'2026-10-03T01:00:00')
                before=(home/'speedbench-history.jsonl').read_bytes()
                env=dict(os.environ,SPEEDBENCH_HOME=str(home))
                frame=json.dumps({'protocol':1,'parent_pid':os.getpid(),'nonce':'b'*64})+'\n'
                result=subprocess.run([sys.executable,'-u','speedbench_desktop.py'],input=frame,
                    capture_output=True,env=env,text=True,encoding='utf-8',timeout=10)
                self.assertEqual(result.returncode,2 if changed else 0,result.stderr)
                if changed:
                    self.assertEqual(result.stdout,'')
                    self.assertEqual((home/'speedbench-history.jsonl').read_bytes(),before)
                    self.assertTrue((home/PENDING).exists())
                    self.assertFalse((home/'speedbench-history.db').exists())
                else:
                    self.assertEqual(json.loads(result.stdout)['protocol'],1)
                    self.assertEqual((home/'speedbench-history.jsonl').read_text().rstrip('\n'),old)
                    self.assertFalse((home/PENDING).exists())

    def test_private_pipe_frame_is_not_a_public_identity_response(self):
        identity=desktop.public_identity('fixture')
        self.assertEqual(identity['app_id'],'com.voodookyo.clash-speedbench')
        self.assertNotIn('nonce',identity)
        self.assertNotIn('token',identity)
        self.assertNotIn('key',str(identity))


class _FakeParentStdin:
    """Real anonymous-pipe stdin: one buffered bootstrap frame, then EOF.

    ``fileno`` exposes the descriptor to the bounded unbuffered reader used
    for bootstrap and controls. Main exercises the production descriptor path
    instead of a mocked readline.
    """
    def __init__(self,frame):
        read_fd,write_fd=os.pipe()
        self.buffer=os.fdopen(read_fd,'rb')
        os.write(write_fd,frame);os.close(write_fd)
    def fileno(self):return self.buffer.fileno()
    def close(self):
        try:self.buffer.close()
        except OSError:pass


class _FakeHandshakeStdout:
    def __init__(self):self.text=''
    def write(self,text):self.text+=text
    def flush(self):pass


class _FakeBackendServer:
    def __init__(self,address,handler):
        self.server_port=1;self.daemon_threads=False;self._done=threading.Event()
    def shutdown(self):self._done.set()
    def serve_forever(self,poll_interval=None):self._done.wait(10)
    def server_close(self):pass


class _FakeClock:
    """monotonic() jumps past the 25s shutdown deadline without real waiting."""
    def __init__(self,step=10.0):self._now=0.0;self.step=step;self.on_sleep=None
    def monotonic(self):
        value=self._now;self._now+=self.step;return value
    def sleep(self,_seconds):
        if self.on_sleep is not None:self.on_sleep()


class _FakeLease:
    instance_id='fixture-lease'
    def __enter__(self):return self
    def __exit__(self,*_):return False


class _RecordingLease:
    """Records lease release so monitor shutdown ordering can be asserted."""
    instance_id='fixture-lease'
    def __init__(self,events):self.events=events
    def __enter__(self):return self
    def __exit__(self,*_):self.events.append('lease_exit');return False


class _FakeMonitor:
    """Owned PowerMonitor stand-in: no thread, records start/stop/join order."""
    def __init__(self,clock,events,interval=None):
        self.clock=clock;self.events=events
    def start(self):self.events.append('start')
    def stop(self):self.events.append('stop')
    def join(self,timeout=None):self.events.append('join')
    def arm(self,job_id):self.events.append('arm')
    def disarm(self,job_id):self.events.append('disarm')
    def poll(self):return None


class DesktopExitCodeTest(unittest.TestCase):
    """The production main()/shutdown() path, driven over a fake private pipe,
    fake clock and fake loopback server; no real controller or data home."""
    def run_main(self,state,clock,monitor_factory=None,lease_factory=None,recover=None,server_factory=None):
        frame=json.dumps({'protocol':1,'parent_pid':os.getppid(),'nonce':'c'*64}).encode()+b'\n'
        handshake=_FakeHandshakeStdout()
        cancelled=[]
        names=('DATA_HOME','DATA_OWNER','DESKTOP_IDENTITY','DESKTOP_ACTIONS',
               'DESKTOP_SHUTDOWN','DESKTOP_EXITING','DESKTOP_POWER')
        snapshot={name:getattr(web,name) for name in names}
        with web.STATE_LOCK:
            state_snapshot=dict(web.STATE)
            web.STATE.clear();web.STATE.update(state)
        saved_stdin,saved_stdout=sys.stdin,sys.stdout
        stdin=_FakeParentStdin(frame)
        sys.stdin=stdin;sys.stdout=handshake
        try:
            with tempfile.TemporaryDirectory() as folder,contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(
                    desktop,'BackendLease',lease_factory or (lambda home:_FakeLease())))
                stack.enter_context(mock.patch.object(desktop,'ThreadingHTTPServer',server_factory or _FakeBackendServer))
                stack.enter_context(mock.patch.object(desktop,'time',clock))
                # A deterministic native clock keeps lifecycle tests portable.
                stack.enter_context(mock.patch.object(
                    desktop.power,'native_clock',lambda:(lambda:(0,0))))
                stack.enter_context(mock.patch.object(web,'DATA_HOME',Path(folder)))
                if isinstance(recover,BaseException):
                    stack.enter_context(mock.patch.object(
                        web,'recover_history_import',side_effect=recover))
                elif recover is not None:
                    stack.enter_context(mock.patch.object(web,'recover_history_import',recover))
                else:
                    stack.enter_context(mock.patch.object(web,'recover_history_import',lambda:False))
                stack.enter_context(mock.patch.object(web,'sync_db',lambda:0))
                stack.enter_context(mock.patch.object(web,'cancel_benchmark',lambda:cancelled.append(True)))
                stack.enter_context(mock.patch.object(web.speedbench_db,'interrupt_tasks',lambda path:None))
                if monitor_factory is not None:
                    stack.enter_context(mock.patch.object(web,'PowerMonitor',monitor_factory))
                code=desktop.main()
            return code,handshake.text,cancelled
        finally:
            sys.stdin,sys.stdout=saved_stdin,saved_stdout
            stdin.close()
            with web.STATE_LOCK:
                web.STATE.clear();web.STATE.update(state_snapshot)
            for name,value in snapshot.items():setattr(web,name,value)

    def test_import_still_running_at_deadline_exits_nonzero(self):
        code,handshake,cancelled=self.run_main({'running':False,'importing':True},_FakeClock())
        self.assertEqual(json.loads(handshake)['nonce'],'c'*64)
        self.assertTrue(cancelled)
        self.assertEqual(code,2)

    def test_leftover_failed_import_exits_nonzero(self):
        code,handshake,_=self.run_main({'running':False,'importing':False,'import_failed':True},_FakeClock())
        self.assertEqual(json.loads(handshake)['nonce'],'c'*64)
        self.assertEqual(code,2)

    def test_import_finishing_before_deadline_exits_clean(self):
        clock=_FakeClock()
        def finish_import():
            with web.STATE_LOCK:web.STATE['importing']=False
        clock.on_sleep=finish_import
        code,handshake,cancelled=self.run_main({'running':False,'importing':True},clock)
        self.assertEqual(json.loads(handshake)['nonce'],'c'*64)
        self.assertTrue(cancelled)
        self.assertEqual(code,0)

    def test_monitor_stops_and_joins_before_lease_release_on_parent_eof(self):
        events=[]
        monitor=_FakeMonitor(None,events)
        code,handshake,cancelled=self.run_main(
            {'running':False},_FakeClock(),
            monitor_factory=lambda clock,interval=None:monitor,
            lease_factory=lambda home:_RecordingLease(events))
        self.assertEqual(json.loads(handshake)['nonce'],'c'*64)
        self.assertEqual(code,0)
        self.assertIn('start',events)
        self.assertIn('stop',events)
        self.assertIn('join',events)
        self.assertIn('lease_exit',events)
        self.assertLess(events.index('stop'),events.index('lease_exit'))
        self.assertLess(events.index('join'),events.index('lease_exit'))
        self.assertIsNone(web.DESKTOP_POWER)

    def test_monitor_closed_and_globals_restored_on_startup_failure(self):
        events=[]
        monitor=_FakeMonitor(None,events)
        code,handshake,_=self.run_main(
            {'running':False},_FakeClock(),
            monitor_factory=lambda clock,interval=None:monitor,
            lease_factory=lambda home:_RecordingLease(events),
            recover=RuntimeError('canary-startup'))
        self.assertEqual(code,2)
        self.assertEqual(handshake,'')
        self.assertIn('stop',events)
        self.assertIn('join',events)
        self.assertIn('lease_exit',events)
        self.assertLess(events.index('join'),events.index('lease_exit'))
        self.assertIsNone(web.DESKTOP_POWER)
        self.assertIsNone(web.DATA_OWNER)

    def test_monitor_start_failure_closes_bound_server_before_lease_release(self):
        events=[]
        class Monitor(_FakeMonitor):
            def start(self):
                self.events.append('start')
                raise RuntimeError('fixture thread start failure')
        class Server(_FakeBackendServer):
            def server_close(self):events.append('server_close')
        monitor=Monitor(None,events)
        with contextlib.redirect_stderr(io.StringIO()):
            code,handshake,_=self.run_main(
                {'running':False},_FakeClock(),
                monitor_factory=lambda clock:monitor,
                lease_factory=lambda home:_RecordingLease(events),server_factory=Server)
        self.assertEqual(code,2)
        self.assertEqual(handshake,'')
        self.assertLess(events.index('join'),events.index('lease_exit'))
        self.assertLess(events.index('server_close'),events.index('lease_exit'))


class DesktopApiTest(WebServerCase):
    def test_compact_os_routes_require_auth_and_never_accept_urls_keys_or_paths(self):
        with mock.patch.object(web,'DESKTOP_ACTIONS',desktop.DesktopActions()):
            status,_=self.request('GET','/api/desktop/state');self.assertEqual(status,403)
            status,_=self.post_authorized('/api/desktop/actions',{'action':'releases','key':'CANARY'})
            self.assertEqual(status,400)
            status,raw=self.post_authorized('/api/desktop/actions',{'action':'dnsleaktest'})
            self.assertEqual(status,202);key=json.loads(raw)['request_id']
            status,raw=self.request('GET','/api/desktop/state',headers={'X-SpeedBench-Token':web.WEB_TOKEN})
            self.assertEqual(status,200);self.assertNotIn(b'CANARY',raw)
            self.assertIsNone(json.loads(raw)['job'])
            self.assertEqual(json.loads(raw)['actions'][0]['request_id'],key)
            self.assertNotIn('http',str(json.loads(raw)['actions']))

    def test_exit_flag_rejects_new_jobs_and_quit_uses_desktop_cleanup(self):
        event=threading.Event();event.set()
        with mock.patch.object(web,'DESKTOP_EXITING',event):
            status,_=self.post_authorized('/api/jobs',{'mode':'quick'})
            self.assertEqual(status,409);self.assertIsNone(web.JOBS.active_id())
        called=threading.Event();event.clear()
        with mock.patch.object(web,'DESKTOP_EXITING',event),mock.patch.object(web,'DESKTOP_SHUTDOWN',called.set):
            status,_=self.post_authorized('/api/quit',{})
            self.assertEqual(status,200);self.assertTrue(event.is_set());self.assertTrue(called.wait(1))
