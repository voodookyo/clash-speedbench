"""One backend per data directory using a kernel-held lease (stdlib only).

The file is never deleted. Stale PID metadata does not confer ownership and
cannot block a restart; this avoids lock-file replacement races/PID reuse.
"""
import json
import os
from pathlib import Path
import secrets
import stat
import sys
import threading
import queue
import re
from contextlib import contextmanager, ExitStack
from datetime import datetime, timezone
from speedbench_identity import _secure_new_file, _windows_private, _windows_sid, IdentityError


class LeaseError(RuntimeError):
    pass


class BackendLease:
    def __init__(self, directory, *, _task=False):
        self.path=Path(directory).resolve()/'backend-owner.lock'
        self._task=_task
        if _task:self.path=self.path.with_name('benchmark-writer.lock')
        self.stream=None
        self.instance_id=secrets.token_hex(16)

    def acquire(self):
        if self.stream is not None:return self
        descriptor=None
        try:
            self.path.parent.mkdir(parents=True,exist_ok=True)
            sid=_windows_sid() if os.name=='nt' else None
            try:
                descriptor=os.open(str(self.path),os.O_CREAT|os.O_EXCL|os.O_RDWR,0o600)
                _secure_new_file(self.path,sid)
            except FileExistsError:
                before=self.path.lstat()
                if not stat.S_ISREG(before.st_mode):raise LeaseError('Ownership file is not a regular private file')
                if os.name=='nt':_windows_private(self.path,sid)
                elif before.st_uid!=os.getuid() or before.st_mode & 0o077:
                    raise LeaseError('Ownership file permissions are unsafe')
                descriptor=os.open(str(self.path),os.O_RDWR|getattr(os,'O_NOFOLLOW',0))
                actual=os.fstat(descriptor)
                if (actual.st_ino,actual.st_dev)!=(before.st_ino,before.st_dev):
                    raise LeaseError('Ownership file changed during validation')
            stream=os.fdopen(descriptor,'r+b',buffering=0);descriptor=None
            self.stream=stream
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            if not self._task:
                # A delegated child keeps this second lease after a backend
                # crash. Probe before any migration/write; PID metadata alone
                # must not authorize a competing backend to start.
                with BackendLease(self.path.parent,_task=True):pass
            metadata=dict(app_id='com.voodookyo.clash-speedbench',protocol=1,pid=os.getpid(),
                          instance_id=self.instance_id,started_at=datetime.now(timezone.utc).isoformat())
            stream.seek(0);stream.write(b' '+json.dumps(metadata).encode('utf-8'));stream.truncate()
            return self
        except (OSError,IdentityError) as error:
            self.close()
            raise LeaseError('Data directory is in use or private ownership cannot be established; close its existing SpeedBench backend first') from None
        except Exception:
            self.close();raise
        finally:
            if descriptor is not None:os.close(descriptor)

    def close(self):
        if self.stream is not None:
            self.stream.close();self.stream=None

    def delegation(self,history):
        history=Path(history).resolve()
        if self.stream is None or self._task or history.parent!=self.path.parent:
            raise LeaseError('No active matching backend ownership for delegation')
        frame=dict(protocol=1,parent_pid=os.getpid(),instance_id=self.instance_id,history=str(history))
        data=json.dumps(frame,ensure_ascii=False).encode('utf-8')+b'\n'
        if len(data)>32768:raise LeaseError('Private benchmark bootstrap too large')
        return data

    def __enter__(self):return self.acquire()
    def __exit__(self,*_):self.close()


_parent_event=None


def parent_disconnected():
    return _parent_event is not None and _parent_event.is_set()


def delegated_active():
    return _parent_event is not None


def _private_owner_stream(path):
    before=path.lstat()
    if not stat.S_ISREG(before.st_mode):raise LeaseError('Invalid ownership file')
    if os.name=='nt':_windows_private(path,_windows_sid())
    elif before.st_uid!=os.getuid() or before.st_mode & 0o077:raise LeaseError('Unsafe ownership file')
    fd=os.open(str(path),os.O_RDWR|getattr(os,'O_NOFOLLOW',0))
    try:
        actual=os.fstat(fd)
        if (before.st_ino,before.st_dev)!=(actual.st_ino,actual.st_dev):raise LeaseError('Ownership file changed')
        stream=os.fdopen(fd,'r+b',buffering=0);fd=None
        return stream
    finally:
        if fd is not None:os.close(fd)


def _owner_held(stream):
    # Read-only kernel probe, never truncate/update a stale ownership record.
    stream.seek(0)
    try:
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
            msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            fcntl.flock(stream.fileno(),fcntl.LOCK_UN)
        return False
    except OSError as error:
        if error.errno in (11,13,35,36):return True
        raise


def _read_private_pipe(stream, *, line=False):
    # Never hold BufferedReader's Python lock in a daemon waiting on stdin.
    # A normally completed child may exit while the parent keeps stdin open;
    # buffered read() would make CPython abort at interpreter shutdown. Raw
    # descriptor reads also leave extra bytes visible to the EOF watcher.
    try:descriptor=stream.fileno()
    except (OSError,AttributeError):descriptor=None
    if descriptor is None:return stream.readline(32769) if line else stream.read(1)
    if not line:return os.read(descriptor,1)
    data=bytearray()
    while len(data)<32769:
        item=os.read(descriptor,1)
        if not item:break
        data.extend(item)
        if item==b'\n':break
    return bytes(data)


def _read_delegation(stream):
    if stream.isatty():raise LeaseError('Private benchmark pipe required')
    received=queue.Queue(maxsize=1)
    def read():
        try:received.put(_read_private_pipe(stream,line=True))
        except Exception:received.put(b'')
    threading.Thread(target=read,daemon=True).start()
    raw=received.get(timeout=5)
    if not isinstance(raw,bytes) or len(raw)>32768 or not raw.endswith(b'\n'):raise LeaseError('Invalid private benchmark frame')
    value=json.loads(raw)
    if (not isinstance(value,dict) or set(value)!={'protocol','parent_pid','instance_id','history'} or
        type(value['protocol']) is not int or value['protocol']!=1 or
        type(value['parent_pid']) is not int or value['parent_pid']!=os.getppid() or
        not isinstance(value['instance_id'],str) or not re.fullmatch('[0-9a-f]{32}',value['instance_id']) or
        not isinstance(value['history'],str)):
        raise LeaseError('Invalid private benchmark identity')
    return value


@contextmanager
def benchmark_ownership(history, *, delegated=False, stream=None):
    """Guard every CLI data write, with parent-bound private child delegation.

    Standalone CLI locks history and identity-home directories in sorted order.
    Delegated children retain a writer lease until their reporting/cleanup ends,
    preventing a restart race when the original backend dies. EOF cancels only
    the current child task; no credential/frame appears in argv or environment.
    """
    global _parent_event
    event=None
    with ExitStack() as stack:
        try:
            history=Path(history).resolve();directory=history.parent
            home=Path(os.environ['SPEEDBENCH_HOME']).resolve() if os.environ.get('SPEEDBENCH_HOME') else directory
            if not delegated:
                for root in sorted({directory,home},key=lambda p:str(p).casefold()):stack.enter_context(BackendLease(root))
            else:
                stream=stream if stream is not None else sys.stdin.buffer
                frame=_read_delegation(stream)
                if frame['history']!=str(history) or home!=directory:raise LeaseError('Mismatched private data directory')
                stack.enter_context(BackendLease(directory,_task=True))
                with _private_owner_stream(directory/'backend-owner.lock') as check:
                    check.seek(1) # byte 0 is exclusively locked by Windows owner
                    metadata=check.read(4097)
                    if len(metadata)>4096:raise LeaseError('Invalid ownership record')
                    metadata=json.loads(metadata)
                    if (metadata.get('app_id')!='com.voodookyo.clash-speedbench' or metadata.get('protocol')!=1 or
                        metadata.get('pid')!=frame['parent_pid'] or metadata.get('instance_id')!=frame['instance_id'] or
                        not _owner_held(check)):
                        raise LeaseError('Backend ownership no longer matches')
                event=threading.Event();_parent_event=event
                def watch():
                    try:_read_private_pipe(stream)
                    except Exception:pass
                    event.set() # EOF, unexpected command or broken pipe all cancel
                threading.Thread(target=watch,daemon=True).start()
        except (OSError,ValueError,IdentityError,queue.Empty,AttributeError):
            raise LeaseError('Data ownership unavailable or private benchmark delegation invalid') from None
        try:yield
        finally:
            if event is not None and _parent_event is event:_parent_event=None
