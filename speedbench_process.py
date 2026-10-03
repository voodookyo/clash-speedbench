"""Owned, interruptible external commands; never enumerates or kills by name."""
import subprocess
import time
import socket
import threading
import io
import select
import ssl
from contextlib import contextmanager


_CANCELLATION=threading.local()


def current_cancellation():
    """Capture this thread's nested request scopes, never a global task flag."""
    callbacks=tuple(getattr(_CANCELLATION,'callbacks',()))
    if not callbacks:return None
    return lambda:any(callback() for callback in callbacks)


@contextmanager
def cancellation_scope(cancel):
    previous=getattr(_CANCELLATION,'callbacks',())
    _CANCELLATION.callbacks=previous+((cancel,) if cancel is not None else ())
    try:yield
    finally:_CANCELLATION.callbacks=previous


def _socket_io(sock,operation,cancel,timeout):
    deadline=None if timeout is None else time.monotonic()+timeout
    while True:
        if cancel():raise KeyboardInterrupt
        try:return operation()
        except (BlockingIOError,ssl.SSLWantReadError):readable=True
        except ssl.SSLWantWriteError:readable=False
        remaining=None if deadline is None else deadline-time.monotonic()
        if remaining is not None and remaining<=0:raise socket.timeout('Controller I/O timed out')
        # Keep buffered HTTP/TLS data intact: retry only this nonblocking raw
        # operation, never restart a header/body read or an HTTP request.
        select.select([sock] if readable else [],[] if readable else [sock],[],
            .05 if remaining is None else min(.05,remaining))


class _SocketReader(io.RawIOBase):
    def __init__(self,sock,cancel,timeout):
        super().__init__();self.sock,self.cancel,self.timeout=sock,cancel,timeout
    def readable(self):return True
    def readinto(self,buffer):
        if self.closed:raise ValueError('I/O on closed request reader')
        return _socket_io(self.sock,lambda:self.sock.recv_into(buffer),self.cancel,self.timeout)
    def close(self):
        if self.sock is not None:self.sock.close();self.sock=None
        super().close()


class _PollingSocket:
    """One HTTP request's socket; makefile transfers closure to its reader."""
    def __init__(self,sock,cancel):
        self.sock,self.cancel=sock,cancel;self.timeout=sock.gettimeout()
        sock.setblocking(False)
    def sendall(self,data):
        view=memoryview(data);deadline=None if self.timeout is None else time.monotonic()+self.timeout
        while view:
            remaining=None if deadline is None else max(0,deadline-time.monotonic())
            def send():
                try:return self.sock.send(view)
                except BlockingIOError:raise ssl.SSLWantWriteError()
            sent=_socket_io(self.sock,send,self.cancel,remaining)
            if not sent:raise OSError('Controller socket closed during write')
            view=view[sent:]
    def makefile(self,mode,buffering=None):
        if mode!='rb' or self.sock is None:raise ValueError('Invalid request reader')
        reader=io.BufferedReader(_SocketReader(self.sock,self.cancel,self.timeout))
        self.sock=None
        return reader
    def close(self):
        if self.sock is not None:self.sock.close();self.sock=None


class SocketCancellation:
    """Cancellable raw socket I/O without detached threads or lost buffers.

    Python's timeout/select on Windows may not wake on another thread's
    shutdown. Poll the owned nonblocking socket instead. Establishment/TLS
    retains its native timeout: not a universal hard request deadline. Named
    pipes have a separate scoped I/O adapter. Ordinary requests outside a
    scope are unchanged.
    """
    def __init__(self,connection,cancel):
        self.connection,self.cancel=connection,cancel
        self.cancelled=False;self.original_connect=None

    def check(self):
        if self.cancel is not None and (self.cancelled or self.cancel()):
            self.cancelled=True;raise KeyboardInterrupt

    def __enter__(self):
        self.check()
        if self.cancel is None:return self
        self.original_connect=self.connection.connect
        def connect():
            self.original_connect()
            if isinstance(self.connection.sock,socket.socket):
                self.connection.sock=_PollingSocket(self.connection.sock,self.cancel)
            self.check()
        self.connection.connect=connect
        return self

    def __exit__(self,kind,value,traceback):
        if self.original_connect is not None:self.connection.connect=self.original_connect


def run_cancellable(cmd, *, cancel=None, timeout=None, **kwargs):
    # Preserve the old subprocess.run interface when no cancellation channel
    # exists (CLI and injected test doubles). No new process group is needed
    # for curl: only the process object just created belongs to this call.
    if cancel is None:
        if timeout is not None:
            kwargs['timeout'] = timeout
        return subprocess.run(cmd,**kwargs)
    if cancel():
        raise KeyboardInterrupt
    check = kwargs.pop('check',False)
    input_value = kwargs.pop('input',None)
    if input_value is not None:
        kwargs['stdin'] = subprocess.PIPE
    if kwargs.pop('capture_output',False):
        if 'stdout' in kwargs or 'stderr' in kwargs:
            raise ValueError('capture_output conflicts with output handles')
        kwargs['stdout'] = subprocess.PIPE
        kwargs['stderr'] = subprocess.PIPE
    deadline = None if timeout is None else time.monotonic()+timeout
    proc = subprocess.Popen(cmd,**kwargs)
    try:
        first = True
        while True:
            if cancel():
                raise KeyboardInterrupt
            remaining = None if deadline is None else deadline-time.monotonic()
            if remaining is not None and remaining <= 0:
                raise subprocess.TimeoutExpired(cmd,timeout)
            try:
                stdout,stderr = proc.communicate(input=input_value if first else None,
                                                timeout=min(.1,remaining) if remaining is not None else .1)
                break
            except subprocess.TimeoutExpired:
                first = False
        result = subprocess.CompletedProcess(cmd,proc.returncode,stdout,stderr)
        if check:
            result.check_returncode()
        return result
    finally:
        if proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                if proc.poll() is None:
                    raise
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=1)
        # communicate drains any pipes after termination; the original error
        # (including KeyboardInterrupt) remains the outcome of the call.
        # On Windows communicate uses reader threads. Rejoin/drain them after
        # the owned child exits before closing their handles.
        proc.communicate()
        for pipe in (proc.stdin,proc.stdout,proc.stderr):
            if pipe is not None:
                pipe.close()
