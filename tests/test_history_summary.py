import json
from pathlib import Path
import subprocess
import tempfile
import unittest

import clash_speedbench as cli
import speedbench_web as web
from tests import test_web_api_node as api
from tests.test_resume_js import NODE
from tests import test_task_ui_js as task_ui


class HistorySummaryApiTest(api.WebApiCase):
    def test_saved_task_metadata_is_correlated_by_job_not_timestamp_and_raw_is_unchanged(self):
        job='job_'+'a'*32
        raw=api.make_record(api.ts_before(),[api.make_result('fixture')])
        raw['task']=dict(job_id=job,mode='quick',target_profile='daily',partial=False,status='completed',selected_node_count=3)
        self.write_history([raw])
        web.speedbench_db.import_jsonl(self.hist.with_suffix('.db'),self.hist)
        for key,elapsed,bytes_ in ((job,1200,500),('job_'+'b'*32,9000,9999)):
            web.speedbench_db.save_task(self.hist.with_suffix('.db'),dict(job_id=key,status='failed',
                config={'mode':'quick'},started_at=raw['ts'],finished_at=raw['ts'],elapsed_ms=elapsed,
                results=[],metrics={'download':dict(bytes=bytes_,attempts=1,successes=1,duration_ms=100)}))
        status,rows=self.get_json('/api/history');self.assertEqual(status,200)
        self.assertEqual(rows[0]['task'],dict(mode='quick',target_profile='daily',partial=True,
            status='failed',selected_node_count=3,elapsed_ms=1200,downloaded_bytes=500))
        self.assertEqual(self.get_json('/api/latest'),(200,raw))

    def test_unstored_task_does_not_manufacture_duration_bytes_or_accept_raw_arbitrary_fields(self):
        raw=api.make_record(api.ts_before(),[])
        raw['task']=dict(mode='ip',target_profile='ip',partial=False,status='<script>',selected_node_count=True,
            elapsed_ms=999,downloaded_bytes=999,token='CANARY')
        self.write_history([raw]);status,rows=self.get_json('/api/history')
        self.assertEqual(status,200)
        self.assertEqual(rows[0]['task'],dict(mode='ip',target_profile='ip',partial=False))


class HistoryScopeRawTest(unittest.TestCase):
    def test_scope_count_is_additive_and_not_a_budget_or_boolean(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'fixture.jsonl'
            for value in (3,True,-1):
                self.assertTrue(cli.append_history([],path,10,1,None,task=dict(mode='quick',selected_node_count=value)))
            rows=[json.loads(line)['task'] for line in path.read_text().splitlines()]
        self.assertEqual(rows[0]['selected_node_count'],3)
        self.assertNotIn('selected_node_count',rows[1]);self.assertNotIn('selected_node_count',rows[2])


@unittest.skipUnless(NODE,'Node unavailable')
class HistorySummaryJsTest(unittest.TestCase):
    def describe(self,rec):
        module=Path(__file__).resolve().parents[1]/'web'/'history-view.js'
        code='const h=require('+json.dumps(str(module))+');console.log(JSON.stringify(h.describe('+json.dumps(rec)+')));'
        proc=subprocess.run([NODE,'-e',code],capture_output=True,text=True,timeout=5)
        self.assertEqual(proc.returncode,0,proc.stderr)
        return json.loads(proc.stdout)

    def test_old_and_invalid_metadata_remain_unknown(self):
        for rec in ({'results':[{'latency_ms':10}]},{'task':{'mode':{},'status':'<img>',
                'partial':'false','elapsed_ms':-1,'downloaded_bytes':'10','selected_node_count':True},'results':[]}):
            with self.subTest(rec=rec):
                result=self.describe(rec)
                self.assertIn('未知',result['mode']);self.assertIn('未知',result['status'])
                self.assertEqual((result['elapsed'],result['traffic']),('未知','未知'))
                self.assertIn('原范围未知',result['range']);self.assertNotIn('<img>',str(result))

    def test_scope_partial_actual_bytes_and_metric_states_are_distinct(self):
        result=self.describe(dict(task=dict(mode='quick',target_profile='daily',selected_node_count=4,
            partial=True,status='cancelled',elapsed_ms=1250,downloaded_bytes=250000),results=[
                {'measurement_scope':dict(probe='completed',bandwidth='completed',intel='not_requested')},
                {'measurement_scope':dict(probe='failed',bandwidth='not_selected',intel='cancelled')},
                {}]))
        self.assertEqual(result['mode'],'快速 · 日常')
        self.assertEqual(result['status'],'已取消 · 部分结果')
        self.assertEqual(result['range'],'已返回 3 / 4 节点')
        self.assertEqual((result['elapsed'],result['traffic']),('1.25s','0.25 MB（仅已报告实际字节）'))
        self.assertIn('探测 1/3 完成',result['coverage']);self.assertIn('失败 1',result['coverage'])
        self.assertIn('未请求 1',result['coverage']);self.assertIn('未知 1',result['coverage'])

    def test_real_live_catalog_distinguishes_unloaded_ambiguous_unknown_and_only_selects_scope(self):
        out=task_ui.TaskUiJsTest.run_app(self,"""
          (async()=>{await new Promise(r=>setTimeout(r,0));
            const a='subscription_v2_'+'a'.repeat(32),b='subscription_v2_'+'b'.repeat(32);
            sourceCatalog={status:'ok',sources:[{subscription_id:a,name:'fixture <A> " 🇭🇰',loaded:true},
              {subscription_id:b,name:'fixture B',loaded:true},{subscription_id:'unloaded',name:'fixture 未加载',loaded:false}],
              nodes:[{subscription_ids:[a],source_status:'verified'},{subscription_ids:[a,b],source_status:'ambiguous'},
                {subscription_ids:[],source_status:'unknown'}]};
            renderLiveSubsCatalog();const html=__el('subs-catalog-tbody').innerHTML;
            let focus=0,sent=0;post=async()=>{sent++;};__el('f-source').focus=()=>focus++;
            __el('subs-catalog-tbody').__listeners.click[0]({target:{closest:()=>({dataset:{testSource:a}})}});
            console.log(JSON.stringify({html,value:__el('f-source').value,hash:window.location.hash,focus,sent}));})();
        """)
        self.assertIn('&lt;A&gt;',out['html']);self.assertNotIn('<A>',out['html'])
        self.assertIn('2（1 多来源）',out['html']);self.assertIn('来源未知',out['html'])
        self.assertNotIn('data-test-source="unloaded"',out['html'])
        self.assertEqual(out['value'],'subscription_v2_'+'a'*32)
        self.assertEqual((out['hash'],out['focus'],out['sent']),('#/nodes',1,0))

    def test_empty_scope_prevents_new_job_and_recommendation_message_follows_profile(self):
        out=task_ui.TaskUiJsTest.run_app(self,"""
          (async()=>{await new Promise(r=>setTimeout(r,0));sourceCatalog={status:'ok',nodes:[]};
            let sent=0,messages=[];post=async()=>{sent++;};toast=x=>messages.push(x);await startTask();
            currentProfile='download';showTask({started_at:'fixture',status:'completed',config:{mode:'quick'},metrics:{},
              results:[{name:'fixture',status:'ok',latency_ms:10,jitter_ms:2,measurement_scope:{probe:'completed',bandwidth:'not_requested'}}]});
            const download=__el('latest-meta').textContent;setProfile('daily');const daily=__el('latest-meta').textContent;
            console.log(JSON.stringify({sent,messages,download,daily}));})();
        """)
        self.assertEqual(out['sent'],0);self.assertIn('Verge 加载订阅',out['messages'][0])
        self.assertIn('没有可推荐结果',out['download']);self.assertIn('已测范围内推荐',out['daily'])
