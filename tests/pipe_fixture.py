"""Independent Win32 HTTP fixture; owns only randomly named test pipes.

Server I/O deliberately stays synchronous (not the client's implementation).
Cleanup cancels only fixture threads and joins them before closing handles.
Never opens the user's Verge pipe, configuration, or network.
"""
import ctypes
from ctypes import wintypes
from contextlib import contextmanager
import sys
import threading
import uuid


@contextmanager
def http_pipe(handler, instances=1):
    if sys.platform != 'win32':
        raise OSError('Windows-only fixture')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateNamedPipeW.argtypes = [wintypes.LPCWSTR] + [wintypes.DWORD]*6 + [wintypes.LPVOID]
    kernel.CreateNamedPipeW.restype = wintypes.HANDLE
    kernel.ConnectNamedPipe.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
    kernel.ConnectNamedPipe.restype = wintypes.BOOL
    kernel.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                               ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    kernel.WriteFile.argtypes = kernel.ReadFile.argtypes
    kernel.ReadFile.restype = kernel.WriteFile.restype = wintypes.BOOL
    kernel.DisconnectNamedPipe.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenThread.restype = wintypes.HANDLE
    kernel.CancelSynchronousIo.argtypes = [wintypes.HANDLE]
    kernel.CancelSynchronousIo.restype = wintypes.BOOL
    name = 'speedbench-fixture-' + uuid.uuid4().hex
    handles = []; threads = []; errors = []
    release = threading.Event(); stopped = threading.Event(); requests = []

    def serve(handle):
        try:
            if not kernel.ConnectNamedPipe(handle, None) and ctypes.get_last_error() != 535:
                if stopped.is_set(): return
                raise OSError(ctypes.get_last_error(), 'fixture connect')
            content = b''
            while b'\r\n\r\n' not in content:
                buf = ctypes.create_string_buffer(8192); count = wintypes.DWORD()
                if not kernel.ReadFile(handle, buf, len(buf), ctypes.byref(count), None) or not count.value:
                    return
                content += buf.raw[:count.value]
                if len(content) > 65536: raise AssertionError('fixture headers too large')
            headers, body = content.split(b'\r\n\r\n', 1)
            length = 0
            for row in headers.split(b'\r\n')[1:]:
                if row.lower().startswith(b'content-length:'): length = int(row.split(b':', 1)[1])
            while len(body) < length:
                buf = ctypes.create_string_buffer(length-len(body)); count = wintypes.DWORD()
                if not kernel.ReadFile(handle, buf, len(buf), ctypes.byref(count), None) or not count.value:
                    return
                body += buf.raw[:count.value]
            request = headers.split(b'\r\n', 1)[0].decode('ascii')
            requests.append(request)
            sent = []
            def send(data):
                buffer = ctypes.create_string_buffer(data); written = wintypes.DWORD()
                if not kernel.WriteFile(handle, buffer, len(data), ctypes.byref(written), None):
                    raise OSError(ctypes.get_last_error(), 'fixture write')
                if written.value != len(data): raise AssertionError('fixture short write')
                sent.append(True)
            handler(request, body, send, release)
            # DisconnectNamedPipe discards unread output. Wait for the HTTP
            # client to close after reading it, without using blocking flush.
            if sent and not release.is_set():
                buf = ctypes.create_string_buffer(1); count = wintypes.DWORD()
                kernel.ReadFile(handle, buf, 1, ctypes.byref(count), None)
        except BaseException as exc:
            if not stopped.is_set(): errors.append(exc)
        finally:
            kernel.DisconnectNamedPipe(handle)

    try:
        for _ in range(instances):
            # Byte mode, synchronous server, duplex; client may use overlapped I/O.
            handle = kernel.CreateNamedPipeW('\\\\.\\pipe\\'+name, 3, 0,
                                             instances, 65536, 65536, 0, None)
            if handle == wintypes.HANDLE(-1).value:
                raise OSError(ctypes.get_last_error(), 'fixture create')
            handles.append(handle)
            thread = threading.Thread(target=serve, args=(handle,)); threads.append(thread)
            thread.start()
        yield 'pipe://'+name, release, requests
    finally:
        stopped.set(); release.set()
        for thread in threads:
            thread.join(.1)
            if thread.is_alive():
                # THREAD_TERMINATE is the access required by CancelSynchronousIo.
                thread_handle = kernel.OpenThread(1, False, thread.native_id)
                if not thread_handle: raise AssertionError('fixture thread handle unavailable')
                try:
                    # An accept/read can race the first cancellation. Bound retry
                    # and join each time; never close a live I/O's pipe buffer.
                    for _ in range(20):
                        kernel.CancelSynchronousIo(thread_handle); thread.join(.05)
                        if not thread.is_alive(): break
                finally: kernel.CloseHandle(thread_handle)
            if thread.is_alive(): raise AssertionError('fixture thread did not stop')
        for handle in handles: kernel.CloseHandle(handle)
        if errors: raise AssertionError('fixture server failed') from errors[0]
