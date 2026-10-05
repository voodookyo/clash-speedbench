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
        proc=subprocess.run([NODE,'-e',code],capture_output=True,text=True,encoding='utf-8',timeout=5)
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
            currentProfile='download';showTask({started_at:'fixture',status:'completed',config:{mode:'ip'},metrics:{},
              milestones:{first_recommendation:0},
              results:[{name:'fixture',status:'ok',latency_ms:10,jitter_ms:2,ip_quality_score:70,ip_grade:'B',
                measurement_scope:{mode:'ip',probe:'completed',bandwidth:'not_requested'}}]});
            const download=__el('latest-meta').textContent;setProfile('daily');const daily=__el('latest-meta').textContent;
            console.log(JSON.stringify({sent,messages,download,daily}));})();
        """)
        self.assertEqual(out['sent'],0);self.assertIn('Verge 加载订阅',out['messages'][0])
        self.assertIn('没有可推荐结果',out['download']);self.assertIn('已测范围内推荐',out['daily'])

    def test_subscription_name_observations_keep_reverts_gaps_and_source_identity(self):
        out=task_ui.TaskUiJsTest.run_app(self,"""
          (async()=>{await new Promise(r=>setTimeout(r,0));
            const a='subscription_v2_'+'a'.repeat(32),b='subscription_v2_'+'b'.repeat(32);
            subsData=[{selection_key:a,subscription_id:a,provider:'当前名'},
              {selection_key:b,subscription_id:b,provider:'当前名'},
              {selection_key:'legacy',source_status:'legacy_unknown',provider:'当前名'},
              {selection_key:'unknown',source_status:'unknown',provider:'来源未知'}];
            const series=[{ts:'t1',name_snapshot:'旧名 <img src=x> " 🇭🇰'},
              {ts:'t2',name_snapshot:'旧名 <img src=x> " 🇭🇰'},
              {ts:'t3',name_snapshot:'当前名'}, {ts:'t4'},
              {ts:'t5',name_snapshot:'当前名'}, {ts:'t6',name_snapshot:'旧名 <img src=x> " 🇭🇰'}];
            let urls=[];getJSON=async url=>{urls.push(url);return url.includes(a)?series:
              url.includes(b)?[{ts:'t7',name_snapshot:'第二来源旧名'}]:series;};
            histLoaded=true;histData=[];drawSubsChart=()=>{};
            await selectSub(a);const first={text:__el('subs-name-history').textContent,html:__el('subs-name-history').innerHTML};
            await selectSub(b);const second=__el('subs-name-history').textContent;
            await selectSub('legacy');const legacy=__el('subs-name-history').textContent;
            await selectSub('unknown');const unknown=__el('subs-name-history').textContent;
            console.log(JSON.stringify({first,second,legacy,unknown,urls}));})();
        """)
        text=out['first']['text']
        self.assertIn('测速观测时间',text)
        self.assertEqual(text.count('旧名 <img src=x> " 🇭🇰'),2)
        self.assertNotIn('t2',text);self.assertIn('t4 · 名称未知',text)
        self.assertLess(text.index('t1'),text.index('t3'))
        self.assertLess(text.index('t3'),text.index('t6'))
        self.assertEqual(out['first']['html'],'')
        self.assertIn('第二来源旧名',out['second']);self.assertNotIn('t1',out['second'])
        for key in ('legacy','unknown'):
            self.assertIn('缺少稳定 ID',out[key]);self.assertNotIn('t1',out[key])
        self.assertEqual(len(out['urls']),3)
        self.assertTrue(out['urls'][0].startswith('/api/source?subscription_id='))
        self.assertIn('/api/subscription?name=',out['urls'][2])

    def test_subscription_name_request_cannot_replace_new_selection_or_day_range(self):
        out=task_ui.TaskUiJsTest.run_app(self,"""
          (async()=>{await new Promise(r=>setTimeout(r,0));
            const a='subscription_v2_'+'a'.repeat(32),b='subscription_v2_'+'b'.repeat(32);
            subsData=[{selection_key:a,subscription_id:a,provider:'A'},
              {selection_key:b,subscription_id:b,provider:'B'}];
            histLoaded=true;histData=[];drawSubsChart=()=>{};
            let pending=[];getJSON=url=>new Promise((resolve,reject)=>pending.push({url,resolve,reject}));
            const old=selectSub(a);const fresh=selectSub(b);
            pending[1].resolve([{ts:'fresh',name_snapshot:'B 新名'}]);await fresh;
            pending[0].reject(new Error('stale'));await old;
            const chosen=__el('subs-name-history').textContent;
            subsDays=7;const week=selectSub(b);subsDays=30;const month=selectSub(b);
            pending[3].resolve([{ts:'month',name_snapshot:'B 月度名'}]);await month;
            pending[2].resolve([{ts:'week',name_snapshot:'B 旧范围名'}]);await week;
            const ranged=__el('subs-name-history').textContent;
            const empty=selectSub(b);pending[4].resolve([]);await empty;
            const noSnapshots=__el('subs-name-history').textContent;
            const failed=selectSub(b);pending[5].reject(new Error('fixture'));await failed;
            console.log(JSON.stringify({chosen,ranged,noSnapshots,failed:__el('subs-name-history').textContent}));})();
        """)
        self.assertIn('B 新名',out['chosen']);self.assertNotIn('A',out['chosen'])
        self.assertIn('B 月度名',out['ranged']);self.assertNotIn('旧范围名',out['ranged'])
        self.assertIn('没有名称快照',out['noSnapshots'])
        self.assertIn('读取名称记录失败',out['failed']);self.assertIn('刷新',out['failed'])

    def test_latest_meta_uses_task_mode_and_valid_sample_params_only(self):
        base={'ts':'2026-10-05T00:00:00','results':[{'latency_ms':10,'median_mbps':50,'score':99}]}
        cases=[
            (dict(base,mb=None,rounds=None,task={'mode':'ip'}),'不请求带宽'),
            (dict(base,mb=10,rounds=1,task={'mode':'ip'}),'不请求带宽'),
            (dict(base,mb=10,rounds=1),'10MB×1轮'),
            (dict(base,mb=10,rounds=1,task={'mode':'quick'}),'10MB×1轮'),
            (dict(base,mb=10.5,rounds=2),'10.5MB×2轮'),
            (dict(base),'带宽样本参数未知'),
            (dict(base,mb=None,rounds=1),'带宽样本参数未知'),
            (dict(base,mb=10),'带宽样本参数未知'),
            (dict(base,mb='10',rounds=1),'带宽样本参数未知'),
            (dict(base,mb=True,rounds=1),'带宽样本参数未知'),
            (dict(base,mb=0,rounds=1),'带宽样本参数未知'),
            (dict(base,mb=-5,rounds=1),'带宽样本参数未知'),
            (dict(base,mb=10,rounds=0),'带宽样本参数未知'),
            (dict(base,mb=10,rounds=1.5),'带宽样本参数未知'),
            (dict(base,mb=10,rounds='1'),'带宽样本参数未知'),
        ]
        driver="""
          (async()=>{await new Promise(r=>setTimeout(r,0));
            const recs=__CASES__;const texts=[];
            for(const rec of recs){
              getJSON=async url=>url==='/api/latest'?rec:{};
              await loadLatest();
              texts.push(__el('latest-meta').textContent);
            }
            console.log(JSON.stringify(texts));})();
        """.replace('__CASES__',json.dumps([rec for rec,_ in cases],ensure_ascii=False))
        out=task_ui.TaskUiJsTest.run_app(self,driver)
        for (_,suffix),text in zip(cases,out):
            self.assertEqual(text,f'上次测速：2026-10-05T00:00:00 · 1 个节点 · {suffix}')
