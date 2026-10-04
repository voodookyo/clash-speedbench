"""Owned, interruptible external commands; never enumerates or kills by name."""
import subprocess
import time
import socket
import threading
import io
import select
import ssl
import errno
import json
import sys
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


def _remaining(deadline):
    remaining=None if deadline is None else deadline-time.monotonic()
    if remaining is not None and remaining<=0:
        raise socket.timeout('Request I/O timed out')
    return remaining


def _socket_io(sock,operation,cancel,timeout,deadline=None):
    if deadline is None and timeout is not None:deadline=time.monotonic()+timeout
    while True:
        if cancel():raise KeyboardInterrupt
        remaining=_remaining(deadline)
        try:return operation()
        except (BlockingIOError,ssl.SSLWantReadError):readable=True
        except ssl.SSLWantWriteError:readable=False
        remaining=_remaining(deadline)
        # Keep buffered HTTP/TLS data intact: retry only this nonblocking raw
        # operation, never restart a header/body read or an HTTP request.
        select.select([sock] if readable else [],[] if readable else [sock],[],
            .05 if remaining is None else min(.05,remaining))


def connect_socket(sock,address,cancel,deadline):
    """Connect an already-owned socket, without a blocking connect call."""
    if cancel():raise KeyboardInterrupt
    _remaining(deadline)
    sock.setblocking(False)
    error=sock.connect_ex(address)
    pending={errno.EINPROGRESS,errno.EWOULDBLOCK,errno.EALREADY,errno.EINTR,
             10035,10036,10037}  # Winsock nonblocking connect states.
    while error not in (0,errno.EISCONN,10056):
        if error not in pending:raise OSError(error,'Connection failed')
        if cancel():raise KeyboardInterrupt
        remaining=_remaining(deadline)
        _,writable,exceptional=select.select([],[sock],[sock],
            .05 if remaining is None else min(.05,remaining))
        if writable or exceptional:error=sock.getsockopt(socket.SOL_SOCKET,socket.SO_ERROR)
    if cancel():raise KeyboardInterrupt
    sock.settimeout(_remaining(deadline))


_RESOLVE_SCRIPT='''import json,socket,sys
host,port=json.load(sys.stdin)
try:
    print(json.dumps(socket.getaddrinfo(host,port,0,socket.SOCK_STREAM)))
except socket.gaierror as error:
    print(json.dumps(error.errno));sys.exit(2)
'''


def _resolve_addresses(host,port,cancel,deadline):
    if cancel():raise KeyboardInterrupt
    # Numeric controller addresses need no resolver process. Other names use
    # the same system resolver in an owned child: getaddrinfo has no portable
    # timeout/cancellation API, and an abandoned resolver thread is unsafe.
    try:return socket.getaddrinfo(host,port,0,socket.SOCK_STREAM,0,socket.AI_NUMERICHOST)
    except socket.gaierror:pass
    try:
        result=run_cancellable([sys.executable,'-I','-c',_RESOLVE_SCRIPT],cancel=cancel,
            timeout=_remaining(deadline),input=json.dumps([host,port]).encode('utf-8'),
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            **({'creationflags':subprocess.CREATE_NO_WINDOW} if sys.platform=='win32' else {}))
    except subprocess.TimeoutExpired:
        raise socket.timeout('Name resolution timed out') from None
    try:
        value=json.loads(result.stdout)
        if result.returncode:raise socket.gaierror(value if isinstance(value,int) else socket.EAI_FAIL,'Name resolution failed')
        return [(family,kind,protocol,name,tuple(address))
                for family,kind,protocol,name,address in value]
    except (ValueError,TypeError):
        raise socket.gaierror(socket.EAI_FAIL,'Name resolution failed') from None


def _create_connection(address,timeout,source_address,cancel,deadline):
    error=None
    for family,kind,protocol,_,target in _resolve_addresses(*address,cancel,deadline):
        if cancel():raise KeyboardInterrupt
        _remaining(deadline)
        sock=socket.socket(family,kind,protocol)
        try:
            if source_address:sock.bind(source_address)
            connect_socket(sock,target,cancel,deadline)
            return sock
        except BaseException as exc:
            sock.close()
            if not isinstance(exc,OSError) or isinstance(exc,socket.timeout):raise
            error=exc
    if error is not None:raise error
    raise OSError('Name resolution returned no addresses')


class _ScopedTLSContext:
    def __init__(self,context,cancel,deadline):
        self.context,self.cancel,self.deadline=context,cancel,deadline
    def wrap_socket(self,sock,**kwargs):
        # Delegate to the original verified context: SNI, trust store,
        # hostname checking and ALPN stay intact. Only the handshake waits
        # become nonblocking; no HTTP request is retried.
        if self.cancel():raise KeyboardInterrupt
        _remaining(self.deadline)
        kwargs['do_handshake_on_connect']=False
        wrapped=self.context.wrap_socket(sock,**kwargs)
        try:
            wrapped.setblocking(False)
            _socket_io(wrapped,wrapped.do_handshake,self.cancel,None,self.deadline)
            wrapped.settimeout(_remaining(self.deadline))
            return wrapped
        except BaseException:
            wrapped.close();raise


class _SocketReader(io.RawIOBase):
    def __init__(self,sock,cancel,timeout,deadline=None):
        super().__init__();self.sock,self.cancel,self.timeout=sock,cancel,timeout;self.deadline=deadline
    def readable(self):return True
    def readinto(self,buffer):
        if self.closed:raise ValueError('I/O on closed request reader')
        return _socket_io(self.sock,lambda:self.sock.recv_into(buffer),self.cancel,self.timeout,self.deadline)
    def close(self):
        if self.sock is not None:self.sock.close();self.sock=None
        super().close()


class _PollingSocket:
    """One HTTP request's socket; makefile transfers closure to its reader."""
    def __init__(self,sock,cancel,deadline=None):
        self.sock,self.cancel=sock,cancel;self.timeout=sock.gettimeout();self.deadline=deadline
        sock.setblocking(False)
    def sendall(self,data):
        view=memoryview(data);deadline=self.deadline
        if deadline is None and self.timeout is not None:deadline=time.monotonic()+self.timeout
        while view:
            remaining=_remaining(deadline)
            def send():
                try:return self.sock.send(view)
                except BlockingIOError:raise ssl.SSLWantWriteError()
            sent=_socket_io(self.sock,send,self.cancel,remaining,deadline)
            if not sent:raise OSError('Controller socket closed during write')
            view=view[sent:]
    def makefile(self,mode,buffering=None):
        if mode!='rb' or self.sock is None:raise ValueError('Invalid request reader')
        reader=io.BufferedReader(_SocketReader(self.sock,self.cancel,self.timeout,self.deadline))
        self.sock=None
        return reader
    def close(self):
        if self.sock is not None:self.sock.close();self.sock=None


class SocketCancellation:
    """Cancellable raw socket I/O without detached threads or lost buffers.

    Python's timeout/select on Windows may not wake on another thread's
    shutdown. Poll the owned nonblocking socket instead. Scoped standard
    HTTP(S) shares one timeout across owned DNS/connect/TLS/read/write; DNS
    uses a reaped child, never a detached resolver thread. Named pipes have
    a separate scoped I/O adapter. Ordinary requests outside a scope are
    unchanged. OS process reaping and disk I/O are not hard real-time bounds.
    """
    def __init__(self,connection,cancel):
        self.connection,self.cancel=connection,cancel
        self.cancelled=False;self.original_connect=None
        self.original_create=None;self.original_context=None;self.deadline=None

    def check(self):
        if self.cancel is not None and (self.cancelled or self.cancel()):
            self.cancelled=True;raise KeyboardInterrupt

    def __enter__(self):
        self.check()
        if self.cancel is None:return self
        timeout=self.connection.timeout
        if timeout is socket._GLOBAL_DEFAULT_TIMEOUT:timeout=socket.getdefaulttimeout()
        self.deadline=None if timeout is None else time.monotonic()+timeout
        # Custom Unix/pipe connections keep their transport contract. Only
        # the standard TCP creator is replaced; restoration is instance-local.
        if getattr(self.connection,'_create_connection',None) is socket.create_connection:
            self.original_create=self.connection._create_connection
            self.connection._create_connection=lambda address,timeout,source_address: \
                _create_connection(address,timeout,source_address,self.cancel,self.deadline)
        if hasattr(self.connection,'_context'):
            self.original_context=self.connection._context
            self.connection._context=_ScopedTLSContext(self.original_context,self.cancel,self.deadline)
        self.original_connect=self.connection.connect
        def connect():
            self.original_connect()
            if isinstance(self.connection.sock,socket.socket):
                self.connection.sock=_PollingSocket(self.connection.sock,self.cancel,self.deadline)
            self.check()
        self.connection.connect=connect
        return self

    def __exit__(self,kind,value,traceback):
        if self.original_connect is not None:self.connection.connect=self.original_connect
        if self.original_create is not None:self.connection._create_connection=self.original_create
        if self.original_context is not None:self.connection._context=self.original_context


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
