import json
from pathlib import Path
import subprocess
import unittest

from tests.test_resume_js import NODE

ROOT=Path(__file__).resolve().parents[1]


@unittest.skipUnless(NODE,'Node unavailable')
class ReleaseUiTest(unittest.TestCase):
    def js(self,code):
        result=subprocess.run([NODE,'-'],input=code,capture_output=True,text=True,encoding='utf-8',timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        return json.loads(result.stdout)

    def test_failure_never_claims_current_and_remote_text_is_not_used(self):
        code='const R=require('+json.dumps(str(ROOT/'web/releases.js'))+');'
        code+='console.log(JSON.stringify(["timeout","rate_limited","invalid_response","not_published","unavailable"].map(status=>R.text({status,current:"1.0.1",message:"CANARY",html_url:"CANARY"}))));'
        out=self.js(code)
        self.assertTrue(all('无法确认' in text for text in out))
        self.assertNotIn('CANARY',json.dumps(out));self.assertFalse(any('一致' in text for text in out))

    def test_alpha_ahead_does_not_mean_up_to_date_or_trigger_downgrade(self):
        out=self.js('const R=require('+json.dumps(str(ROOT/'web/releases.js'))+');'+
                    'console.log(JSON.stringify(R.text({status:"ok",current:"1.1.0-alpha.1",current_prerelease:true,latest:"v1.0.1",comparison:"ahead",cached:true})));')
        self.assertIn('预发布版',out);self.assertIn('不自动降级',out);self.assertIn('未检查预发布',out);self.assertIn('缓存',out)

    def test_shared_ui_only_checks_after_click_and_uses_fixed_browser_or_native_opener(self):
        app=(ROOT/'web/app.js').read_text(encoding='utf-8')
        function=app[app.index('function initReleaseSettings(){'):app.index('function initPreferenceTransfer(){')]
        for desktop in (False,True):
            code='const SBReleases=require('+json.dumps(str(ROOT/'web/releases.js'))+');'
            code+='''const elements=new Map(),calls=[],opened=[],native=[];
              const document={getElementById:id=>{if(!elements.has(id))elements.set(id,{disabled:false,addEventListener:(name,fn)=>elements.get(id)[name]=fn});return elements.get(id);}};
              var window={open:(...args)=>opened.push(args)};const SB_TOKEN='fixture-token';
              const fetch=async(url,options)=>{calls.push({url,options});return{json:async()=>({status:'not_checked',current:'1.0.1'})};};
              const post=async(url,body)=>{calls.push({url,body});return{status:'timeout',current:'1.0.1'};};
              const desktopAction=action=>native.push(action);'''
            code+='const SB_DESKTOP='+json.dumps(desktop)+';'+function
            code+='''(async()=>{initReleaseSettings();await new Promise(r=>setTimeout(r,0));
              const before=[...calls];await elements.get('btn-release-check').click();elements.get('btn-official-releases').click();
              console.log(JSON.stringify({before,calls,opened,native,status:elements.get('release-status').textContent,disabled:elements.get('btn-release-check').disabled}));})();'''
            out=self.js(code)
            self.assertEqual([c['url'] for c in out['before']],['/api/releases'])
            self.assertEqual(out['calls'][-1],{'url':'/api/releases/check','body':{}})
            self.assertIn('无法确认',out['status']);self.assertFalse(out['disabled'])
            if desktop:self.assertEqual(out['native'],['releases']);self.assertEqual(out['opened'],[])
            else:self.assertEqual(out['native'],[]);self.assertEqual(out['opened'][0][0],'https://github.com/voodookyo/clash-speedbench/releases')

    def test_delayed_local_info_cannot_overwrite_the_user_check_result(self):
        app=(ROOT/'web/app.js').read_text(encoding='utf-8')
        function=app[app.index('function initReleaseSettings(){'):app.index('function initPreferenceTransfer(){')]
        code='const SBReleases=require('+json.dumps(str(ROOT/'web/releases.js'))+');'
        code+='''var window={open:()=>{}};const SB_DESKTOP=false,SB_TOKEN='fixture';
          const elements=new Map();const document={getElementById:id=>{if(!elements.has(id))elements.set(id,{addEventListener:(k,f)=>elements.get(id)[k]=f});return elements.get(id);}};
          let resolveInfo;const fetch=()=>new Promise(resolve=>{resolveInfo=resolve;});
          const post=async()=>({status:'timeout',current:'1.0.1'});'''+function
        code+='''(async()=>{initReleaseSettings();await elements.get('btn-release-check').click();
          const checked=elements.get('release-status').textContent;
          resolveInfo({json:async()=>({status:'not_checked',current:'1.0.1'})});await new Promise(r=>setTimeout(r,0));
          console.log(JSON.stringify({checked,final:elements.get('release-status').textContent}));})();'''
        out=self.js(code);self.assertIn('无法确认',out['checked']);self.assertEqual(out['final'],out['checked'])
