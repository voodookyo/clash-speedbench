"""Owned subprocess + real localhost API, production raw/history projections."""
from contextlib import contextmanager
import http.client
import json
from pathlib import Path
from queue import Queue
import re
import subprocess
import sys
import threading
import time
import unittest


@contextmanager
def fixture(*args):
    proc=subprocess.Popen([sys.executable,'-m','tests.ui_fixture_server','--port','0',*args],
        cwd=Path(__file__).resolve().parents[1],stdout=subprocess.PIPE,stderr=subprocess.PIPE,
        text=True,encoding='utf-8')
    ready=Queue()
    reader=threading.Thread(target=lambda:ready.put(proc.stdout.readline()),daemon=True);reader.start()
    port=None;token=None
    def request(method,path,body=None,authorized=True):
        conn=http.client.HTTPConnection('127.0.0.1',port,timeout=5)
        headers={}
        if body is not None:headers['Content-Type']='application/json';body=json.dumps(body)
        if token and authorized:headers['X-SpeedBench-Token']=token
        try:
            conn.request(method,path,body,headers);r=conn.getresponse();raw=r.read().decode('utf-8')
            return r.status,json.loads(raw) if r.getheader('Content-Type','').startswith('application/json') else raw
        finally:conn.close()
    try:
        line=ready.get(timeout=5);match=re.search(r'http://127\.0\.0\.1:(\d+)',line)
        if not match:raise AssertionError('Fixture did not announce owned loopback address: '+line)
        port=int(match.group(1));status,html=request('GET','/')
        if status!=200:raise AssertionError('Fixture page unavailable')
        token=re.search(r'<meta name="sb-token" content="([^"]+)">',html).group(1)
        yield request,html
    finally:
        if proc.poll() is None and port and token:
            try:request('POST','/api/quit',{})
            except (OSError,http.client.HTTPException):pass
        try:proc.wait(timeout=6)
        except subprocess.TimeoutExpired:
            proc.kill();proc.wait(timeout=3)
            raise AssertionError('Owned fixture failed to stop cleanly: '+proc.stderr.read())
        finally:
            proc.stdout.close();proc.stderr.close();reader.join(timeout=1)
        if proc.returncode!=0:raise AssertionError('Fixture exit '+str(proc.returncode))


def wait_job(request, job, *, terminal=True):
    deadline=time.monotonic()+5
    while time.monotonic()<deadline:
        status,value=request('GET','/api/jobs/'+job)
        if status!=200:raise AssertionError('Job unavailable')
        reached=value['status'] in ('completed','cancelled','failed') if terminal else bool(value.get('results'))
        if reached:
            return value
        time.sleep(.025)
    raise AssertionError('Fixture job did not reach expected boundary')


class UiFixtureScenariosTest(unittest.TestCase):
    def test_default_keeps_old_unknown_metadata_and_real_cancel_partial(self):
        with fixture() as (request,html):
            self.assertNotIn('/static/fixture-leak.js',html)
            self.assertEqual(request('GET','/api/history'),(200,[]))
            status,started=request('POST','/api/jobs',{'mode':'quick'})
            self.assertEqual(status,202);job=started['job_id']
            pending=wait_job(request,job,terminal=False)
            self.assertNotIn('metric_updated_at',pending['results'][0])
            self.assertEqual(request('POST','/api/jobs/'+job+'/cancel',{})[0],200)
            final=wait_job(request,job)
            self.assertEqual(final['status'],'cancelled');self.assertTrue(final['results'])
            self.assertTrue(request('GET','/api/tasks/'+job)[1]['partial'])

    def test_history_uses_real_raw_and_identity_ip_reputation_queries(self):
        with fixture('--history-fixture') as (request,html):
            status,history=request('GET','/api/history');self.assertEqual(status,200)
            self.assertEqual(len(history),3)
            first,last=history[0]['results'][0],history[-1]['results'][0]
            self.assertEqual(first['node_id'],last['node_id']);self.assertNotEqual(first['name'],last['name'])
            self.assertNotIn('metric_updated_at',history[-1]['results'][-1])
            node=request('GET','/api/node?node_id='+last['node_id'])[1]
            self.assertEqual(len(node['series']),3)
            self.assertEqual([r['exit_ip'] for r in node['ip_changes']],['203.0.113.10','192.0.2.20'])
            self.assertTrue(node['ip_reputation_changes'][1]['same_ip_reputation_worsened'])
            failed=request('GET','/api/node?node_id='+history[-1]['results'][2]['node_id'])[1]
            self.assertTrue(all(not r['intel_available'] and r['exit_ip'] is None for r in failed['ip_reputation_changes']))
            sources=request('GET','/api/sources/history')[1]
            self.assertEqual(next(s for s in sources if s['subscription_id']==last['subscription_ids'][0])['name'],'界面验证 fixture <订阅>')
            self.assertTrue(any(s['source_status']=='legacy_unknown' for s in sources))
            self.assertTrue(any(s['source_status']=='unknown' for s in sources))
            self.assertTrue(any(s.get('ambiguous_node_count') for s in sources))
            status,data=request('GET','/api/data-status')
            self.assertEqual(status,200)
            home=Path(data['data_home']);self.assertTrue(home.name.startswith('speedbench-ui-fixture-'))
            self.assertEqual(Path(data['history']['jsonl_path']).parent,home)
        self.assertFalse(home.exists(),'Owned temp history remained after fixture exit')

    def test_disconnected_empty_and_timeout_are_distinct_real_http_responses(self):
        for state in ('disconnected','empty','timeout'):
            with self.subTest(state=state),fixture('--controller-state',state) as (request,html):
                status,current=request('GET','/api/current');self.assertEqual(status,200)
                self.assertEqual(current['ok'],state=='empty')
                status,catalog=request('GET','/api/catalog')
                if state=='timeout':self.assertEqual(status,504)
                else:self.assertEqual(catalog['nodes'],[])

    def test_failed_and_partial_tasks_keep_results_in_production_task_store(self):
        for outcome,expected in (('failed','completed'),('partial','cancelled')):
            with self.subTest(outcome=outcome),fixture('--task-outcome',outcome) as (request,html):
                status,started=request('POST','/api/jobs',{'mode':'quick'});self.assertEqual(status,202)
                job=started['job_id'];final=wait_job(request,job)
                self.assertEqual(final['status'],expected);self.assertEqual(len(final['results']),3)
                if outcome=='failed':self.assertTrue(all(r['measurement_scope']['probe']=='failed' for r in final['results']))
                else:self.assertTrue(request('GET','/api/tasks/'+job)[1]['partial'])

    def test_leak_fixture_is_opt_in_and_production_evaluate_save_remains_authenticated(self):
        with fixture('--leak-fixture') as (request,html):
            self.assertGreater(html.index('/static/fixture-leak.js'),html.index('/static/app.js'))
            status,script=request('GET','/static/fixture-leak.js');self.assertEqual(status,200)
            self.assertNotIn('fetch(',script);self.assertNotIn('RTCPeerConnection',script)
            payload={'candidates':[],'exit_ipv4':'192.0.2.10','exit_ipv6':'2001:db8::10',
                'collection_complete':False,'policy_blocked':True,'client_environment':'browser'}
            self.assertEqual(request('POST','/api/leak/audit',payload,authorized=False)[0],403)
            status,saved=request('POST','/api/leak/audit',payload)
            self.assertEqual(status,200);self.assertEqual(saved['status'],'unknown')
            self.assertTrue(saved['persistence']['saved'])
            audits=request('GET','/api/leak/audits')[1]['audits']
            self.assertEqual(len(audits),1);self.assertEqual(audits[0]['details']['client_environment'],'browser')

    def test_synthetic_release_comparison_and_leak_warning_match_client_environment(self):
        for args,expected in (((), 'current'),(('--desktop-preferences',),'ahead')):
            with self.subTest(args=args),fixture('--leak-fixture',*args) as (request,html):
                self.assertIn('fixture 合成传输：无外部 IP/STUN 请求；不能判断实际泄漏。',html)
                status,result=request('POST','/api/releases/check',{})
                self.assertEqual(status,200);self.assertEqual(result['comparison'],expected)
