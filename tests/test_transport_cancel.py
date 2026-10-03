"""Owned in-flight cancellation, local fixtures only; no real controller/DNS."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import http.server
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import clash_speedbench as core
import speedbench_ip_intel as intel
import speedbench_process as process
import speedbench_workers as workers


@contextmanager
def stalled_http(body=False):
    started=threading.Event();release=threading.Event();requests=[]
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            requests.append(self.path)
            if body:
                self.send_response(200);self.send_header('Content-Length','100')
                self.send_header('Connection','close');self.end_headers()
                self.wfile.write(b'{');self.wfile.flush()
            started.set();release.wait(3)
        def do_PUT(self):
            self.rfile.read(int(self.headers.get('Content-Length','0')))
            requests.append(self.path);self.send_response(200)
            self.send_header('Content-Length','2');self.end_headers();self.wfile.write(b'{}')
    server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
    server.daemon_threads=False
    thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01})
    thread.start()
    try:yield 'http://127.0.0.1:'+str(server.server_port),started,release,requests
    finally:
        release.set();server.shutdown();server.server_close();thread.join(2)


class ControllerCancellationTest(unittest.TestCase):
    def test_fragmented_success_after_poll_wait_keeps_all_http_body_bytes(self):
        received=threading.Event();release=threading.Event();polled=threading.Event()
        select_ready=process.select.select
        def wait_ready(*args):polled.set();return select_ready(*args)
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                self.send_response(200);self.send_header('Content-Length','12')
                self.send_header('Connection','close');self.end_headers()
                self.wfile.write(b'{"delay":');self.wfile.flush();received.set()
                release.wait(2);self.wfile.write(b'20}');self.wfile.flush()
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01});thread.start()
        try:
            with mock.patch.object(process.select,'select',side_effect=wait_ready),ThreadPoolExecutor(max_workers=1) as pool:
                api=core.MihomoAPI('http://127.0.0.1:'+str(server.server_port),timeout=2)
                future=pool.submit(api.proxy_delay,'fixture','u',5000)
                try:self.assertTrue(received.wait(1));self.assertTrue(polled.wait(1));self.assertFalse(future.done())
                finally:release.set()
                self.assertEqual(future.result(timeout=1),20)
        finally:release.set();server.shutdown();server.server_close();thread.join(2)
    def test_uncancelled_stalled_read_keeps_timeout_failure_semantics(self):
        with stalled_http() as (base,started,release,_):
            try:self.assertIsNone(core.MihomoAPI(base,timeout=.1).proxy_delay('fixture','u',5000))
            finally:release.set()
    def stalled(self,body):
        cancelled=threading.Event()
        with stalled_http(body) as (base,started,release,requests), \
                mock.patch.object(core,'cancel_requested',side_effect=cancelled.is_set), \
                ThreadPoolExecutor(max_workers=1) as pool:
            api=core.MihomoAPI(base,timeout=2)
            future=pool.submit(api.proxy_delay,'fixture','https://example.invalid',5000)
            self.assertTrue(started.wait(2));cancelled.set()
            try:
                with self.assertRaises(KeyboardInterrupt):future.result(timeout=1)
                # Restoration is deliberately outside the read-only probe scope.
                self.assertEqual(api.put('/config',{'mode':'rule'}),{})
                self.assertEqual(requests[-1],'/config')
            finally:release.set()
        self.assertFalse(any(t.name=='speedbench-socket-cancel' for t in threading.enumerate()))
    def test_abort_waiting_headers_without_cancelling_restore(self):self.stalled(False)
    def test_abort_connection_close_body_with_transferred_socket_without_cancelling_restore(self):self.stalled(True)
    def test_precancelled_probe_never_enters_connection_io(self):
        with mock.patch.object(core,'cancel_requested',return_value=True), \
                mock.patch.object(core.http.client,'HTTPConnection') as connection:
            with self.assertRaises(KeyboardInterrupt):core.MihomoAPI('http://127.0.0.1:1').proxy_delay('n','u',5)
            connection.return_value.request.assert_not_called()
    def test_pool_local_stop_aborts_sibling_socket_and_does_not_probe_queued_node(self):
        names=[]
        with stalled_http() as (base,started,release,_):
            class Api(core.MihomoAPI):
                def proxy_delay(self,name,*args):
                    names.append(name)
                    if name=='interrupt':
                        if not started.wait(2):raise RuntimeError('fixture did not start')
                        raise KeyboardInterrupt
                    return super().proxy_delay(name,*args)
            try:
                with ThreadPoolExecutor(max_workers=1) as owner:
                    future=owner.submit(workers.probe_latency_pool,Api(base,timeout=2),
                        ['active','interrupt','queued'],5000,max_workers=2,probe_count=1)
                    with self.assertRaises(KeyboardInterrupt):future.result(timeout=1)
            finally:release.set()
        self.assertNotIn('queued',names)


class ScopedCancellationTest(unittest.TestCase):
    def test_socket_reader_transfer_closes_exactly_owned_socket(self):
        client,peer=socket.socketpair();client.settimeout(1)
        try:
            facade=process._PollingSocket(client,lambda:False);reader=facade.makefile('rb')
            facade.close();peer.sendall(b'ok');self.assertEqual(reader.read(2),b'ok')
            self.assertNotEqual(client.fileno(),-1);reader.close();reader.close()
            self.assertEqual(client.fileno(),-1);self.assertNotEqual(peer.fileno(),-1)
        finally:client.close();peer.close()
    def test_tls_want_write_polls_write_side_then_retries_same_operation(self):
        import ssl
        operation=mock.Mock(side_effect=[ssl.SSLWantWriteError(),b'fixture'])
        sock=mock.Mock()
        with mock.patch.object(process.select,'select',return_value=([],[sock],[])) as select_ready:
            self.assertEqual(process._socket_io(sock,operation,lambda:False,1),b'fixture')
        self.assertEqual(select_ready.call_args.args[:3],([],[sock],[]))
    def test_nested_scopes_restore_and_do_not_leak_into_other_threads(self):
        event=threading.Event()
        self.assertIsNone(process.current_cancellation())
        with process.cancellation_scope(event.is_set):
            self.assertFalse(process.current_cancellation()())
            with process.cancellation_scope(lambda:True):self.assertTrue(process.current_cancellation()())
            self.assertFalse(process.current_cancellation()())
            with ThreadPoolExecutor(max_workers=1) as pool:self.assertIsNone(pool.submit(process.current_cancellation).result())
            event.set();self.assertTrue(process.current_cancellation()())
        self.assertIsNone(process.current_cancellation())
    def test_external_call_combines_scoped_and_parent_cancellation(self):
        with process.cancellation_scope(lambda:True), \
                mock.patch.object(core,'run_cancellable') as run, \
                mock.patch.object(core,'_CANCEL_FILE','fixture'), \
                mock.patch.object(core,'cancel_requested',return_value=False):
            core.run_external(['fixture'])
            self.assertTrue(run.call_args.kwargs['cancel']())
        with mock.patch.object(core,'run_cancellable') as run, \
                mock.patch.object(core,'_CANCEL_FILE','fixture'):
            core.run_external(['fixture'])
            self.assertIs(run.call_args.kwargs['cancel'],core.cancel_requested)


class DnsAndReadinessCancellationTest(unittest.TestCase):
    def test_dns_ordinary_failure_is_not_masked_as_sibling_cancellation(self):
        barrier=threading.Barrier(8);lock=threading.Lock();count=[0]
        def resolve(domain,*args):
            cancel=process.current_cancellation()
            with lock:index=count[0];count[0]+=1
            barrier.wait(2)
            if index==7:raise workers.WorkerUnavailable('fixture resolver failure')
            while not cancel():threading.Event().wait(.01)
            raise KeyboardInterrupt
        with mock.patch.object(workers,'doh_resolve',side_effect=resolve):
            with self.assertRaises(workers.WorkerUnavailable):workers.build_hosts([{'server':'node%d.example'%n} for n in range(40)])
    def test_precancelled_dns_never_starts_resolver_or_pool(self):
        with mock.patch.object(workers,'cancel_requested',return_value=True), \
                mock.patch.object(workers,'doh_resolve') as resolve:
            with self.assertRaises(KeyboardInterrupt):workers.build_hosts([])
            resolve.assert_not_called()
    def test_dns_interrupt_sets_local_stop_and_prevents_remaining_domain_requests(self):
        barrier=threading.Barrier(8);lock=threading.Lock();calls=[];scopes=[]
        def resolve(domain,record_type='A'):
            callback=process.current_cancellation()
            with lock:index=len(calls);calls.append(domain);scopes.append(callback)
            barrier.wait(2)
            if index==0:raise KeyboardInterrupt
            deadline=time.monotonic()+2
            while time.monotonic()<deadline:
                if callback():raise KeyboardInterrupt
                threading.Event().wait(.01)
            raise RuntimeError('Sibling DNS did not stop')
        with mock.patch.object(workers,'doh_resolve',side_effect=resolve), \
                mock.patch.object(workers,'cancel_requested',return_value=False):
            with self.assertRaises(KeyboardInterrupt):workers.build_hosts([{'server':'node%d.example'%n} for n in range(40)])
        self.assertEqual(len(calls),8)
        self.assertTrue(all(callable(c) and c() for c in scopes))
        self.assertIsNone(process.current_cancellation())
    def test_dns_scope_reaches_owned_external_child_and_reaps_it(self):
        import subprocess,sys
        event=threading.Event();captured=[];popen=subprocess.Popen
        def spawn(*args,**kwargs):
            child=popen(*args,**kwargs);captured.append(child);event.set();return child
        with process.cancellation_scope(event.is_set),mock.patch.object(process.subprocess,'Popen',side_effect=spawn):
            with self.assertRaises(KeyboardInterrupt):core.run_external([sys.executable,'-c','import time; time.sleep(20)'],capture_output=True,timeout=3)
        self.assertEqual(len(captured),1);self.assertIsNotNone(captured[0].poll())
    def test_precancelled_worker_start_never_initializes(self):
        worker=workers.Worker('fixture',[],{},None)
        with mock.patch.object(workers,'cancel_requested',return_value=True), \
                mock.patch.object(worker,'_initialize_unlocked') as initialize:
            with self.assertRaises(KeyboardInterrupt):worker.start()
            initialize.assert_not_called()
    def test_readiness_cancellation_after_request_never_claims_ready(self):
        worker=workers.Worker('fixture',[],{},None);worker.proc=mock.Mock()
        worker.proc.poll.return_value=None;worker.api=mock.Mock();event=threading.Event()
        worker.api.get.side_effect=lambda *a:event.set()
        with mock.patch.object(workers,'cancel_requested',side_effect=event.is_set):
            with self.assertRaises(KeyboardInterrupt):worker._wait_until_ready(time.time()+1,1)
    def test_readiness_scope_can_abort_stalled_controller(self):
        event=threading.Event()
        with stalled_http() as (base,started,release,_), \
                mock.patch.object(workers,'cancel_requested',side_effect=event.is_set),ThreadPoolExecutor(max_workers=1) as pool:
            worker=workers.Worker('fixture',[],{},None);worker.proc=mock.Mock();worker.proc.poll.return_value=None
            worker.api=core.MihomoAPI(base,timeout=2)
            future=pool.submit(worker._wait_until_ready,time.time()+8,1)
            self.assertTrue(started.wait(2));event.set()
            try:
                with self.assertRaises(KeyboardInterrupt):future.result(timeout=1)
            finally:release.set()


class IntelligenceCancellationTest(unittest.TestCase):
    def test_args_cancelled_during_normal_join_also_skips_remaining_provider(self):
        with tempfile.TemporaryDirectory() as folder:
            args=SimpleNamespace(history=str(Path(folder)/'h.jsonl'),intel_workers=2)
            first=intel.IpqsProvider(key='fixture',transport=lambda *a,**k:setattr(args,'cancelled',True) or {'success':True,'fraud_score':4})
            second=intel.IpInfoProvider(token='fixture',transport=mock.Mock())
            with mock.patch.object(core,'make_default_providers',return_value=[first,second]):enricher=core._IntelEnrichment(args)
            try:
                enricher.submit_ip('192.0.2.1');enricher.finish()
                second.transport.assert_not_called()
                self.assertEqual(enricher.values['192.0.2.1'].provider_results['ipqs'].status,'ok')
            finally:enricher.close()
    def test_query_many_precancelled_makes_no_provider_call_or_fake_result(self):
        with tempfile.TemporaryDirectory() as folder:
            cache=intel.IpIntelCache(Path(folder)/'h.db');provider=mock.Mock(name='provider');provider.name='ipqs'
            self.assertEqual(cache.query_many('192.0.2.1',[provider],max_workers=1,cancel=lambda:True),{})
            provider.query.assert_not_called()
    def test_enricher_close_skips_next_providers_but_joins_current_cache_writers(self):
        with tempfile.TemporaryDirectory() as folder:
            started=threading.Barrier(3);release=threading.Event();closed=threading.Event();calls=[];lock=threading.Lock()
            class Provider:
                def __init__(self,name):self.name=name
                def query(self,ip):
                    with lock:calls.append((self.name,ip))
                    if self.name=='ipqs':started.wait(2);release.wait(3)
                    return intel.ProviderResult(self.name,ip,'ok',normalized={'fraud_score':4})
            args=SimpleNamespace(history=str(Path(folder)/'h.jsonl'),intel_workers=2)
            with mock.patch.object(core,'make_default_providers',return_value=[Provider('ipqs'),Provider('scamalytics')]):
                enricher=core._IntelEnrichment(args)
            thread=None
            try:
                for ip in ('192.0.2.1','192.0.2.2','192.0.2.3'):enricher.submit_ip(ip)
                started.wait(2);queued=enricher.futures['192.0.2.3'];cancelled=threading.Event()
                queued.add_done_callback(lambda f:cancelled.set())
                def close():enricher.close();closed.set()
                thread=threading.Thread(target=close);thread.start();self.assertTrue(cancelled.wait(2))
                self.assertFalse(closed.is_set());release.set();thread.join(3)
                self.assertTrue(closed.is_set());self.assertEqual(len(calls),2)
                self.assertTrue(all(name=='ipqs' for name,ip in calls))
                self.assertEqual(set(enricher.values),{'192.0.2.1','192.0.2.2'})
                self.assertEqual(enricher.cache.get('ipqs','192.0.2.1').status,'cache_hit')
            finally:
                release.set()
                if thread:thread.join(3)
                enricher.close()


if __name__=='__main__':unittest.main()
