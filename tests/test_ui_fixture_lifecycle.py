import http.client
from http.server import BaseHTTPRequestHandler
from pathlib import Path
import tempfile
import threading
import unittest

from tests.ui_fixture_server import FixtureServer


class UiFixtureLifecycleTest(unittest.TestCase):
    def test_fixture_close_waits_for_inflight_file_handles_before_temp_cleanup(self):
        entered=threading.Event();release=threading.Event();closed=threading.Event()
        errors=[]
        with tempfile.TemporaryDirectory(prefix='speedbench-fixture-close-') as folder:
            path=Path(folder)/'fixture-only';path.write_bytes(b'fixture')
            class Handler(BaseHTTPRequestHandler):
                def log_message(self,*args):pass
                def do_GET(self):
                    with path.open('rb'):
                        entered.set()
                        if not release.wait(5):raise TimeoutError('Fixture did not release handle')
                    self.send_response(200);self.end_headers();self.wfile.write(b'fixture')
            server=FixtureServer(('127.0.0.1',0),Handler)
            serving=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01})
            serving.start()
            def fetch():
                connection=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5)
                try:
                    connection.request('GET','/');response=connection.getresponse()
                    if response.status!=200 or response.read()!=b'fixture':errors.append('Response mismatch')
                except Exception as error:errors.append(type(error).__name__)
                finally:connection.close()
            request=threading.Thread(target=fetch);request.start()
            def close():server.server_close();closed.set()
            closing=threading.Thread(target=close)
            try:
                self.assertTrue(entered.wait(2));server.shutdown();closing.start()
                self.assertFalse(closed.wait(.05),'Closed before the owned request released its file')
                release.set();self.assertTrue(closed.wait(2));request.join(2)
                self.assertEqual(errors,[])
            finally:
                release.set();server.shutdown();server.server_close()
                serving.join(2);request.join(2)
                if closing.ident is not None:closing.join(2)
        self.assertFalse(path.parent.exists())
