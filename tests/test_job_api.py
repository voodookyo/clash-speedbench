import io
import json
import tempfile
from pathlib import Path
from unittest import mock
import speedbench_web as web
from speedbench_tasks import resolve_config
from speedbench_progress import ProgressEmitter
from tests.web_server_case import WebServerCase


class JobApiTest(WebServerCase):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patch = mock.patch.object(web,'HISTORY',Path(self.temp.name)/'history.jsonl')
        patch.start()
        self.addCleanup(patch.stop)
        self.set_state(running=False,job_id=None,cancel_requested=False)

    def create(self):
        return web.JOBS.create(resolve_config({'mode':'quick'}))

    def test_new_job_is_queued_before_dispatch_and_requires_token(self):
        self.assertEqual(self.post_json('/api/jobs',{'mode':'quick'})[0],403)
        with mock.patch.object(web,'run_benchmark'):
            status,body = self.post_authorized('/api/jobs',{'mode':'quick'})
            self.assertEqual(status,202)
            value = json.loads(body)
            job = web.JOBS.snapshot(value['job_id'])
            self.assertEqual(job['status'],'queued')
            self.assertEqual(job['config']['mode'],'quick')
            self.assertEqual(self.post_authorized('/api/jobs',{})[0],409)

    def test_snapshot_host_validation_and_incremental_cursor(self):
        job = self.create()
        web.JOBS.publish(job,'node_probe',node_id='n',payload={'result':{'name':'node','latency_ms':12}})
        self.assertEqual(self.request('GET','/api/jobs/'+job,headers={'Host':'evil.example'})[0],403)
        status,body = self.request('GET','/api/jobs/'+job+'/events?since_seq=1')
        self.assertEqual(status,200)
        self.assertEqual(json.loads(body)['events'][0]['type'],'node_probe')
        status,body = self.request('GET','/api/jobs/'+job)
        self.assertEqual(json.loads(body)['results'][0]['latency_ms'],12)
        self.assertEqual(self.request('GET','/api/jobs/'+job+'/events?since_seq=-1')[0],400)

    def test_sse_terminal_stream_and_last_event_id(self):
        job = self.create()
        web.JOBS.transition(job,'failed')
        status,headers,body = self.request_full('GET','/api/jobs/'+job+'/stream',headers={'Last-Event-ID':'1'})
        self.assertEqual(status,200)
        self.assertIn('text/event-stream',headers['content-type'])
        self.assertIn(b'id: 2',body)
        self.assertIn(b'job_failed',body)
        self.assertNotIn(b'job_started',body)

    def test_expired_cursor_sse_sends_snapshot_not_incomplete_delta(self):
        web.JOBS = web.JobStore(event_limit=1)
        job = self.create()
        web.JOBS.publish(job,'node_probe',node_id='n',payload={'result':{'latency_ms':12}})
        web.JOBS.transition(job,'failed')
        status,_headers,body = self.request_full('GET','/api/jobs/'+job+'/stream')
        self.assertEqual(status,200)
        self.assertIn(b'event: snapshot',body)
        self.assertIn(b'latency_ms',body)

    def test_cancel_queued_job_does_not_spawn_or_cancel_another_job(self):
        job = self.create()
        self.set_state(running=True,proc=None,job_id=job,cancel_requested=False)
        status,body = self.post_authorized('/api/jobs/'+job+'/cancel',{})
        self.assertTrue(json.loads(body)['ok'])
        self.assertEqual(web.JOBS.snapshot(job)['status'],'cancelling')
        self.assertEqual(self.post_authorized('/api/jobs/job_'+'0'*32+'/cancel',{})[0],409)

    def test_runner_consumes_typed_records_ignores_duplicate_seq_and_redacts(self):
        job = self.create()
        stream = io.StringIO()
        e = ProgressEmitter(job,stream)
        e.emit('phase_started','preparing')
        e.emit('phase_started','probing')
        e.emit('node_probe','probing','node',{'result':{'name':'节点','latency_ms':12,'raw':'CANARY'}})
        duplicate = stream.getvalue().splitlines()[-1]
        e.emit('phase_started','enriching')
        e.emit('phase_started','finalizing')
        proc = mock.Mock(stdout=iter(stream.getvalue().splitlines()+[duplicate]))
        proc.wait.return_value = 0
        self.set_state(job_id=job,cancel_requested=False)
        with mock.patch.object(web,'connect_controller'), mock.patch.object(web,'sync_db'), \
                mock.patch.object(web.subprocess,'Popen',return_value=proc):
            web.run_benchmark({'mode':'quick','_job_id':job})
        snapshot = web.JOBS.snapshot(job)
        self.assertEqual(snapshot['status'],'completed')
        self.assertEqual(len(snapshot['results']),1)
        self.assertNotIn('CANARY',json.dumps(snapshot))
        self.assertFalse(web.STATE['running'])

    def test_queued_cancel_survives_runner_start(self):
        job = self.create()
        self.set_state(running=True,job_id=job,cancel_requested=True,proc=None)
        web.JOBS.transition(job,'cancelling')
        with mock.patch.object(web,'connect_controller'),mock.patch.object(web,'sync_db'), \
                mock.patch.object(web.subprocess,'Popen') as popen:
            web.run_benchmark({'mode':'quick','_job_id':job})
        popen.assert_not_called()
        self.assertEqual(web.JOBS.snapshot(job)['status'],'cancelled')
        self.assertFalse(any('启动测速失败' in line for line in web.STATE['lines']))

    def test_child_cancel_code_without_parent_request_is_cancelled_not_failed(self):
        job=self.create();self.set_state(job_id=job,cancel_requested=False)
        stream=io.StringIO();emitter=ProgressEmitter(job,stream)
        emitter.emit('phase_started','probing')
        emitter.emit('node_probe','probing','fixture',{'result':{'name':'fixture','latency_ms':12}})
        proc=mock.Mock(stdout=iter(stream.getvalue().splitlines()));proc.wait.return_value=130
        with mock.patch.object(web,'connect_controller'),mock.patch.object(web,'sync_db'), \
                mock.patch.object(web.subprocess,'Popen',return_value=proc):
            web.run_benchmark({'mode':'quick','_job_id':job})
        snapshot=web.JOBS.snapshot(job)
        self.assertEqual(snapshot['status'],'cancelled');self.assertTrue(snapshot['partial'])
        self.assertEqual(len(snapshot['results']),1);self.assertFalse(web.STATE['running'])

    def test_cleanup_failure_during_requested_cancel_is_failed_not_successful_cancel(self):
        job=self.create();self.set_state(running=True,job_id=job,cancel_requested=False)
        web.JOBS.publish(job,'node_probe',node_id='fixture',payload={'result':{'name':'fixture','latency_ms':12}})
        proc=mock.Mock(stdout=iter(()));proc.wait.return_value=3
        def spawn(*args,**kwargs):
            self.set_state(cancel_requested=True);return proc
        with mock.patch.object(web,'connect_controller'),mock.patch.object(web,'sync_db'), \
             mock.patch.object(web.subprocess,'Popen',side_effect=spawn):
            web.run_benchmark({'mode':'quick','_job_id':job})
        snapshot=web.JOBS.snapshot(job)
        self.assertEqual(snapshot['status'],'failed');self.assertTrue(snapshot['partial'])
        self.assertEqual(len(snapshot['results']),1)
        self.assertTrue(web.STATE['cleanup_incomplete'])
        with mock.patch.object(web,'run_benchmark') as run:
            code,raw=self.post_authorized('/api/jobs',{'mode':'quick'})
        self.assertEqual(code,409);run.assert_not_called()

    def test_cancel_between_preflight_and_popen_is_delivered_to_child(self):
        job = self.create()
        proc = mock.Mock(stdout=iter(()))
        proc.wait.return_value = 0
        self.set_state(job_id=job,cancel_requested=False,proc=None)
        def spawn(*args,**kwargs):
            web.cancel_benchmark()  # Popen has not published its handle yet.
            self.assertEqual(kwargs['env']['SPEEDBENCH_CANCEL_PRIMED'],'1')
            return proc
        with mock.patch.object(web,'connect_controller'),mock.patch.object(web,'sync_db'), \
                mock.patch.object(web.subprocess,'Popen',side_effect=spawn):
            web.run_benchmark({'mode':'quick','_job_id':job})
        self.assertTrue(web.CANCEL_FILE.exists())
        self.assertEqual(web.JOBS.snapshot(job)['status'],'cancelled')
