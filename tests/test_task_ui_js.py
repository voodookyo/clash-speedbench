import json
import subprocess
import unittest
from pathlib import Path
from tests.test_resume_js import NODE
from tests.test_resume_js import STUB_JS, APP_JS
import tempfile

MODULE = Path(__file__).resolve().parents[1]/'web'/'tasks.js'


@unittest.skipUnless(NODE,'Node unavailable')
class TaskUiJsTest(unittest.TestCase):
    def test_table_and_region_recommendations_prefer_measured_coverage(self):
        out=self.run_app("""
          (async()=>{await new Promise(r=>setTimeout(r,0));currentProfile='daily';
            const rows=[{name:'only-probe',latency_ms:1,jitter_ms:1,score:100,
              measurement_scope:{mode:'quick',bandwidth:'not_selected'}},
              {name:'measured',latency_ms:150,jitter_ms:50,median_mbps:10,score:30,
              measurement_scope:{mode:'quick',bandwidth:'completed'}}];
            latestData={results:rows};sortRows(rows,'score',false);renderBoard();
            const desc=rows.map(r=>r.name);sortRows(rows,'score',true);
            console.log(JSON.stringify({order:desc,asc:rows.map(r=>r.name),board:__el('board-body').innerHTML}));})();
        """)
        self.assertEqual(out['order'],['measured','only-probe'])
        self.assertEqual(out['asc'],['measured','only-probe'])
        self.assertLess(out['board'].index('measured'),out['board'].index('only-probe'))

    def test_history_champion_uses_saved_target_and_keeps_legacy_overall(self):
        out=self.run_app("""
          (async()=>{await new Promise(r=>setTimeout(r,0));const rows=[
            {name:'overall',score:90,median_mbps:10},{name:'download',score:40,median_mbps:180}];
            console.log(JSON.stringify({legacy:championOf({results:rows}).name,
              saved:championOf({results:rows,task:{target_profile:'download'}}).name}));})();
        """)
        self.assertEqual(out,{'legacy':'overall','saved':'download'})

    def test_serial_choice_waits_for_every_explicit_confirmation(self):
        out=self.run_app("""
          (async()=>{await new Promise(r=>setTimeout(r,0));
            __el('f-mode').value='legacy';__el('f-target').value='daily';__el('f-rounds').value='1';
            sourceCatalog={nodes:[{runtime_name:'fixture',node_id:'node_v2_'+'a'.repeat(32),identity_strength:'strong'}]};
            currentGroup='stale';currentNode='stale';
            let pending,confirmations=[],sent=[];
            confirmModal=(message,yes)=>{confirmations.push(message);pending=yes;};
            post=async(url,body)=>{sent.push({url,body:{...body}});return {ok:false,msg:'fixture'};};
            await startTask();const before=sent.length;
            pending=null;await startTask();const afterCancel=sent.length;
            pending();await new Promise(r=>setTimeout(r,0));
            await startTask();const thirdAwait=sent.length;
            console.log(JSON.stringify({before,afterCancel,thirdAwait,confirmations,sent}));})();
        """,{'/api/current':{'ok':True,'group':'Main','now':'fixture'}})
        self.assertEqual((out['before'],out['afterCancel'],out['thirdAwait']),(0,0,1))
        self.assertEqual(len(out['confirmations']),3)
        self.assertTrue(all('GLOBAL' in text and 'Main = fixture' in text for text in out['confirmations']))
        body=out['sent'][0]['body'];self.assertEqual(body['mode'],'legacy')
        self.assertEqual(body['workers'],1);self.assertIs(body['allow_serial'],True)
        self.assertEqual(body['node_ids'],['node_v2_'+'a'*32])

    def test_serial_confirmation_freezes_identity_even_if_subscription_expands(self):
        out=self.run_app("""
          (async()=>{await new Promise(r=>setTimeout(r,0));
            __el('f-mode').value='legacy';__el('f-source').value='subscription_v2_'+'b'.repeat(32);
            const original={node_id:'node_v2_'+'a'.repeat(32),identity_strength:'strong',
              subscription_ids:[__el('f-source').value]};sourceCatalog={nodes:[original]};
            let pending,sent=[];confirmModal=(message,yes)=>{pending=yes;};
            post=async(url,body)=>{sent.push({...body});return {ok:false};};
            await startTask();sourceCatalog.nodes.push({...original,node_id:'node_v2_'+'c'.repeat(32)});
            pending();await new Promise(r=>setTimeout(r,0));console.log(JSON.stringify(sent));})();
        """)
        self.assertEqual(out[0]['node_ids'],['node_v2_'+'a'*32])
        self.assertEqual(out[0]['subscription_ids'],['subscription_v2_'+'b'*32])

    def test_serial_scope_with_unknown_identity_or_no_nodes_cannot_be_confirmed(self):
        out=self.run_app("""
          (async()=>{await new Promise(r=>setTimeout(r,0));__el('f-mode').value='legacy';
            let confirms=0,sent=0;confirmModal=()=>{confirms++;};post=async()=>{sent++;};
            for(const nodes of [[],[{node_id:'node_v2_'+'a'.repeat(32),identity_strength:'weak'}]]){
              sourceCatalog={nodes};await startTask();}
            console.log(JSON.stringify({confirms,sent}));})();
        """)
        self.assertEqual(out,{'confirms':0,'sent':0})

    def test_serial_confirmation_does_not_present_failed_current_refresh_as_current(self):
        out=self.run_app("""
          (async()=>{await new Promise(r=>setTimeout(r,0));__el('f-mode').value='legacy';
            sourceCatalog={nodes:[{node_id:'node_v2_'+'a'.repeat(32),identity_strength:'strong'}]};
            currentGroup='stale-group';currentNode='stale-node';
            getJSON=async()=>{throw Error('offline');};let text='';confirmModal=message=>{text=message;};
            await startTask();console.log(JSON.stringify(text));})();
        """)
        self.assertNotIn('stale-',out);self.assertIn('尚未确认',out)

    def test_serial_budget_displays_all_selected_nodes_not_top_fifteen(self):
        out=self.run_app("""
          (async()=>{await new Promise(r=>setTimeout(r,0));
            taskConfig={modes:{legacy:{bandwidth:true,top_n:15,mb:null,rounds:1,multi:false}}};
            sourceCatalog={nodes:Array.from({length:20},(_,i)=>({node_id:'n'+i}))};
            __el('f-mode').value='legacy';__el('f-rounds').value='1';
            updateTaskBudget();console.log(JSON.stringify(__el('task-budget').textContent));})();
        """)
        self.assertIn('精测最多 20 节点',out);self.assertIn('1920 MB',out)

    def test_result_metadata_details_show_updates_states_count_and_unknown(self):
        out=self.run_app("""
          (async()=>{await new Promise(r=>setTimeout(r,0));
            const legacy=detailHtml({name:'old',latency_ms:12});
            const fresh=detailHtml({name:'new',latency_ms:12,median_mbps:20,probe_loss_pct:0,
              measured_metric_count:4,metric_updated_at:{network:1700000000000,ip_grade:1700000000001},
              measurement_scope:{probe:'completed',bandwidth:'not_selected',intel:'failed'}});
            const malicious=detailHtml({name:'x',measurement_scope:{probe:'<img src=x>',intel:'constructor'},
              metric_updated_at:{network:1700000000000.5},measured_metric_count:9999});
            console.log(JSON.stringify({legacy,fresh,malicious}));})();
        """)
        for label in ('Network 更新','IP Grade 更新','已测指标数','探测状态','带宽状态','情报状态'):
            self.assertIn(label,out['fresh'])
        self.assertIn('完成',out['fresh'])
        self.assertIn('未选中',out['fresh'])
        self.assertIn('失败',out['fresh'])
        self.assertIn('未知',out['legacy'])
        self.assertNotIn('1700000000000',out['legacy'])
        self.assertNotIn('<img src=x>',out['malicious'])
        self.assertNotIn('src=x',out['malicious'])
        self.assertNotIn('function Object',out['malicious'])
        self.assertNotIn('9999',out['malicious'])

    def test_probe_details_keep_main_and_worker_separate_and_explain_partial_denominator(self):
        out=self.run_app("""
          (async()=>{await new Promise(r=>setTimeout(r,0));
            console.log(JSON.stringify(detailHtml({name:'A',probe_sources:{
              main:{attempts:3,successes:0,failures:3,started:3,requested:3,status:'completed',loss_pct:100},
              worker:{attempts:2,successes:1,failures:1,started:3,requested:3,status:'partial',loss_pct:50}}})));})();
        """)
        self.assertIn('主实例应用层探测',out);self.assertIn('worker 应用层探测',out)
        self.assertIn('已完成 2 / 请求 3',out);self.assertIn('已调用 3',out)
        self.assertIn('部分',out);self.assertIn('非 ICMP',out)

    def test_probe_details_escape_untrusted_values_and_ignore_unknown_paths(self):
        out=self.run_app("""
          (async()=>{await new Promise(r=>setTimeout(r,0));
            console.log(JSON.stringify(detailHtml({name:'A',probe_sources:{
              serial:{attempts:'<img src=x>',successes:1,failures:0,status:'partial'},
              unknown:{attempts:'CANARY'}}})));})();
        """)
        self.assertNotIn('<img src=x>',out);self.assertIn('&lt;img src=x&gt;',out)
        self.assertNotIn('CANARY',out)

    def test_desktop_restores_backend_preferences_without_using_port_local_storage(self):
        identity='node_v2_'+'a'*32
        data={'/api/preferences':{'ok':True,'version':1,'values':{'sb_theme':'dark','sb_profile':'daily','sb_favs_v2':json.dumps([identity]),'sb_mode':'ip'}}}
        stub=STUB_JS.replace('__FETCH_MAP__',json.dumps(data))+"\nwindow.SPEEDBENCH_ENV=Object.freeze({client:'webview'});localStorage.setItem('sb_theme','light');"
        script=stub+'\n'+MODULE.read_text(encoding='utf-8')+'\n'+APP_JS.read_text(encoding='utf-8')+"""
          (async()=>{await new Promise(r=>setTimeout(r,0));
            console.log(JSON.stringify({theme:lsGet('sb_theme'),profile:currentProfile,ids:[...favIds],mode:document.getElementById('f-mode').value,environment:document.getElementById('leak-environment').textContent,desktop:document.getElementById('desktop-settings').hidden}));})();
        """
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'fixture.js';path.write_text(script,encoding='utf-8')
            result=subprocess.run([NODE,str(path)],capture_output=True,text=True,encoding='utf-8',timeout=10)
        self.assertEqual(result.returncode,0,result.stderr);out=json.loads(result.stdout)
        self.assertEqual(out['theme'],'dark');self.assertEqual(out['profile'],'daily')
        self.assertEqual(out['ids'],[identity]);self.assertEqual(out['mode'],'ip')
        self.assertIn('不代表 Chrome',out['environment']);self.assertFalse(out['desktop'])
    def test_power_notice_is_fixed_chinese_without_raw_values(self):
        out=self.run_app("""
          (async()=>{await new Promise(r=>setTimeout(r,0));
            const resume=powerNotice({cleanup:{counters:{system_resumes:1}}});
            const clock=powerNotice({cleanup:{counters:{power_clock_errors:2}}});
            const both=powerNotice({cleanup:{counters:{system_resumes:1,power_clock_errors:1}}});
            const none=powerNotice({});
            const manual=powerNotice({cleanup:{counters:{cache_hits:5}}});
            console.log(JSON.stringify({resume,clock,both,none,manual}));})();
        """)
        self.assertIn('从睡眠',out['resume']);self.assertIn('手动',out['resume'])
        self.assertIn('未报告的流量',out['resume']);self.assertNotIn('<',out['resume'])
        self.assertIn('重启应用',out['clock']);self.assertIn('不会自动重试',out['clock'])
        self.assertIn('从睡眠',out['both']);self.assertIn('重启应用',out['both'])
        self.assertEqual(out['none'],'');self.assertEqual(out['manual'],'')

    def test_power_explanations_appear_in_live_and_history_views(self):
        job='job_'+'a'*32
        task={'job_id':job,'status':'cancelled','partial':True,'mode':'quick',
              'elapsed_ms':1234,'results':[],'config':{'mode':'quick'},
              'metrics':{'cleanup':{'duration_ms':0,'attempts':0,'successes':0,'bytes':0,
                                    'counters':{'power_clock_errors':1}}}}
        out=self.run_app("""
          (async()=>{await new Promise(r=>setTimeout(r,0));
            showTask(%s);
            const live=document.getElementById('task-summary').textContent;
            await showTaskHistory(%s);
            const html=document.getElementById('task-detail').innerHTML;
            console.log(JSON.stringify({live,html}));})();
        """ % (json.dumps(task), json.dumps(job)), {'/api/tasks/'+job:task})
        self.assertIn('重启应用',out['live'])
        self.assertIn('重启应用',out['html'])
        self.assertNotIn('<script',out['html'])
        self.assertNotIn('SECRET',out['html'])
        self.assertNotIn('power_clock_errors',out['html'])

    def run_app(self,driver,data=None):
        stub=STUB_JS.replace('__FETCH_MAP__',json.dumps(data or {}))
        code=stub+'\n'+MODULE.read_text(encoding='utf-8')+'\n'+APP_JS.read_text(encoding='utf-8')+'\n'+driver
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'app.js';path.write_text(code,encoding='utf-8')
            result=subprocess.run([NODE,str(path)],capture_output=True,text=True,encoding='utf-8',timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        return json.loads(result.stdout)

    def test_all_trend_entrypoints_carry_identity_and_old_name_fallback_is_separate(self):
        out=self.run_app("""
          (async()=>{await new Promise(r=>setTimeout(r,0));
            histSelNode='重复名';histSelNodeId='node_v2_1';
            await fetchNodeTrend('重复名','node_v2_1');
            console.log(JSON.stringify({calls:__fetchCalls,detail:detailHtml({name:'重复名',node_id:'node_v2_1'}),board:boardItem({name:'重复名',node_id:'node_v2_1',sc:1})}));})();
        """)
        self.assertTrue(any('&node_id=node_v2_1' in url for url in out['calls']))
        self.assertIn('data-node-id="node_v2_1"',out['detail'])
        self.assertIn('data-node-id="node_v2_1"',out['board'])

    def test_unknown_source_does_not_query_legacy_empty_name(self):
        out=self.run_app("""
          (async()=>{await new Promise(r=>setTimeout(r,0));
            subsData=[{provider:'来源未知',selection_key:'unknown',source_status:'unknown'}];
            await selectSub('unknown');
            console.log(JSON.stringify(__fetchCalls));})();
        """)
        self.assertFalse(any(url.startswith('/api/subscription?') for url in out))

    def test_client_elapsed_uses_monotonic_received_snapshot_not_wall_clock(self):
        out=self.run_js("""
          (async()=>{let now=100,states=[],stream;
            class E{constructor(){this.handlers={};stream=this;} addEventListener(k,v){this.handlers[k]=v;} close(){}}
            const client=new T.Client({now:()=>now,EventSource:E,get:async()=>({version:1,job_id:'j',seq:1,status:'probing',elapsed_ms:500,results:[]}),onChange:s=>states.push(s.elapsed_ms)});
            await client.attach('j');now=1100;
            stream.handlers.progress({data:JSON.stringify({version:1,job_id:'j',seq:2,type:'node_probe',payload:{}})});
            client.close();console.log(JSON.stringify(states));})();
        """)
        self.assertEqual(out,[500,1500])

    def run_js(self,script):
        code = 'const T=require('+json.dumps(str(MODULE))+');\n'+script
        result = subprocess.run([NODE,'-e',code],text=True,capture_output=True,encoding='utf-8',timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        return json.loads(result.stdout)

    def test_merge_sequence_and_gap_resync_preserve_partial_fields(self):
        out=self.run_js("""
          const s=new T.TaskState({job_id:'j',seq:1,results:[{node_id:'id',name:'旧名',latency_ms:12}]});
          const e={version:1,job_id:'j',seq:2,type:'node_exit',payload:{result:{node_id:'id',name:'新名',exit_ipv4:'203.0.113.1'}}};
          const a=s.apply(e),b=s.apply(e),c=s.apply({...e,seq:4});
          console.log(JSON.stringify({a,b,c,state:s.value}));
        """)
        self.assertEqual((out['a'],out['b'],out['c']),('updated','ignored','resync'))
        self.assertEqual(len(out['state']['results']),1)
        self.assertEqual(out['state']['results'][0]['latency_ms'],12)

    def test_favorites_only_migrate_unique_strong_identity(self):
        out=self.run_js("""
          console.log(JSON.stringify(T.migrateFavorites(['A','B','C'],[
            {runtime_name:'A',node_id:'id1',identity_strength:'strong'},
            {runtime_name:'B',node_id:'id2',identity_strength:'strong'},
            {runtime_name:'B',node_id:'id3',identity_strength:'strong'},
            {runtime_name:'C',node_id:'weak',identity_strength:'weak'}])));
        """)
        self.assertEqual(out,{'ids':['id1'],'pending':['B','C']})

    def test_budget_contains_warmup_and_multistream_but_ip_only_zero(self):
        out=self.run_js("""
          const c={bandwidth:true,top_n:5,mb:null,rounds:2,multi:true};
          console.log(JSON.stringify([T.budget(c,10),T.budget({...c,bandwidth:false},10)]));
        """)
        self.assertEqual(out,[4755,0])

    def test_refresh_then_sse_does_not_start_a_second_job(self):
        out=self.run_js("""
          (async()=>{
            let stream,calls=[];
            class E{constructor(url){this.url=url;this.handlers={};stream=this;} addEventListener(k,v){this.handlers[k]=v;} close(){this.closed=true;}}
            const client=new T.Client({EventSource:E,get:async u=>{calls.push(u);return {version:1,job_id:'j',seq:1,status:'probing',results:[]};},onChange:s=>{}});
            await client.attach('j');
            stream.handlers.progress({data:JSON.stringify({version:1,job_id:'j',seq:2,type:'node_probe',payload:{result:{node_id:'id',latency_ms:12}}})});
            console.log(JSON.stringify({calls,url:stream.url,results:client.state.value.results}));client.close();
          })();
        """)
        self.assertEqual(out['calls'],['/api/jobs/j'])
        self.assertIn('since_seq=1',out['url'])
        self.assertEqual(out['results'][0]['latency_ms'],12)

    def test_real_app_starts_mode_job_without_requesting_a_fixed_sample(self):
        data={'/api/catalog':{'status':'ok','sources':[],'nodes':[]},
              '/api/jobs':{'ok':True,'job_id':'job_'+'a'*32},
              '/api/jobs/job_'+'a'*32:{'version':1,'job_id':'job_'+'a'*32,'seq':1,'status':'failed','results':[]}}
        stub=STUB_JS.replace('__FETCH_MAP__',json.dumps(data))
        stub=stub.replace('async function fetch(url){',
            'async function fetch(url,opts){if(opts&&opts.body) requests.push({url,body:JSON.parse(opts.body)});')
        code='const requests=[];\n'+stub+'\n'+MODULE.read_text(encoding='utf-8')+'\n'+APP_JS.read_text(encoding='utf-8')
        code+="\n(async()=>{await new Promise(r=>setTimeout(r,0));__el('f-mode').value='quick';__el('f-target').value='download';__el('f-rounds').value='1';await startRun();console.log(JSON.stringify(requests));})();"
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'app.js'
            path.write_text(code,encoding='utf-8')
            result=subprocess.run([NODE,str(path)],capture_output=True,text=True,encoding='utf-8',timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        out=json.loads(result.stdout)
        body=next(x['body'] for x in out if x['url']=='/api/jobs')
        self.assertEqual(body['mode'],'quick')
        self.assertEqual(body['target_profile'],'download')
        self.assertNotIn('mb',body)

    def test_switch_preview_then_confirmation_uses_stable_id_and_exact_plan(self):
        out=self.run_app("""
          (async()=>{await new Promise(r=>setTimeout(r,0));
            const nid='node_v2_'+'a'.repeat(32), sid='subscription_v2_'+'b'.repeat(32);
            const plan={node_id:nid,runtime_name:'节点',identity_strength:'strong',
              source_status:'verified',subscription_name:'订阅甲',subscription_ids:[sid],
              subscriptions:[{subscription_id:sid,name:'订阅甲'}],
              group:'选择',current:'old',root_revision:1};
            let pending=null,previews=[],switches=[];
            post=async(url,body)=>{if(url==='/api/switch/preview'){previews.push(body);return {ok:true,plan};}
              switches.push({url,body});return {ok:true,msg:'已切换',group:'选择',now:'节点'};};
            confirmModal=(text,yes)=>{pending=yes;};
            const btn={dataset:{name:'节点',nodeId:nid},disabled:false,textContent:'切换'};
            await switchNode('节点',btn);
            const beforeConfirm={previews:previews.length,switches:switches.length,
              modal:typeof pending==='function',disabled:btn.disabled,previewBody:previews[0]};
            await pending();
            console.log(JSON.stringify({beforeConfirm,last:switches[switches.length-1]}));})();
        """)
        self.assertEqual(out['beforeConfirm']['previews'],1)
        self.assertEqual(out['beforeConfirm']['switches'],0)
        self.assertTrue(out['beforeConfirm']['modal'])
        self.assertFalse(out['beforeConfirm']['disabled'])
        self.assertEqual(out['beforeConfirm']['previewBody'],{'node_id':'node_v2_'+'a'*32})
        self.assertEqual(out['last']['url'],'/api/switch')
        confirmation=out['last']['body']['confirmation']
        self.assertEqual(out['last']['body']['node_id'],'node_v2_'+'a'*32)
        self.assertEqual(set(confirmation),{'node_id','runtime_name','identity_strength',
            'source_status','subscription_name','subscription_ids','subscriptions',
            'group','current','root_revision'})
        self.assertEqual(confirmation['group'],'选择')
        self.assertEqual(confirmation['runtime_name'],'节点')

    def test_switch_modal_cancel_performs_no_switch_and_restores_button(self):
        out=self.run_app("""
          (async()=>{await new Promise(r=>setTimeout(r,0));
            const nid='node_v2_'+'a'.repeat(32);
            const plan={node_id:nid,runtime_name:'节点',identity_strength:'strong',
              source_status:'verified',subscription_name:'',subscription_ids:[],subscriptions:[],
              group:'选择',current:'old',root_revision:1};
            let pending=null,switches=0;
            post=async(url)=>{if(url==='/api/switch')switches++;return {ok:true,plan};};
            confirmModal=(text,yes)=>{pending=yes;};
            const btn={dataset:{name:'节点',nodeId:nid},disabled:false};
            await switchNode('节点',btn);
            console.log(JSON.stringify({opened:typeof pending==='function',
              switches,disabled:btn.disabled}));})();
        """)
        self.assertTrue(out['opened'])
        self.assertEqual(out['switches'],0)
        self.assertFalse(out['disabled'])

    def test_switch_cancel_restores_clicked_button_even_after_table_replacement(self):
        out=self.run_app("""
          (async()=>{await new Promise(r=>setTimeout(r,0));let clicked=0,replacement=0,wrong=0;
            const nodeId='node_v2_'+'a'.repeat(32),btn={dataset:{nodeId},disabled:false,isConnected:true,
              focus:()=>{clicked++;}};
            document.activeElement={isConnected:true,focus:()=>{wrong++;}};
            post=async()=>({ok:true,plan:{node_id:nodeId,runtime_name:'fixture',group:'group',current:'old',
              source_status:'unknown',identity_strength:'weak',subscriptions:[]}});
            await switchNode('fixture',btn);closeModal();
            await switchNode('fixture',btn);btn.isConnected=false;
            document.querySelectorAll=()=>[{dataset:{nodeId},isConnected:true,focus:()=>{replacement++;}}];closeModal();
            console.log(JSON.stringify({clicked,replacement,wrong}));})();
        """)
        self.assertEqual(out,{'clicked':1,'replacement':1,'wrong':0})

    def test_confirmed_switch_focuses_result_row_after_current_button_becomes_disabled(self):
        out=self.run_app("""
          (async()=>{await new Promise(r=>setTimeout(r,0));let focused=0;
            const nodeId='node_v2_'+'a'.repeat(32),btn={dataset:{nodeId},disabled:false};
            const plan={node_id:nodeId,runtime_name:'fixture',group:'group',current:'old',
              source_status:'unknown',identity_strength:'weak',subscriptions:[]};
            post=async(url)=>url.endsWith('/preview')?{ok:true,plan}:{ok:true,now:'fixture',group:'group'};
            await switchNode('fixture',btn);const yes=modalYes;closeModal();
            document.querySelectorAll=()=>[{dataset:{nodeId},focus:()=>{focused++;}}];renderTable=()=>{};
            await yes();console.log(JSON.stringify({focused,disabled:btn.disabled}));})();
        """)
        self.assertEqual(out,{'focused':1,'disabled':False})

    def test_switch_confirmation_displays_fresh_plan_not_cached_metadata(self):
        out=self.run_app("""
          (async()=>{await new Promise(r=>setTimeout(r,0));
            const nid='node_v2_'+'a'.repeat(32);
            latestData={results:[{name:'节点',node_id:nid,subscription_name:'旧来源',provider:'旧'}]};
            currentGroup='stale-group';currentNode='stale-node';
            const plan={node_id:nid,runtime_name:'新名',identity_strength:'strong',
              source_status:'verified',subscription_name:'新来源',subscription_ids:[],subscriptions:[],
              group:'新组',current:'新当前',root_revision:2};
            let modalText='';post=async()=>({ok:true,plan});
            confirmModal=text=>{modalText=text;};
            await switchNode('节点',{dataset:{name:'节点',nodeId:nid},disabled:false});
            console.log(JSON.stringify(modalText));})();
        """)
        self.assertIn('新名',out);self.assertIn('新组',out)
        self.assertIn('新当前',out);self.assertIn('新来源',out)
        self.assertNotIn('stale-group',out);self.assertNotIn('旧来源',out)

    def test_failed_switch_preview_shows_no_modal_and_performs_no_switch(self):
        out=self.run_app("""
          (async()=>{await new Promise(r=>setTimeout(r,0));
            let modal=0,switches=0;
            post=async(url)=>{if(url==='/api/switch')switches++;return {ok:false,msg:'目录暂不可用'};};
            confirmModal=()=>{modal++;};
            const btn={dataset:{name:'节点',nodeId:'node_v2_'+'a'.repeat(32)},disabled:false};
            await switchNode('节点',btn);
            console.log(JSON.stringify({modal,switches,disabled:btn.disabled}));})();
        """)
        self.assertEqual(out,{'modal':0,'switches':0,'disabled':False})

    def test_switch_confirmation_labels_sources_honestly_and_renders_unsafe_names_as_text(self):
        out=self.run_app("""
          (async()=>{await new Promise(r=>setTimeout(r,0));
            const nid='node_v2_'+'a'.repeat(32);let texts=[];
            const base={node_id:nid,runtime_name:'节点',identity_strength:'strong',
              source_status:'verified',subscription_name:'',subscription_ids:[],subscriptions:[],
              group:'选择',current:'old',root_revision:1};
            const realConfirm=confirmModal;
            confirmModal=text=>{texts.push(text);};
            post=async()=>({ok:true,plan:{...base,source_status:'ambiguous'}});
            await switchNode('节点',{dataset:{name:'节点',nodeId:nid},disabled:false});
            post=async()=>({ok:true,plan:{...base,identity_strength:'weak',source_status:'unknown'}});
            await switchNode('节点',{dataset:{name:'节点',nodeId:nid},disabled:false});
            confirmModal=realConfirm;
            post=async()=>({ok:true,plan:{...base,runtime_name:'<img src=x onerror=alert(1)>'}});
            await switchNode('节点',{dataset:{name:'节点',nodeId:nid},disabled:false});
            const el=document.getElementById('modal-text');
            console.log(JSON.stringify({texts,text:el.textContent,html:el.innerHTML}));})();
        """)
        self.assertIn('多个来源（无法唯一确认）',out['texts'][0])
        self.assertIn('来源未知',out['texts'][1])
        self.assertIn('名称范围，身份未验证',out['texts'][1])
        self.assertIn('<img src=x onerror=alert(1)>',out['text'])
        self.assertNotIn('<img',out['html'])
