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
