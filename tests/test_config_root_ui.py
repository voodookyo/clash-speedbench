import json
from pathlib import Path
import subprocess
import unittest
from tests.test_resume_js import NODE, STUB_JS, APP_JS

ROOT=Path(__file__).resolve().parents[1]
SETUP='''
const C=require(__MODULE__);
const ids=['config-root-path','config-root-status','btn-config-preview','btn-config-apply','btn-config-auto'];
const elements=Object.fromEntries(ids.map(id=>[id,{value:'',textContent:'',disabled:false,addEventListener(k,f){this[k]=f;}}]));
const document={getElementById:id=>elements[id]};const calls=[],confirmations=[];let changes=0;
let handler=async url=>({ok:true,path:null,mode:'auto'});
const fetch=async(url,options)=>{calls.push({url,options});return{json:async()=>handler(url,options)};};
const controls=C.init({document,fetch,token:'fixture-token',confirm:(text,callback)=>confirmations.push({text,callback}),onChanged:async()=>{changes++;}});
const tick=()=>new Promise(resolve=>setTimeout(resolve,0));const input=elements['config-root-path'];
const preview=elements['btn-config-preview'],apply=elements['btn-config-apply'],reset=elements['btn-config-auto'],status=elements['config-root-status'];
'''


@unittest.skipUnless(NODE,'Node unavailable')
class ConfigRootUiTest(unittest.TestCase):
    def js(self,driver):
        code=SETUP.replace('__MODULE__',json.dumps(str(ROOT/'web/config-root.js')))
        result=subprocess.run([NODE,'-'],input=code+'\n(async()=>{await tick();'+driver+'})();',
                              text=True,capture_output=True,encoding='utf-8',timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        return json.loads(result.stdout)

    def test_preview_does_not_apply_until_explicit_confirmation_and_only_local_authenticated_requests(self):
        out=self.js('''input.value='/fixture 中文';input.input();handler=async()=>({ok:true,path:'/fixture 中文'});
          await preview.click();const before=calls.map(c=>c.url);apply.click();const after=calls.map(c=>c.url);
          await confirmations[0].callback();console.log(JSON.stringify({before,after,calls,changes,status:status.textContent}));''')
        self.assertEqual(out['before'],['/api/config-root','/api/config-root/preview'])
        self.assertEqual(out['before'],out['after']);self.assertEqual(out['changes'],1)
        for call in out['calls']:
            self.assertTrue(call['url'].startswith('/api/config-root'))
            self.assertEqual(call['options']['headers']['X-SpeedBench-Token'],'fixture-token')
        self.assertEqual(json.loads(out['calls'][-1]['options']['body']),{'root':'/fixture 中文'})

    def test_stale_preview_cannot_apply_new_input(self):
        out=self.js('''let finish;handler=()=>new Promise(r=>finish=r);input.value='/old';input.input();
          const request=preview.click();await tick();input.value='/new';input.input();finish({ok:true,path:'/old'});await request;
          apply.click();console.log(JSON.stringify({disabled:apply.disabled,confirms:confirmations.length,status:status.textContent,calls}));''')
        self.assertTrue(out['disabled']);self.assertEqual(out['confirms'],0)
        self.assertNotIn('/old',out['status'])

    def test_changed_input_or_busy_state_invalidates_pending_confirmation(self):
        for action in ("input.value='/new';input.input();",'controls.setBusy(true);'):
            out=self.js('''input.value='/old';input.input();handler=async()=>({ok:true,path:'/old'});
              await preview.click();apply.click();'''+action+'''
              await confirmations[0].callback();console.log(JSON.stringify({calls,changes}));''')
            self.assertEqual(len(out['calls']),2);self.assertEqual(out['changes'],0)

    def test_ambiguous_write_failure_does_not_retry_or_claim_unchanged(self):
        out=self.js('''input.value='/fixture';input.input();handler=async()=>({ok:true,path:'/fixture'});
          await preview.click();apply.click();handler=async()=>{throw Error('CANARY-secret');};
          await confirmations[0].callback();console.log(JSON.stringify({calls,changes,status:status.textContent}));''')
        self.assertEqual(len(out['calls']),3);self.assertEqual(out['changes'],0)
        self.assertIn('无法确认',out['status']);self.assertNotIn('CANARY',out['status'])

    def test_reset_to_auto_is_explicit_and_session_path_never_uses_storage(self):
        out=self.js('''reset.click();const count=calls.length;await confirmations[0].callback();
          console.log(JSON.stringify({count,calls,changes}));''')
        self.assertEqual(out['count'],1);self.assertEqual(out['changes'],1)
        self.assertEqual(json.loads(out['calls'][-1]['options']['body']),{'root':''})
        source=(ROOT/'web/config-root.js').read_text(encoding='utf-8')
        for forbidden in ('localStorage','sessionStorage','innerHTML','console.log'):
            self.assertNotIn(forbidden,source)

    def test_old_catalog_request_cannot_restore_stale_node_ids_after_refresh(self):
        data={'/api/catalog':{'status':'ok','nodes':[],'sources':[]}}
        code=STUB_JS.replace('__FETCH_MAP__',json.dumps(data))+'\n'+APP_JS.read_text(encoding='utf-8')+'''
          (async()=>{await new Promise(r=>setTimeout(r,0));let old;
            getJSON=()=>new Promise(r=>old=r);const first=loadSourceCatalog();
            getJSON=async()=>({status:'ok',nodes:[{node_id:'fresh'}],sources:[]});await loadSourceCatalog();
            old({status:'ok',nodes:[{node_id:'stale'}],sources:[]});await first;
            console.log(JSON.stringify(sourceCatalog.nodes));})();'''
        result=subprocess.run([NODE,'-'],input=code,text=True,capture_output=True,encoding='utf-8',timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout),[{'node_id':'fresh'}])
