"""One backend per data directory using a kernel-held lease (stdlib only).

The file is never deleted. Stale PID metadata does not confer ownership and
cannot block a restart; this avoids lock-file replacement races/PID reuse.
"""
import json
import os
from pathlib import Path
import secrets
import stat
from datetime import datetime, timezone
from speedbench_identity import _secure_new_file, _windows_private, _windows_sid, IdentityError


class LeaseError(RuntimeError):
    pass


class BackendLease:
    def __init__(self, directory):
        self.path=Path(directory).resolve()/'backend-owner.lock'
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

    def __enter__(self):return self.acquire()
    def __exit__(self,*_):self.close()
