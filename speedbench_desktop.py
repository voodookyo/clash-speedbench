"""Private Tauri backend bootstrap/control pipe. No third-party packages.

Never run as an ordinary CLI server: stdin/stdout are an inherited private
parent pipe, not logs. The nonce and write token are absent from argv/URLs.
"""
import json
import os
import re
import sys
import threading
import time
import secrets
from collections import deque
from http.server import ThreadingHTTPServer

from speedbench_owner import BackendLease, LeaseError
import speedbench_power as power

# Fixed startup diagnostic: never include native detail, paths or clock values.
POWER_CLOCK_STARTUP_ERROR='Desktop suspend/resume clock unavailable; refusing to run unmonitored'

APP_ID='com.voodookyo.clash-speedbench'
VERSION='1.1.0-alpha.1'
PROTOCOL=1


class DesktopError(ValueError):pass


class DesktopActions:
    """Bounded, explicit OS requests; never accept a path, URL or command."""
    ALLOWED=('browserleaks_dns','dnsleaktest','browser_audit','releases')

    def __init__(self,notifications=False):
        self.lock=threading.RLock();self.pending=deque();self.records={};self.notifications=notifications

    def request(self,body):
        if not isinstance(body,dict) or set(body)!={'action'} or body['action'] not in self.ALLOWED:
            raise DesktopError('Unsupported desktop action')
        with self.lock:
            if len(self.pending)>=8:raise DesktopError('Desktop action queue is full')
            key=secrets.token_hex(16)
            self.records[key]={'request_id':key,'status':'queued','created':time.monotonic()}
            self.pending.append({'request_id':key,'action':body['action']})
            while len(self.records)>32:self.records.pop(next(iter(self.records)))
            return key

    def drain(self):
        with self.lock:
            actions=[]
            while self.pending:
                action=self.pending.popleft();record=self.records.get(action['request_id'])
                if record and time.monotonic()-record['created']<=30:actions.append(action)
                elif record:record['status']='expired'
            return actions

    def finish(self,key,ok):
        with self.lock:
            if key in self.records:self.records[key]['status']='opened' if ok else 'failed'

    def result(self,key):
        with self.lock:
            record=self.records.get(key)
            if record and record['status']=='queued' and time.monotonic()-record['created']>30:
                record['status']='expired'
            return {'request_id':key,'status':record['status']} if record else None


def validate_bootstrap(value):
    if not isinstance(value,dict) or set(value)!= {'protocol','nonce','parent_pid'}:
        raise DesktopError('Invalid private bootstrap')
    if type(value['protocol']) is not int or value['protocol']!=PROTOCOL:
        raise DesktopError('Unsupported desktop protocol')
    if type(value['parent_pid']) is not int or value['parent_pid']!=os.getppid():
        raise DesktopError('Bootstrap parent identity mismatch')
    if not isinstance(value['nonce'],str) or not re.fullmatch(r'[0-9a-f]{64}',value['nonce']):
        raise DesktopError('Invalid startup challenge')
    return value


def public_identity(instance_id):
    return dict(app_id=APP_ID,version=VERSION,protocol=PROTOCOL,instance_id=instance_id,pid=os.getpid())


class UnbufferedControlReader:
    """Bounded line reader over the inherited private control descriptor.

    The daemon control thread waits on a private stdin pipe that the parent
    normally keeps open for the whole session. A ``BufferedReader.readline``
    blocked there still holds the buffered-reader lock, so when the backend
    exits (for example on authenticated ``POST /api/quit``) interpreter
    finalization aborts with ``Fatal Python error: _enter_buffered_busy``.
    Reading the raw descriptor never takes that lock, so the parent staying
    connected no longer prevents a clean finalization. Each call is bounded by
    the readline limit and the pending buffer is bounded the same way.
    """
    def __init__(self,fd,read_size=4096):
        self._fd=fd;self._read_size=read_size;self._pending=bytearray()

    def readline(self,limit=-1):
        limit=257 if (limit is None or limit<0) else limit
        while True:
            newline=self._pending.find(b'\n')
            if 0<=newline<limit:end=newline+1
            elif len(self._pending)>=limit:end=limit
            else:end=None
            if end is not None:
                line=bytes(self._pending[:end]);del self._pending[:end];return line
            try:
                chunk=os.read(self._fd,self._read_size)
            except InterruptedError:
                continue
            if not chunk:
                line=bytes(self._pending);self._pending.clear();return line
            self._pending.extend(chunk)


def control_reader():
    """Unbuffered reader for the current private stdin descriptor."""
    stream=sys.stdin
    if sys.platform=='win32':
        # An inherited Windows std handle may still translate CRLF at the CRT
        # layer; force binary so a bounded frame is never silently rewritten.
        import msvcrt
        try:msvcrt.setmode(stream.fileno(),os.O_BINARY)
        except OSError:pass
    return UnbufferedControlReader(stream.fileno())


def read_control(stream):
    """Read one bounded private frame; a malformed transport fails closed."""
    raw=stream.readline(257)
    if not raw:return None
    if len(raw)>256 or not raw.endswith(b'\n'):
        raise DesktopError('Invalid private control frame')
    try:command=json.loads(raw)
    except (ValueError,UnicodeError):return 'ignore'
    if command=={'command':'cancel'}:return 'cancel'
    if command=={'command':'exit'}:return 'exit'
    if (isinstance(command,dict) and set(command)=={'command','request_id','ok'}
        and command['command']=='action_result' and type(command['ok']) is bool
        and isinstance(command['request_id'],str) and re.fullmatch(r'[0-9a-f]{32}',command['request_id'])):
        return command
    return 'ignore'


def main():
    # Explicit isolated resource/data paths come from the parent environment,
    # not a URL or renderer-controlled shell command.
    web=None
    monitor=None
    server=None
    # Initialize and validate the native suspend/resume clock before the
    # private handshake. A missing or broken API fails closed here rather than
    # silently falling back to a wall clock.
    try:
        clock=power.native_clock()
        power.validate_sample(clock())
    except power.PowerClockError:
        print(POWER_CLOCK_STARTUP_ERROR,file=sys.stderr)
        return 2
    stage='bootstrap'
    try:
        control=control_reader()
        frame=control.readline(4097)
        if not frame.endswith(b'\n') or len(frame)>4096:raise DesktopError('Bootstrap frame too large')
        bootstrap=validate_bootstrap(json.loads(frame))
        stage='imports'
        import speedbench_web as web
        monitor=web.PowerMonitor(clock)
        stage='lease'
        with BackendLease(web.DATA_HOME) as lease:
            web.DATA_OWNER=lease
            try:
                stage='history_recovery'
                web.recover_history_import()
                stage='loopback_bind'
                server=ThreadingHTTPServer(('127.0.0.1',0),web.Handler)
                server.daemon_threads=True
                web.DESKTOP_IDENTITY=public_identity(lease.instance_id)
                try:preferences=web.Preferences(web.DATA_HOME).read()
                except web.PreferenceError:preferences={}
                web.DESKTOP_ACTIONS=DesktopActions(preferences.get('sb_notifications')=='on')
                stage='database'
                web.sync_db();web.speedbench_db.interrupt_tasks(web.db_path())
                web.DESKTOP_POWER=monitor
                stage='power_monitor'
                monitor.start()
                response={**web.DESKTOP_IDENTITY,'port':server.server_port,
                          'nonce':bootstrap['nonce'],'token':web.WEB_TOKEN}
                sys.stdout.write(json.dumps(response)+'\n');sys.stdout.flush()
                # No further structured or human output can accidentally disclose
                # the private frame on stdout. Rust deliberately never logs it.
                sys.stdout=sys.stderr
                exiting=threading.Event()
                web.DESKTOP_EXITING=exiting
                shutdown_lock=threading.Lock()
                shutdown_started=threading.Event()
                def shutdown():
                    with shutdown_lock:
                        if shutdown_started.is_set():return
                        shutdown_started.set()
                    with web.STATE_LOCK:
                        exiting.set();busy=web.STATE['running'] or web.STATE.get('importing')
                    if busy:web.cancel_benchmark()
                    deadline=time.monotonic()+25
                    while time.monotonic()<deadline:
                        with web.STATE_LOCK:busy=web.STATE['running'] or web.STATE.get('importing')
                        if not busy:break
                        time.sleep(.1)
                    # A failed cleanup is not presented as success. The parent has
                    # an independent owned-process-tree timeout and final wait.
                    server.shutdown()
                web.DESKTOP_SHUTDOWN=shutdown
                # Reuse the bootstrap reader so any already-read control bytes
                # remain owned by this same bounded, unbuffered transport.
                def controls():
                    try:
                        while True:
                            command=read_control(control)
                            if command=='cancel':web.cancel_benchmark()
                            elif command is None or command=='exit':return
                            elif isinstance(command,dict):web.DESKTOP_ACTIONS.finish(command['request_id'],command['ok'])
                    except DesktopError:
                        pass # Fail closed without dumping a private input frame.
                    finally:
                        shutdown() # Parent EOF/crash/bad transport: release ownership.
                threading.Thread(target=controls,daemon=True).start()
                server.serve_forever(poll_interval=.1)
                # Match the shutdown wait condition: an import still running at
                # the deadline, or a failed import left pending, is not a clean exit.
                with web.STATE_LOCK:
                    busy=(web.STATE['running'] or web.STATE.get('importing')
                          or web.STATE.get('import_failed'))
                return 2 if busy else 0
            finally:
                # Stop and join the owned watcher before the data-owner lease is
                # released; the thread is bounded and never auto-restarts.
                if monitor is not None:
                    monitor.stop();monitor.join()
                web.DESKTOP_POWER=None
                if server is not None:server.server_close()
    except Exception:
        # Never dump arbitrary input, nonce, env, credentials or exception URL.
        print('Desktop backend startup or ownership failed: '+stage,file=sys.stderr)
        return 2
    finally:
        if web is not None:web.DATA_OWNER=None


if __name__=='__main__':raise SystemExit(main())
