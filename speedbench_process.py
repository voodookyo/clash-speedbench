"""Owned, interruptible external commands; never enumerates or kills by name."""
import subprocess
import time


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
