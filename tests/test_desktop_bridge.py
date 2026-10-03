import io
import os
import json
import sys
import tempfile
import subprocess
import http.client
from pathlib import Path
from unittest import mock
import unittest
import threading
import queue
import speedbench_web as web
from tests.web_server_case import WebServerCase

import speedbench_desktop as desktop


class DesktopBridgeTest(unittest.TestCase):
    def test_explicit_os_actions_are_bounded_aged_and_acknowledged(self):
        actions=desktop.DesktopActions()
        for bad in ({'action':'https://evil.example'},{'action':'releases','key':'CANARY'},[],{'action':'cmd.exe'}):
            with self.assertRaises(desktop.DesktopError):actions.request(bad)
        key=actions.request({'action':'browser_audit'})
        self.assertEqual(actions.result(key)['status'],'queued')
        self.assertEqual(actions.drain(),[{'action':'browser_audit','request_id':key}])
        self.assertEqual(actions.drain(),[])
        actions.finish(key,True);self.assertEqual(actions.result(key)['status'],'opened')
        for _ in range(8):actions.request({'action':'releases'})
        with self.assertRaises(desktop.DesktopError):actions.request({'action':'releases'})
        with mock.patch('speedbench_desktop.time.monotonic',return_value=10**15):
            self.assertEqual(actions.drain(),[])

    def test_control_is_bounded_and_unknown_commands_have_no_authority(self):
        self.assertEqual(desktop.read_control(io.BytesIO(b'{"command":"cancel"}\n')),'cancel')
        self.assertEqual(desktop.read_control(io.BytesIO(b'{"command":"exit"}\n')),'exit')
        self.assertIsNone(desktop.read_control(io.BytesIO()))
        acknowledged={'command':'action_result','request_id':'a'*32,'ok':False}
        self.assertEqual(desktop.read_control(io.BytesIO(json.dumps(acknowledged).encode()+b'\n')),acknowledged)
        for raw in (b'not-json\n',b'{"command":"exec","key":"CANARY"}\n',b'{"command":"exit","extra":1}\n'):
            self.assertEqual(desktop.read_control(io.BytesIO(raw)),'ignore')
        for raw in (b'x'*1_000_000,b'{"command":"exit"}'):
            stream=io.BytesIO(raw)
            with self.assertRaises(desktop.DesktopError):desktop.read_control(stream)
            self.assertLessEqual(stream.tell(),257)

    def test_real_private_bootstrap_dynamic_port_guard_and_parent_eof(self):
        with tempfile.TemporaryDirectory() as folder:
            env=dict(os.environ,SPEEDBENCH_HOME=folder)
            # No inherited third-party credentials belong in a CI fixture.
            for key in list(env):
                if key.startswith('SPEEDBENCH_IP') or key.startswith('SPEEDBENCH_SCAMALYTICS'):env.pop(key)
            proc=subprocess.Popen([sys.executable,'-u','speedbench_desktop.py'],stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env,text=True,encoding='utf-8')
            try:
                proc.stdin.write(json.dumps({'protocol':1,'parent_pid':os.getpid(),'nonce':'b'*64})+'\n');proc.stdin.flush()
                # Owned process is killed/reaped on every test exit, including
                # failed readiness assertions. No real controller is queried.
                received=queue.Queue(maxsize=1)
                threading.Thread(target=lambda:received.put(proc.stdout.readline(4097)),daemon=True).start()
                frame=json.loads(received.get(timeout=10))
                self.assertEqual(frame['nonce'],'b'*64)
                self.assertGreater(frame['port'],0)
                self.assertEqual(frame['pid'],proc.pid)
                # A separate legacy HTTP port is not a second task owner.
                competing=subprocess.run([sys.executable,'speedbench_web.py','--no-browser','--port','0'],
                    env=env,capture_output=True,timeout=10)
                self.assertEqual(competing.returncode,2)
                self.assertNotIn(frame['token'].encode(),competing.stdout+competing.stderr)
                conn=http.client.HTTPConnection('127.0.0.1',frame['port'],timeout=3)
                self.addCleanup(conn.close)
                conn.request('GET','/api/desktop/identity')
                response=conn.getresponse();self.assertEqual(response.status,403);response.read()
                conn.request('GET','/api/desktop/identity',headers={'X-SpeedBench-Token':frame['token']})
                response=conn.getresponse();self.assertEqual(response.status,200);body=response.read()
                self.assertNotIn(frame['token'].encode(),body);self.assertNotIn(frame['nonce'].encode(),body)
                conn.request('GET','/',headers={'Host':'evil.example'})
                response=conn.getresponse();self.assertEqual(response.status,403);response.read()
                proc.stdin.close();proc.stdin=None
                out,err=proc.communicate(timeout=10)
                self.assertEqual(proc.returncode,0,err)
                self.assertNotIn(frame['token'],out+err)
                self.assertNotIn(frame['nonce'],out+err)
                self.assertFalse((Path(folder)/'speedbench-history.jsonl').exists())
            finally:
                if proc.poll() is None:proc.kill();proc.communicate(timeout=5)
                for stream in (proc.stdin,proc.stdout,proc.stderr):
                    if stream:stream.close()

    def test_handshake_requires_parent_nonce_and_bounded_fields(self):
        data={'protocol':1,'nonce':'a'*64,'parent_pid':os.getppid()}
        self.assertEqual(desktop.validate_bootstrap(data)['nonce'],'a'*64)
        for bad in ({**data,'parent_pid':-1},{**data,'nonce':'bad'},{**data,'protocol':2},
                    {**data,'key':'CANARY-key'},[],{**data,'parent_pid':True}):
            with self.subTest(bad=bad),self.assertRaises(desktop.DesktopError):desktop.validate_bootstrap(bad)

    def test_private_pipe_frame_is_not_a_public_identity_response(self):
        identity=desktop.public_identity('fixture')
        self.assertEqual(identity['app_id'],'com.voodookyo.clash-speedbench')
        self.assertNotIn('nonce',identity)
        self.assertNotIn('token',identity)
        self.assertNotIn('key',str(identity))


class DesktopApiTest(WebServerCase):
    def test_compact_os_routes_require_auth_and_never_accept_urls_keys_or_paths(self):
        with mock.patch.object(web,'DESKTOP_ACTIONS',desktop.DesktopActions()):
            status,_=self.request('GET','/api/desktop/state');self.assertEqual(status,403)
            status,_=self.post_authorized('/api/desktop/actions',{'action':'releases','key':'CANARY'})
            self.assertEqual(status,400)
            status,raw=self.post_authorized('/api/desktop/actions',{'action':'dnsleaktest'})
            self.assertEqual(status,202);key=json.loads(raw)['request_id']
            status,raw=self.request('GET','/api/desktop/state',headers={'X-SpeedBench-Token':web.WEB_TOKEN})
            self.assertEqual(status,200);self.assertNotIn(b'CANARY',raw)
            self.assertIsNone(json.loads(raw)['job'])
            self.assertEqual(json.loads(raw)['actions'][0]['request_id'],key)
            self.assertNotIn('http',str(json.loads(raw)['actions']))

    def test_exit_flag_rejects_new_jobs_and_quit_uses_desktop_cleanup(self):
        event=threading.Event();event.set()
        with mock.patch.object(web,'DESKTOP_EXITING',event):
            status,_=self.post_authorized('/api/jobs',{'mode':'quick'})
            self.assertEqual(status,409);self.assertIsNone(web.JOBS.active_id())
        called=threading.Event();event.clear()
        with mock.patch.object(web,'DESKTOP_EXITING',event),mock.patch.object(web,'DESKTOP_SHUTDOWN',called.set):
            status,_=self.post_authorized('/api/quit',{})
            self.assertEqual(status,200);self.assertTrue(event.is_set());self.assertTrue(called.wait(1))
