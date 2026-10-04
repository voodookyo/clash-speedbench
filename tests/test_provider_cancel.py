"""Real loopback provider transport; never calls a paid API or public DNS."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import http.client
import http.server
import socket
import ssl
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import speedbench_ip_intel as intel
import speedbench_process as process
from tests.test_transport_cancel import stalled_http


class ProviderTransportCancellationTest(unittest.TestCase):
    def stalled(self, body):
        cancelled = threading.Event()
        with stalled_http(body) as (base, started, release, requests), ThreadPoolExecutor(1) as pool:
            def query():
                with process.cancellation_scope(cancelled.is_set):
                    return intel._urllib_transport(base + '/lookup', timeout=2)
            future = pool.submit(query)
            self.assertTrue(started.wait(1))
            cancelled.set()
            try:
                with self.assertRaises(KeyboardInterrupt):
                    future.result(timeout=1)
                self.assertEqual(requests, ['/lookup'])
            finally:
                release.set()

    def test_cancel_stalled_headers(self): self.stalled(False)
    def test_cancel_stalled_connection_close_body(self): self.stalled(True)

    def test_precancelled_request_does_not_open_connection(self):
        with process.cancellation_scope(lambda: True), mock.patch.object(socket, 'socket') as create:
            with self.assertRaises(KeyboardInterrupt):
                intel._urllib_transport('http://127.0.0.1:1', timeout=.1)
            create.assert_not_called()

    def test_shared_deadline_stops_trickled_body_and_closes_stream(self):
        import urllib.error
        disconnected=threading.Event(); release=threading.Event()
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_GET(self):
                self.send_response(200); self.send_header('Content-Length','10'); self.end_headers()
                try:
                    for _ in range(10):
                        self.wfile.write(b'x'); self.wfile.flush()
                        if release.wait(.06):break
                except OSError: disconnected.set()
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01});thread.start()
        try:
            with process.cancellation_scope(lambda: False), self.assertRaises((socket.timeout,urllib.error.URLError)):
                intel._urllib_transport('http://127.0.0.1:'+str(server.server_port),timeout=.15)
            self.assertTrue(disconnected.wait(1))
        finally:
            release.set(); server.shutdown(); server.server_close(); thread.join(2)

    def test_localhost_dns_child_and_chunked_response_succeed_without_redirect(self):
        paths=[]
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                paths.append(self.path)
                if self.path=='/redirect':
                    self.send_response(302);self.send_header('Location','/unexpected');self.send_header('Content-Length','0');self.end_headers()
                else:
                    self.send_response(200);self.send_header('Transfer-Encoding','chunked');self.end_headers()
                    for part in (b'7\r\n{"ok": \r\n',b'5\r\ntrue}\r\n',b'0\r\n\r\n'):
                        self.wfile.write(part);self.wfile.flush()
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01});thread.start()
        try:
            with process.cancellation_scope(lambda:False):
                base='http://localhost:'+str(server.server_port)
                response=intel._urllib_transport(base+'/ok',timeout=2)
                self.assertEqual(response.body,b'{"ok": true}')
                response=intel._urllib_transport(base+'/redirect',timeout=2,headers={'Authorization':'Bearer CANARY'})
                self.assertEqual(response.status_code,302)
            self.assertEqual(paths,['/ok','/redirect'])
        finally:server.shutdown();server.server_close();thread.join(2)

    def test_cancel_http_error_body_closes_its_owned_response(self):
        started=threading.Event();processed=threading.Event();release=threading.Event();cancel=threading.Event();responses=[]
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                self.send_response(429);self.send_header('Content-Length','100');self.end_headers()
                self.wfile.write(b'{');self.wfile.flush();started.set();release.wait(2)
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01});thread.start()
        original=intel.urllib.request.HTTPErrorProcessor.http_response
        def response_processor(handler,request,response):
            responses.append(response);processed.set();return original(handler,request,response)
        try:
            with mock.patch.object(intel.urllib.request.HTTPErrorProcessor,'http_response',response_processor),ThreadPoolExecutor(1) as pool:
                def query():
                    with process.cancellation_scope(cancel.is_set):
                        return intel._urllib_transport('http://127.0.0.1:'+str(server.server_port),timeout=2)
                future=pool.submit(query);self.assertTrue(started.wait(1));self.assertTrue(processed.wait(1));cancel.set()
                try:
                    with self.assertRaises(KeyboardInterrupt):future.result(timeout=1)
                    self.assertEqual(len(responses),1);self.assertTrue(responses[0].isclosed())
                finally:release.set()
        finally:release.set();server.shutdown();server.server_close();thread.join(2)

    def test_serial_provider_cancellation_retains_completed_provider_and_skips_next(self):
        with tempfile.TemporaryDirectory() as folder, stalled_http() as (base, started, release, _):
            stop = threading.Event()
            first = intel.IpqsProvider(key='fixture', transport=lambda *a, **k: {'success': True, 'fraud_score': 4})
            second = intel.IpInfoProvider(token='fixture', transport=lambda *a, **k: intel._urllib_transport(base, timeout=2))
            third = mock.Mock(); third.name = 'third'
            cache = intel.IpIntelCache(Path(folder) / 'cache.db')
            with ThreadPoolExecutor(1) as pool:
                future = pool.submit(cache.query_many, '192.0.2.1', [first, second, third], 1, cancel=stop.is_set)
                self.assertTrue(started.wait(1)); stop.set()
                try:
                    values = future.result(timeout=1)
                    self.assertEqual(list(values), ['ipqs'])
                    self.assertEqual(values['ipqs'].status, 'ok')
                    third.query.assert_not_called()
                    self.assertIsNone(cache.get('ipinfo', '192.0.2.1'))
                finally: release.set()

    def test_singleflight_waiter_cancels_without_cancelling_owner_or_requerying(self):
        with tempfile.TemporaryDirectory() as folder:
            started = threading.Event(); release = threading.Event(); waited = threading.Event(); stop = threading.Event()
            calls = []
            class Provider:
                name = 'ipqs'
                def query(self, ip):
                    calls.append(ip); started.set(); release.wait(2)
                    return intel.ProviderResult(self.name, ip, 'ok', normalized={'fraud_score': 4})
            cache = intel.IpIntelCache(Path(folder) / 'cache.db', observer=lambda name, metric: waited.set() if name == 'intel_cache_wait' else None)
            with ThreadPoolExecutor(2) as pool:
                owner = pool.submit(cache.get_or_query, Provider(), '192.0.2.1')
                self.assertTrue(started.wait(1))
                def wait():
                    with process.cancellation_scope(stop.is_set):
                        return cache.get_or_query(Provider(), '192.0.2.1')
                # Wait until the flight waiter has actually entered Event.wait.
                flight = cache._flights[('ipqs', '192.0.2.1')]
                original = flight.event.wait
                entered = threading.Event()
                def event_wait(timeout=None): entered.set(); return original(timeout)
                with mock.patch.object(flight.event, 'wait', side_effect=event_wait):
                    waiter = pool.submit(wait)
                    try:
                        self.assertTrue(entered.wait(1)); stop.set()
                        with self.assertRaises(KeyboardInterrupt): waiter.result(timeout=1)
                        self.assertFalse(owner.done()); self.assertTrue(waited.is_set())
                    finally: release.set()
                self.assertEqual(owner.result(timeout=1).status, 'ok')
            self.assertEqual(calls, ['192.0.2.1'])
            self.assertEqual(cache.get('ipqs', '192.0.2.1').status, 'cache_hit')


class EstablishmentCancellationTest(unittest.TestCase):
    def test_resolver_interpreter_failure_is_dns_error_without_raw_stderr(self):
        import subprocess
        output=subprocess.CompletedProcess([],1,stdout=b'',stderr=b'CANARY-process-error')
        with mock.patch.object(process,'run_cancellable',return_value=output):
            with self.assertRaises(socket.gaierror) as caught:
                process._resolve_addresses('localhost',1,lambda:False,None)
        self.assertNotIn('CANARY',str(caught.exception))

    def test_failed_tls_verification_closes_wrapped_socket_without_retry(self):
        context=mock.Mock();wrapped=mock.Mock();context.wrap_socket.return_value=wrapped
        wrapped.do_handshake.side_effect=ssl.SSLCertVerificationError('fixture certificate')
        wrapper=process._ScopedTLSContext(context,lambda:False,None)
        with self.assertRaises(ssl.SSLCertVerificationError):wrapper.wrap_socket(mock.Mock(),server_hostname='fixture.invalid')
        context.wrap_socket.assert_called_once()
        self.assertEqual(context.wrap_socket.call_args.kwargs,dict(server_hostname='fixture.invalid',do_handshake_on_connect=False))
        wrapped.do_handshake.assert_called_once();wrapped.close.assert_called_once()

    def test_dns_deadline_reaps_stuck_child(self):
        import subprocess
        original=subprocess.Popen;children=[]
        def spawn(*args,**kwargs):
            child=original([sys.executable,'-I','-c','import time; time.sleep(20)'],**kwargs)
            children.append(child);return child
        with mock.patch.object(process.subprocess,'Popen',side_effect=spawn):
            with self.assertRaises(socket.timeout):
                process._resolve_addresses('localhost',1,lambda:False,process.time.monotonic()+.1)
        self.assertEqual(len(children),1);self.assertIsNotNone(children[0].poll())

    def test_cancel_during_connect_closes_owned_socket_without_next_address(self):
        stop=threading.Event();sock=mock.Mock();sock.connect_ex.return_value=process.errno.EINPROGRESS
        addresses=[(socket.AF_INET,socket.SOCK_STREAM,0,'',('127.0.0.1',1))]*2
        def wait(*args):stop.set();return [],[],[]
        with mock.patch.object(process,'_resolve_addresses',return_value=addresses), \
                mock.patch.object(process.socket,'socket',return_value=sock) as create, \
                mock.patch.object(process.select,'select',side_effect=wait):
            with self.assertRaises(KeyboardInterrupt):
                process._create_connection(('localhost',1),2,None,stop.is_set,None)
        self.assertEqual(create.call_count,1);sock.close.assert_called_once()
    def test_real_local_tls_handshake_is_cancellable_without_sending_http(self):
        listener = socket.socket(); listener.bind(('127.0.0.1', 0)); listener.listen(1)
        accepted = threading.Event(); release = threading.Event(); captured = []
        def server():
            peer, _ = listener.accept(); captured.append(peer); accepted.set(); release.wait(3); peer.close()
        thread = threading.Thread(target=server); thread.start()
        stop = threading.Event()
        connection = http.client.HTTPSConnection('127.0.0.1', listener.getsockname()[1], timeout=2)
        self.assertEqual(connection._context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(connection._context.check_hostname)
        try:
            def connect():
                with process.SocketCancellation(connection, stop.is_set): connection.connect()
            with ThreadPoolExecutor(1) as pool:
                future = pool.submit(connect); self.assertTrue(accepted.wait(1)); stop.set()
                try:
                    with self.assertRaises(KeyboardInterrupt): future.result(timeout=1)
                    self.assertEqual(connection._context.verify_mode, ssl.CERT_REQUIRED)
                    self.assertTrue(connection._context.check_hostname)
                finally: release.set(); connection.close()
        finally:
            release.set(); listener.close(); thread.join(3)
            for peer in captured: peer.close()

    def test_scoped_dns_uses_owned_child_and_reaps_on_cancel(self):
        import subprocess
        stop = threading.Event(); children = []; original = subprocess.Popen
        connection = http.client.HTTPConnection('localhost', 1, timeout=2)
        def spawn(*args, **kwargs):
            # Deterministic stuck resolver instead of external DNS.
            child = original([sys.executable, '-I', '-c', 'import time; time.sleep(20)'], **kwargs)
            children.append(child); stop.set(); return child
        try:
            with mock.patch.object(process.subprocess, 'Popen', side_effect=spawn):
                with self.assertRaises(KeyboardInterrupt), process.SocketCancellation(connection, stop.is_set):
                    connection.connect()
            self.assertEqual(len(children), 1)
            self.assertIsNotNone(children[0].poll())
        finally: connection.close()


if __name__ == '__main__': unittest.main()
