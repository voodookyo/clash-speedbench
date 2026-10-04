from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import threading
from unittest import mock

import speedbench_web as web
from speedbench_owner import BackendLease
from tests.test_history_transfer import ledger
from tests.web_server_case import WebServerCase


class HistoryTransferApiTest(WebServerCase):
    def setUp(self):
        super().setUp()
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.home=Path(self.temp.name)/'home';self.source=Path(self.temp.name)/'旧目录 空格'
        self.home.mkdir();self.source.mkdir()
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        self.owner=self.stack.enter_context(BackendLease(self.home))
        for name,value in [('DATA_HOME',self.home),('HISTORY',self.home/'speedbench-history.jsonl'),('DATA_OWNER',self.owner)]:
            self.stack.enter_context(mock.patch.object(web,name,value))
        self.set_state(running=False,cleanup_incomplete=False,importing=False,import_failed=False)
        ledger(self.source,'2026-10-01T01:00:00',whitespace=True)

    def preview(self):
        code,raw=self.post_authorized('/api/history-import/preview',{'directory':str(self.source)})
        self.assertEqual(code,200,raw)
        return json.loads(raw)

    def test_preview_apply_status_and_rollback_over_real_http(self):
        preview=self.preview()
        self.assertEqual(preview['new_runs'],1)
        self.assertFalse((self.home/'speedbench-history.db').exists())
        code,raw=self.post_authorized('/api/history-import/apply',{'token':preview['token']})
        self.assertEqual(code,200,raw);result=json.loads(raw)
        code,raw=self.request('GET','/api/history-import/status',headers={'X-SpeedBench-Token':web.WEB_TOKEN})
        self.assertEqual((code,json.loads(raw)['backup_id']),(200,result['backup_id']))
        code,raw=self.request('GET','/api/latest')
        self.assertEqual((code,json.loads(raw)['ts']),(200,'2026-10-01T01:00:00'))
        code,raw=self.post_authorized('/api/history-import/rollback',{'backup_id':result['backup_id']})
        self.assertEqual(code,200,raw)

    def test_each_import_operation_requires_token_origin_and_strict_body(self):
        for endpoint,body in [('preview',{'directory':str(self.source)}),('apply',{'token':'x'}),('rollback',{'backup_id':'x'})]:
            path='/api/history-import/'+endpoint
            self.assertEqual(self.post_json(path,body)[0],403)
            self.assertEqual(self.post_authorized(path,body,headers={'Origin':'https://evil.example'})[0],403)
            self.assertEqual(self.post_authorized(path,dict(body,extra=True))[0],400)
        self.assertEqual(self.request('GET','/api/history-import/status')[0],403)
        self.assertFalse((self.home/'history-import-backups').exists())

    def test_idle_and_owned_directory_required_and_private_errors_are_masked(self):
        for state in ('running','cleanup_incomplete','importing','import_failed'):
            self.set_state(**{state:True})
            self.assertEqual(self.post_authorized('/api/history-import/preview',{'directory':str(self.source)})[0],409)
            self.set_state(**{state:False})
        with mock.patch.object(web,'DATA_OWNER',None):
            self.assertEqual(self.post_authorized('/api/history-import/preview',{'directory':str(self.source)})[0],409)
        code,raw=self.post_authorized('/api/history-import/preview',{'directory':str(self.source/'CANARY-private-nonexistent')})
        self.assertEqual(code,409);self.assertNotIn(b'CANARY',raw)

    def test_stale_preview_and_conflict_do_not_touch_destination(self):
        preview=self.preview();ledger(self.source,'2026-10-02T01:00:00')
        self.assertEqual(self.post_authorized('/api/history-import/apply',{'token':preview['token']})[0],409)
        self.assertFalse((self.home/'speedbench-history.jsonl').exists())
        ledger(self.home,'2026-10-01T01:00:00',speed=999)
        preview=self.preview();self.assertFalse(preview['can_apply'])
        self.assertEqual(self.post_authorized('/api/history-import/apply',{'token':preview['token']})[0],409)

    def test_import_reservation_blocks_new_tasks_and_data_writes_but_not_cancel(self):
        entered=threading.Event();release=threading.Event();response=[]
        preview=self.preview()
        service=web.history_transfer()
        apply=service.apply
        def held(*args):
            entered.set();release.wait(3);return apply(*args)
        with mock.patch.object(service,'apply',side_effect=held):
            thread=threading.Thread(target=lambda:response.append(self.post_authorized(
                '/api/history-import/apply',{'token':preview['token']})))
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                for path,body in [('/api/jobs',{}),('/api/preferences',{'sb_theme':'dark'}),('/api/config-root',{'root':''})]:
                    self.assertEqual(self.post_authorized(path,body)[0],409,path)
                self.assertEqual(self.request('GET','/api/latest')[0],409)
                self.assertEqual(self.post_authorized('/api/run/cancel')[0],200)
                self.assertEqual(self.request('GET','/api/run/status')[0],200)
            finally:release.set();thread.join(4)
        self.assertFalse(thread.is_alive());self.assertEqual(response[0][0],200,response)

    def test_pending_failed_import_blocks_data_and_new_tasks(self):
        self.set_state(import_failed=True)
        self.assertEqual(self.request('GET','/api/tasks')[0],409)
        self.assertEqual(self.post_authorized('/api/jobs')[0],409)
        self.assertEqual(self.post_authorized('/api/preferences',{'sb_theme':'dark'})[0],409)
        self.assertFalse((self.home/'ui-preferences.json').exists())

    def test_ip_intel_status_blocked_with_import_and_failure_states(self):
        entered=threading.Event();release=threading.Event();response=[]
        calls=[]
        preview=self.preview()
        service=web.history_transfer()
        apply=service.apply
        def held(*args):
            entered.set();release.wait(3);return apply(*args)
        def payload():
            calls.append(True);return {'ok':True,'providers':{}}
        with mock.patch.object(service,'apply',side_effect=held), \
                mock.patch.object(web,'_provider_status_payload',side_effect=payload):
            thread=threading.Thread(target=lambda:response.append(self.post_authorized(
                '/api/history-import/apply',{'token':preview['token']})))
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                code,raw=self.request('GET','/api/ip-intel/status')
                self.assertEqual(code,409,raw)
                self.assertEqual(calls,[])
                self.assertEqual(self.post_authorized('/api/run/cancel')[0],200)
                self.assertEqual(self.request('GET','/api/run/status')[0],200)
            finally:release.set();thread.join(4)
            self.assertFalse(thread.is_alive())
            self.assertEqual(response[0][0],200,response)
            self.assertEqual(self.request('GET','/api/ip-intel/status')[0],200)
            self.assertEqual(calls,[True])
            self.set_state(import_failed=True)
            self.assertEqual(self.request('GET','/api/ip-intel/status')[0],409)
            self.assertEqual(calls,[True])

    def test_ip_intel_status_reads_database_under_existing_db_mutex(self):
        entered=threading.Event();probed=threading.Event();proceed=threading.Event()
        acquired=[];response=[]
        def payload():
            entered.set()
            def contender():
                held=web._DB_SYNC_LOCK.acquire(timeout=0.5)
                acquired.append(held)
                if held:web._DB_SYNC_LOCK.release()
                probed.set()
            probe=threading.Thread(target=contender);probe.start();probe.join(2)
            proceed.wait(3)
            return {'ok':True,'providers':{}}
        with mock.patch.object(web,'_provider_status_payload',side_effect=payload):
            thread=threading.Thread(target=lambda:response.append(self.request('GET','/api/ip-intel/status')))
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertTrue(probed.wait(2))
                self.assertEqual(acquired,[False])
            finally:
                proceed.set();thread.join(4)
        self.assertFalse(thread.is_alive())
        self.assertEqual((response[0][0],json.loads(response[0][1])['ok']),(200,True))
