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
    try:
        frame=sys.stdin.buffer.readline(4097)
        if not frame.endswith(b'\n') or len(frame)>4096:raise DesktopError('Bootstrap frame too large')
        bootstrap=validate_bootstrap(json.loads(frame))
        import speedbench_web as web
        with BackendLease(web.DATA_HOME) as lease:
            server=ThreadingHTTPServer(('127.0.0.1',0),web.Handler)
            server.daemon_threads=True
            web.DESKTOP_IDENTITY=public_identity(lease.instance_id)
            try:preferences=web.Preferences(web.DATA_HOME).read()
            except web.PreferenceError:preferences={}
            web.DESKTOP_ACTIONS=DesktopActions(preferences.get('sb_notifications')=='on')
            web.sync_db();web.speedbench_db.interrupt_tasks(web.db_path())
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
                    exiting.set();busy=web.STATE['running']
                if busy:web.cancel_benchmark()
                deadline=time.monotonic()+25
                while time.monotonic()<deadline:
                    with web.STATE_LOCK:busy=web.STATE['running']
                    if not busy:break
                    time.sleep(.1)
                # A failed cleanup is not presented as success. The parent has
                # an independent owned-process-tree timeout and final wait.
                server.shutdown()
            web.DESKTOP_SHUTDOWN=shutdown
            def controls():
                try:
                    while True:
                        command=read_control(sys.stdin.buffer)
                        if command=='cancel':web.cancel_benchmark()
                        elif command is None or command=='exit':return
                        elif isinstance(command,dict):web.DESKTOP_ACTIONS.finish(command['request_id'],command['ok'])
                except DesktopError:
                    pass # Fail closed without dumping a private input frame.
                finally:
                    shutdown() # Parent EOF/crash/bad transport: release ownership.
            threading.Thread(target=controls,daemon=True).start()
            try:server.serve_forever(poll_interval=.1)
            finally:server.server_close()
            with web.STATE_LOCK:busy=web.STATE['running']
            return 2 if busy else 0
    except Exception:
        # Never dump arbitrary input, nonce, env, credentials or exception URL.
        print('Desktop backend startup or ownership failed',file=sys.stderr)
        return 2


if __name__=='__main__':raise SystemExit(main())
