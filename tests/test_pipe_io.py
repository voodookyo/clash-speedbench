"""Portable Win32 contract tests; native pipe acceptance stays separate."""
import ctypes
import importlib
import inspect
import signal
import socket
import sys
import threading
import unittest
import weakref
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

import clash_speedbench as core
from speedbench_process import cancellation_scope


class FakeKernel:
    """Retain weak references only: pending native I/O must own live storage."""
    def __init__(self, *, pending=False, data=b'fixture', result_error=0):
        self.error = 0
        self.pending = pending
        self.data = data
        self.result_error = result_error
        self.cancelled = False
        self.cancel_error = 0
        self.drain_polls = 0
        self.wait_action = None
        self.second_interrupt = False
        self.events = []
        self.closed = []
        self.operations = []
        self.cancel_calls = []
        self.writes = []
        self.write_limit = None
        for name in ('CreateEventW', 'ReadFile', 'WriteFile', 'GetOverlappedResult',
                     'WaitForSingleObject', 'CancelIoEx', 'CloseHandle'):
            setattr(self, name, mock.Mock(side_effect=getattr(self, '_' + name)))

    def _CreateEventW(self, security, manual, initial, name):
        assert manual and not initial and name is None
        event = 100 + len(self.events)
        self.events.append(event)
        return event

    def _start(self, handle, buffer, size, count, pointer, writing):
        assert count is None  # Overlapped results, never the synchronous count.
        op = pointer._obj
        self.active = (handle, weakref.ref(buffer), weakref.ref(op), size, writing)
        self.operations.append((id(op), op.hEvent))
        self.complete = False
        self.transferred = min(size, self.write_limit) if writing and self.write_limit is not None else (size if writing else min(size, len(self.data)))
        if writing:
            self.writes.append(ctypes.string_at(buffer, self.transferred))
        self.error = 997 if self.pending else self.result_error
        return not self.error

    def _ReadFile(self, *args): return self._start(*args, writing=False)
    def _WriteFile(self, *args): return self._start(*args, writing=True)

    def _GetOverlappedResult(self, handle, pointer, count, wait):
        active_handle, buffer, op, size, writing = self.active
        assert handle == active_handle and pointer._obj is op()
        assert buffer() is not None and op() is not None
        assert op().hEvent not in self.closed and not wait
        if self.pending:
            self.error = 996
            return False
        self.complete = True
        count._obj.value = self.transferred
        if not writing and self.transferred:
            ctypes.memmove(buffer(), self.data, self.transferred)
        self.error = self.result_error
        return not self.error

    def _WaitForSingleObject(self, event, milliseconds):
        assert 0 < milliseconds <= 50 and event == self.active[2]().hEvent
        if self.wait_action is not None:
            action, self.wait_action = self.wait_action, None
            action()
        if self.cancelled:
            if self.second_interrupt:
                self.second_interrupt = False
                raise KeyboardInterrupt('second interrupt during drain')
            if self.drain_polls:
                self.drain_polls -= 1
            else:
                self.pending = False
                self.result_error = 995 if not self.cancel_error else 0
        return 258

    def _CancelIoEx(self, handle, pointer):
        assert pointer is not None and pointer._obj is self.active[2]()
        assert handle == self.active[0]
        self.cancel_calls.append((handle, id(pointer._obj)))
        self.cancelled = True
        self.error = self.cancel_error
        return not self.cancel_error

    def _CloseHandle(self, event):
        assert self.complete, 'event closed before native completion was observed'
        self.closed.append(event)
        return True


class PipeFacadeTest(unittest.TestCase):
    def test_precancelled_scope_never_opens_pipe(self):
        with cancellation_scope(lambda: True), mock.patch.object(core, '_open_pipe_handle', side_effect=AssertionError('opened')) as opened:
            with self.assertRaises(KeyboardInterrupt): core.MihomoAPI('pipe://fixture').get('/version')
        opened.assert_not_called()

    def test_restore_outside_scope_keeps_synchronous_plumbing(self):
        response = b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}'
        with mock.patch.object(core, '_open_pipe_handle', return_value=77) as opened, \
                mock.patch.object(core, '_pipe_read', side_effect=[response, b'']), \
                mock.patch.object(core, '_pipe_write_all') as write, \
                mock.patch.object(core, '_pipe_close') as close, \
                mock.patch.object(core, 'cancel_requested', return_value=True):
            self.assertEqual(core.MihomoAPI('pipe://fixture').put('/proxies/GLOBAL', {'name': 'original'}), {})
        opened.assert_called_once_with('fixture')
        self.assertEqual(write.call_count, 2)  # One header, one body; no replay.
        close.assert_called_once_with(77)

    def test_open_flags_and_nt_fallback_keep_overlapped_handle(self):
        from tests.test_windows import reload_with_platform
        with reload_with_platform(core, 'win32'), mock.patch.object(sys, 'platform', 'win32'):
            for fallback in (False, True):
                with self.subTest(fallback=fallback):
                    kernel = mock.Mock()
                    kernel.CreateFileW.return_value = core._INVALID_HANDLE_VALUE if fallback else 77
                    nt = mock.Mock()
                    def nt_open(handle, *args):
                        handle._obj.value = 78
                        self.assertEqual(args[7], 0)  # No synchronous CreateOptions.
                        return 0
                    nt.NtCreateFile.side_effect = nt_open
                    with mock.patch.object(core, '_pipe_kernel32', return_value=kernel), \
                            mock.patch.object(core.ctypes, 'WinDLL', return_value=nt, create=True):
                        self.assertEqual(core._open_pipe_handle('fixture', overlapped=True), 78 if fallback else 77)
                    self.assertEqual(kernel.CreateFileW.call_args.args[5], 0x40000000)
                    self.assertEqual(nt.NtCreateFile.call_count, int(fallback))


class OverlappedPipeTest(unittest.TestCase):
    def setUp(self):
        self.pipe = importlib.import_module('speedbench_pipe')

    def io(self, kernel, cancel=lambda: False, timeout=1):
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(self.pipe, '_last_error', side_effect=lambda: kernel.error).start()
        return self.pipe.PipeIO(77, cancel, timeout, kernel=kernel)

    def test_abi_uses_windows_widths_on_posix_and_pointer_sized_handles(self):
        pointer = ctypes.sizeof(ctypes.c_void_p)
        self.assertEqual(ctypes.sizeof(self.pipe.DWORD), 4)
        self.assertEqual(ctypes.sizeof(self.pipe.BOOL), 4)
        self.assertEqual(ctypes.sizeof(self.pipe.OVERLAPPED), 32 if pointer == 8 else 20)
        self.assertEqual(self.pipe.OVERLAPPED.hEvent.offset, 24 if pointer == 8 else 16)
        kernel = FakeKernel()
        with mock.patch.object(self.pipe.ctypes, 'WinDLL', return_value=kernel, create=True):
            self.pipe._kernel32()
        self.assertIs(kernel.CreateEventW.restype, ctypes.c_void_p)
        self.assertIs(kernel.GetOverlappedResult.argtypes[1]._type_, self.pipe.OVERLAPPED)

    def test_import_does_not_load_windows_dll(self):
        with mock.patch.object(self.pipe.ctypes, 'WinDLL', side_effect=AssertionError('eager DLL'), create=True):
            importlib.reload(self.pipe)

    def test_immediate_read_observes_completion_and_closes_only_event(self):
        kernel = FakeKernel()
        self.assertEqual(self.io(kernel).read(40), b'fixture')
        self.assertEqual(kernel.closed, kernel.events)
        self.assertEqual(kernel.GetOverlappedResult.call_count, 1)
        kernel.CancelIoEx.assert_not_called()

    def test_pending_success_is_not_a_new_read(self):
        kernel = FakeKernel(pending=True)
        kernel.wait_action = lambda: setattr(kernel, 'pending', False)
        self.assertEqual(self.io(kernel).read(40), b'fixture')
        self.assertEqual(kernel.ReadFile.call_count, 1)
        self.assertEqual(kernel.closed, kernel.events)

    def cancelled_transfer(self, writing=False, race=False, interrupt=False):
        kernel = FakeKernel(pending=True)
        kernel.drain_polls = 2
        kernel.cancel_error = 1168 if race else 0
        kernel.second_interrupt = interrupt
        cancelled = [False]
        kernel.wait_action = lambda: cancelled.__setitem__(0, True)
        io = self.io(kernel, lambda: cancelled[0])
        with self.assertRaises(KeyboardInterrupt):
            io.write_all(b'payload') if writing else io.read(40)
        self.assertEqual(kernel.cancel_calls, [(77, kernel.operations[0][0])])
        self.assertEqual(kernel.closed, kernel.events)
        self.assertEqual(kernel.ReadFile.call_count + kernel.WriteFile.call_count, 1)

    def test_cancel_read_waits_for_exact_operation_and_live_buffers(self): self.cancelled_transfer()
    def test_cancel_write_waits_for_exact_operation_and_live_buffers(self): self.cancelled_transfer(writing=True)
    def test_cancel_not_found_completion_race_still_preserves_cancellation(self): self.cancelled_transfer(race=True)
    def test_second_keyboard_interrupt_does_not_release_pending_storage(self): self.cancelled_transfer(interrupt=True)

    def test_interrupt_during_drain_setup_keeps_pending_storage_alive(self):
        kernel = FakeKernel(pending=True)
        cancel = [False]
        kernel.wait_action = lambda: cancel.__setitem__(0, True)
        io = self.io(kernel, lambda: cancel[0])
        count_type = self.pipe.DWORD
        calls = [0]
        def count():
            calls[0] += 1
            if calls[0] == 2:
                raise KeyboardInterrupt('interrupt before drain polling')
            return count_type()
        with mock.patch.object(self.pipe, 'DWORD', side_effect=count):
            with self.assertRaises(KeyboardInterrupt): io.read(40)
        self.assertTrue(kernel.complete)
        self.assertEqual(kernel.closed, kernel.events)

    def test_second_sigint_at_cleanup_loop_condition_is_deferred_until_completion(self):
        kernel = FakeKernel(pending=True)
        cancel = [False]
        kernel.wait_action = lambda: cancel.__setitem__(0, True)
        io = self.io(kernel, lambda: cancel[0])
        target = next((getattr(self.pipe.PipeIO, name) for name in ('_transfer_owned', '_transfer')
                       if hasattr(self.pipe.PipeIO, name)))
        lines, first = inspect.getsourcelines(target)
        location = first + next(index for index, line in enumerate(lines) if 'while issued and not finished:' in line)
        delivered = []
        def trace(frame, event, arg):
            if frame.f_code is target.__code__ and event == 'line' and frame.f_lineno == location and not delivered:
                delivered.append(True)
                signal.raise_signal(signal.SIGINT)
            return trace
        previous_trace, previous_signal = sys.gettrace(), signal.getsignal(signal.SIGINT)
        try:
            signal.signal(signal.SIGINT, signal.default_int_handler)
            sys.settrace(trace)
            with self.assertRaises(KeyboardInterrupt): io.read(40)
            self.assertIs(signal.getsignal(signal.SIGINT), signal.default_int_handler)
        finally:
            sys.settrace(previous_trace)
            signal.signal(signal.SIGINT, previous_signal)
        self.assertTrue(delivered)
        self.assertTrue(kernel.complete)
        self.assertEqual(kernel.closed, kernel.events)

    def test_first_default_sigint_cancels_pending_io_and_restores_handler(self):
        kernel = FakeKernel(pending=True)
        kernel.wait_action = lambda: signal.raise_signal(signal.SIGINT)
        previous = signal.getsignal(signal.SIGINT)
        try:
            signal.signal(signal.SIGINT, signal.default_int_handler)
            with self.assertRaises(KeyboardInterrupt): self.io(kernel).read(40)
            self.assertIs(signal.getsignal(signal.SIGINT), signal.default_int_handler)
        finally:
            signal.signal(signal.SIGINT, previous)
        self.assertEqual(len(kernel.cancel_calls), 1)
        self.assertTrue(kernel.complete)
        self.assertEqual(kernel.closed, kernel.events)

    def test_custom_sigint_handler_keeps_its_existing_behavior(self):
        kernel = FakeKernel(pending=True)
        calls = []
        def custom(signum, frame): calls.append(signum)
        def release():
            kernel.pending = False
            signal.raise_signal(signal.SIGINT)
        kernel.wait_action = release
        previous = signal.getsignal(signal.SIGINT)
        try:
            signal.signal(signal.SIGINT, custom)
            self.assertEqual(self.io(kernel).read(40), b'fixture')
            self.assertIs(signal.getsignal(signal.SIGINT), custom)
        finally:
            signal.signal(signal.SIGINT, previous)
        self.assertEqual(calls, [signal.SIGINT])

    def test_worker_thread_never_installs_process_signal_handler(self):
        kernel = FakeKernel()
        io = self.io(kernel)
        with mock.patch.object(signal, 'signal', side_effect=AssertionError('worker changed signal handler')), \
                ThreadPoolExecutor(max_workers=1) as pool:
            self.assertEqual(pool.submit(io.read, 40).result(timeout=1), b'fixture')

    def test_timeout_drains_pending_io_and_preserves_socket_timeout(self):
        kernel = FakeKernel(pending=True)
        io = self.io(kernel, timeout=.1)
        with mock.patch.object(self.pipe.time, 'monotonic', side_effect=[0, 0, .2]):
            with self.assertRaises(socket.timeout): io.read(40)
        self.assertEqual(len(kernel.cancel_calls), 1)
        self.assertEqual(kernel.closed, kernel.events)

    def test_precancel_never_allocates_event_or_starts_io(self):
        kernel = FakeKernel()
        with self.assertRaises(KeyboardInterrupt): self.io(kernel, lambda: True).read(40)
        kernel.CreateEventW.assert_not_called()
        kernel.ReadFile.assert_not_called()

    def test_eof_more_data_and_other_error_are_distinct(self):
        for error in (109, 233, 234, 5):
            with self.subTest(error=error):
                kernel = FakeKernel(pending=True, result_error=error)
                kernel.wait_action = lambda: setattr(kernel, 'pending', False)
                io = self.io(kernel)
                if error in (109, 233): self.assertEqual(io.read(40), b'')
                elif error == 234: self.assertEqual(io.read(40), b'fixture')
                else:
                    with self.assertRaises(OSError) as raised: io.read(40)
                    self.assertEqual(raised.exception.errno, 5)
                kernel.CancelIoEx.assert_not_called()
                self.assertEqual(kernel.closed, kernel.events)

    def test_short_writes_keep_payload_and_zero_progress_fails(self):
        kernel = FakeKernel(); kernel.write_limit = 2
        self.io(kernel).write_all(b'abcde')
        self.assertEqual(b''.join(kernel.writes), b'abcde')
        self.assertEqual(kernel.closed, kernel.events)
        self.assertEqual(len(set(event for _, event in kernel.operations)), 3)
        kernel = FakeKernel(); kernel.write_limit = 0
        with self.assertRaises(OSError): self.io(kernel).write_all(b'abc')
        self.assertEqual(kernel.WriteFile.call_count, 1)

    def test_partial_write_exhausting_budget_never_submits_remaining_bytes(self):
        kernel = FakeKernel(); kernel.write_limit = 2
        io = self.io(kernel, timeout=.1)
        clock = [0]
        result = kernel.GetOverlappedResult.side_effect
        def complete(*args):
            ok = result(*args)
            clock[0] = .2
            return ok
        kernel.GetOverlappedResult.side_effect = complete
        with mock.patch.object(self.pipe.time, 'monotonic', side_effect=lambda: clock[0]):
            with self.assertRaises(socket.timeout): io.write_all(b'abcde')
        self.assertEqual(kernel.WriteFile.call_count, 1)
        self.assertEqual(b''.join(kernel.writes), b'ab')

    def test_response_owns_io_after_connection_close_and_closes_handle_once(self):
        kernel = FakeKernel(data=b'body')
        sock = core._PipeSock(77, pipe_io=self.io(kernel))
        with mock.patch.object(core, '_pipe_close') as close:
            reader = sock.makefile('rb'); sock.close()
            close.assert_not_called()
            self.assertEqual(reader.read(4), b'body')
            reader.close(); reader.close(); sock.close()
        close.assert_called_once_with(77)

    def test_scope_opens_overlapped_handle_and_failed_setup_closes_it(self):
        with cancellation_scope(lambda: False), \
                mock.patch.object(core, '_open_pipe_handle', return_value=77) as opened, \
                mock.patch.object(self.pipe, 'PipeIO', side_effect=OSError('binding failed')), \
                mock.patch.object(core, '_pipe_close') as close:
            with self.assertRaises(OSError): core.WinPipeHTTPConnection('fixture', 1).connect()
        opened.assert_called_once_with('fixture', overlapped=True)
        close.assert_called_once_with(77)

    def test_http_chunked_fragments_use_one_request_and_reader_ownership(self):
        chunks = [b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n',
                  b'8\r\n{"delay"\r\n', b'4\r\n:20}\r\n0\r\n\r\n']
        kernel = FakeKernel()
        start = kernel.ReadFile.side_effect
        def read(*args):
            kernel.data = chunks.pop(0) if chunks else b''
            return start(*args)
        kernel.ReadFile.side_effect = read
        with mock.patch.object(self.pipe, '_kernel32', return_value=kernel), \
                mock.patch.object(self.pipe, '_last_error', side_effect=lambda: kernel.error), \
                mock.patch.object(core, '_open_pipe_handle', return_value=77) as opened, \
                mock.patch.object(core, '_pipe_close') as close:
            self.assertEqual(core.MihomoAPI('pipe://fixture').proxy_delay('fixture', 'u', 5000), 20)
        opened.assert_called_once_with('fixture', overlapped=True)
        self.assertEqual(b''.join(kernel.writes).count(b'GET '), 1)
        self.assertFalse(chunks)
        self.assertEqual(kernel.closed, kernel.events)
        close.assert_called_once_with(77)

    def test_http_cancel_stalled_header_and_transferred_body_then_restore(self):
        for body in (False, True):
            with self.subTest(body=body):
                cancel = threading.Event()
                kernel = FakeKernel()
                chunks = [b'HTTP/1.1 200 OK\r\nContent-Length: 100\r\nConnection: close\r\n\r\n{'] if body else []
                start = kernel.ReadFile.side_effect
                def read(*args):
                    if chunks:
                        kernel.data = chunks.pop(0)
                    else:
                        kernel.pending = True
                        kernel.wait_action = cancel.set
                    return start(*args)
                kernel.ReadFile.side_effect = read
                restore = b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}'
                with mock.patch.object(self.pipe, '_kernel32', return_value=kernel), \
                        mock.patch.object(self.pipe, '_last_error', side_effect=lambda: kernel.error), \
                        mock.patch.object(core, 'cancel_requested', side_effect=cancel.is_set), \
                        mock.patch.object(core, '_open_pipe_handle', side_effect=[77, 78]) as opened, \
                        mock.patch.object(core, '_pipe_close') as close, \
                        mock.patch.object(core, '_pipe_read', return_value=restore), \
                        mock.patch.object(core, '_pipe_write_all') as restore_write:
                    api = core.MihomoAPI('pipe://fixture')
                    with self.assertRaises(KeyboardInterrupt): api.proxy_delay('fixture', 'u', 5000)
                    self.assertEqual(api.put('/proxies/GLOBAL', {'name': 'original'}), {})
                self.assertEqual(opened.call_args_list, [mock.call('fixture', overlapped=True), mock.call('fixture')])
                self.assertEqual(close.call_args_list, [mock.call(77), mock.call(78)])
                self.assertEqual(restore_write.call_count, 2)
                self.assertEqual(len(kernel.cancel_calls), 1)
                self.assertEqual(kernel.closed, kernel.events)
