"""Scoped overlapped named-pipe I/O, with no detached threads or DLL on import.

The caller owns the pipe handle. Each operation owns its buffer, OVERLAPPED
and manual-reset event until GetOverlappedResult confirms terminal completion,
including after CancelIoEx and repeated KeyboardInterrupt during pending-I/O
cleanup. This is not a guarantee of final reporting after arbitrary signals.
"""
import ctypes
import math
import signal
import socket
import threading
import time
from contextlib import contextmanager


# wintypes uses host C long on POSIX; fixed Windows widths keep ABI tests useful.
DWORD = ctypes.c_uint32
BOOL = ctypes.c_int32
HANDLE = ctypes.c_void_p


class _Offsets(ctypes.Structure):
    _fields_ = [('Offset', DWORD), ('OffsetHigh', DWORD)]


class _Position(ctypes.Union):
    _fields_ = [('offsets', _Offsets), ('Pointer', ctypes.c_void_p)]


class OVERLAPPED(ctypes.Structure):
    _fields_ = [('Internal', ctypes.c_size_t), ('InternalHigh', ctypes.c_size_t),
                ('position', _Position), ('hEvent', HANDLE)]


def _kernel32():
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    signatures = {
        'CreateEventW': (HANDLE, [ctypes.c_void_p, BOOL, BOOL, ctypes.c_wchar_p]),
        'ReadFile': (BOOL, [HANDLE, ctypes.c_void_p, DWORD, ctypes.POINTER(DWORD), ctypes.POINTER(OVERLAPPED)]),
        'WriteFile': (BOOL, [HANDLE, ctypes.c_void_p, DWORD, ctypes.POINTER(DWORD), ctypes.POINTER(OVERLAPPED)]),
        'GetOverlappedResult': (BOOL, [HANDLE, ctypes.POINTER(OVERLAPPED), ctypes.POINTER(DWORD), BOOL]),
        'WaitForSingleObject': (DWORD, [HANDLE, DWORD]),
        'CancelIoEx': (BOOL, [HANDLE, ctypes.POINTER(OVERLAPPED)]),
        'CloseHandle': (BOOL, [HANDLE]),
    }
    for name, (result, arguments) in signatures.items():
        function = getattr(kernel, name)
        function.restype, function.argtypes = result, arguments
    return kernel


def _last_error():
    return ctypes.get_last_error()


@contextmanager
def _defer_default_sigint():
    # Python dispatches signals on the main thread. A second default Ctrl+C
    # must not unwind a pending I/O frame between cleanup bytecodes. Defer it
    # before submission; request checks initiate cancellation, then we restore
    # the handler and propagate the interrupt only after owned cleanup. Worker
    # threads and application-specific handlers are left untouched.
    previous = signal.getsignal(signal.SIGINT)
    interrupted = [False]
    active = threading.current_thread() is threading.main_thread() and previous is signal.default_int_handler
    if active:
        def defer(signum, frame):
            interrupted[0] = True
        signal.signal(signal.SIGINT, defer)
    try:
        yield lambda: interrupted[0]
    finally:
        if active:
            signal.signal(signal.SIGINT, previous)
            if interrupted[0]:
                raise KeyboardInterrupt


class PipeIO:
    def __init__(self, handle, cancel, timeout, *, kernel=None):
        self.handle, self.cancel, self.timeout = handle, cancel, timeout
        self.kernel = _kernel32() if kernel is None else kernel

    def _check(self, interrupted=lambda: False):
        if interrupted() or self.cancel():
            raise KeyboardInterrupt

    def _drain(self, overlap):
        # CancelIoEx requests cancellation; even ERROR_NOT_FOUND can race a
        # normal completion. Never cancel the whole handle or free pending I/O.
        while True:
            try:
                self.kernel.CancelIoEx(self.handle, ctypes.byref(overlap))
                break
            except KeyboardInterrupt:
                continue
        count = DWORD()
        while True:
            try:
                if self.kernel.GetOverlappedResult(self.handle, ctypes.byref(overlap), ctypes.byref(count), False):
                    return
                if _last_error() != 996:  # ERROR_IO_INCOMPLETE
                    return  # aborted, failed or normally completed
                result = self.kernel.WaitForSingleObject(overlap.hEvent, 50)
                if result not in (0, 258):
                    # A broken wait must not cause premature buffer release.
                    # Keep confirming the exact I/O, without a busy loop.
                    time.sleep(.05)
            except KeyboardInterrupt:
                continue

    def _transfer(self, buffer, size, writing, timeout):
        with _defer_default_sigint() as interrupted:
            return self._transfer_owned(buffer, size, writing, timeout, interrupted)

    def _transfer_owned(self, buffer, size, writing, timeout, interrupted):
        self._check(interrupted)
        if timeout is not None and timeout <= 0:
            raise socket.timeout('Controller pipe I/O timed out')
        deadline = None if timeout is None else time.monotonic() + timeout
        event = self.kernel.CreateEventW(None, True, False, None)
        if not event:
            raise OSError(_last_error(), 'CreateEventW controller pipe failed')
        overlap = OVERLAPPED()
        overlap.hEvent = event
        issued = finished = False
        try:
            self._check(interrupted)
            operation = self.kernel.WriteFile if writing else self.kernel.ReadFile
            issued = True
            ok = operation(self.handle, buffer, size, None, ctypes.byref(overlap))
            if not ok:
                error = _last_error()
                if error not in (997, 234):  # IO_PENDING / MORE_DATA
                    finished = True
                    if not writing and error in (109, 233):
                        return 0
                    raise OSError(error, 'Controller pipe I/O failed')
            count = DWORD()
            while True:
                self._check(interrupted)
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise socket.timeout('Controller pipe I/O timed out')
                ok = self.kernel.GetOverlappedResult(self.handle, ctypes.byref(overlap), ctypes.byref(count), False)
                error = 0 if ok else _last_error()
                if error != 996:
                    finished = True
                    self._check(interrupted)
                    if not writing and error in (109, 233):
                        return 0
                    if error and (writing or error != 234):
                        raise OSError(error, 'Controller pipe I/O failed')
                    if count.value > size:
                        raise OSError('Controller pipe returned an invalid byte count')
                    return count.value
                milliseconds = 50 if remaining is None else max(1, min(50, math.ceil(remaining * 1000)))
                result = self.kernel.WaitForSingleObject(event, milliseconds)
                if result not in (0, 258):
                    raise OSError(_last_error(), 'WaitForSingleObject controller pipe failed')
        finally:
            while issued and not finished:
                try:
                    # Keep ownership in this frame even if a second interrupt
                    # hits the drain's entry/setup, outside its polling loop.
                    self._drain(overlap)
                    finished = True
                except KeyboardInterrupt:
                    continue
            self.kernel.CloseHandle(event)

    def read(self, size):
        buffer = ctypes.create_string_buffer(size)
        count = self._transfer(buffer, size, False, self.timeout)
        return buffer.raw[:count]

    def write_all(self, data):
        view = memoryview(data)
        deadline = None if self.timeout is None else time.monotonic() + self.timeout
        while view:
            remaining = None if deadline is None else max(0, deadline - time.monotonic())
            buffer = ctypes.create_string_buffer(view.tobytes())
            count = self._transfer(buffer, len(view), True, remaining)
            if not count:
                raise OSError('Controller pipe closed during write')
            view = view[count:]
