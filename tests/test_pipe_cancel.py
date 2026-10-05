"""Windows cancellation uses owned fixtures only; never the real controller."""
from concurrent.futures import ThreadPoolExecutor
import sys
import threading
import time
import unittest
from unittest import mock

import clash_speedbench as core
from speedbench_process import cancellation_scope
from pipe_fixture import http_pipe


@unittest.skipUnless(sys.platform == 'win32', 'native Windows pipe fixture')
class NativePipeCancellationTest(unittest.TestCase):
    def stall(self, body):
        received = threading.Event(); cancel = threading.Event()
        def handler(request, payload, send, release):
            if request.startswith('PUT '):
                send(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}')
                return
            if body:
                send(b'HTTP/1.1 200 OK\r\nContent-Length: 100\r\nConnection: close\r\n\r\n{')
            received.set(); release.wait(3)
        with http_pipe(handler, instances=2) as (base, release, requests), \
                mock.patch.object(core, 'cancel_requested', side_effect=cancel.is_set), \
                ThreadPoolExecutor(max_workers=1) as pool:
            api = core.MihomoAPI(base, timeout=2)
            future = pool.submit(api.proxy_delay, 'fixture', 'u', 5000)
            try:
                self.assertTrue(received.wait(1)); self.assertFalse(future.done())
                start = time.monotonic(); cancel.set()
                with self.assertRaises(KeyboardInterrupt): future.result(timeout=.8)
                self.assertLess(time.monotonic()-start, .8)
                # The globally cancelled task must still restore its selection.
                self.assertEqual(api.put('/proxies/GLOBAL', {'name': 'original'}), {})
                self.assertEqual(len(requests), 2)
            finally: release.set()

    def test_cancel_stalled_headers_and_restore(self): self.stall(False)
    def test_cancel_connection_close_body_and_restore(self): self.stall(True)

    def test_timeout_is_probe_failure_without_cancellation(self):
        def handler(request, payload, send, release): release.wait(3)
        with http_pipe(handler) as (base, release, _):
            try:
                start = time.monotonic()
                self.assertIsNone(core.MihomoAPI(base, timeout=.1).proxy_delay('fixture', 'u', 5000))
                self.assertLess(time.monotonic()-start, .8)
            finally: release.set()

    def test_pre_cancel_does_not_open_pipe(self):
        with mock.patch.object(core, '_open_pipe_handle', side_effect=AssertionError('pre-cancelled request opened a pipe')) as opened, cancellation_scope(lambda: True):
            with self.assertRaises(KeyboardInterrupt): core.MihomoAPI('pipe://fixture').get('/version')
        opened.assert_not_called()

    def test_chunked_success_and_nt_path_fallback(self):
        def handler(request, payload, send, release):
            send(b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n'
                 b'8\r\n{"delay"\r\n4\r\n:20}\r\n0\r\n\r\n')
        for fallback in (False, True):
            with self.subTest(fallback=fallback), http_pipe(handler) as (base, _, requests):
                factory = core._pipe_kernel32
                def kernel():
                    dll = factory()
                    dll.CreateFileW = mock.Mock(return_value=core._INVALID_HANDLE_VALUE)
                    return dll
                context = mock.patch.object(core, '_pipe_kernel32', side_effect=kernel) if fallback else mock.patch.object(core, '_pipe_kernel32', side_effect=factory)
                with context:
                    self.assertEqual(core.MihomoAPI(base).proxy_delay('fixture', 'u', 5000), 20)
                self.assertEqual(len(requests), 1)
