import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from speedbench_preferences import PreferenceError, validate
import speedbench_web as web
from tests.test_resume_js import NODE, STUB_JS, APP_JS
from tests.web_server_case import WebServerCase

MODULE = Path(__file__).resolve().parents[1] / 'web/preferences.js'


@unittest.skipUnless(NODE, 'Node unavailable')
class PreferenceTransferTest(unittest.TestCase):
    def js(self, script):
        result = subprocess.run([NODE, '-e', 'const P=require(' + json.dumps(str(MODULE)) + ');' + script],
                                capture_output=True, text=True, encoding='utf-8', timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_export_selects_only_non_secret_fields_and_round_trips(self):
        out = self.js('''const values={sb_theme:'dark',sb_favs:JSON.stringify(['中文 👋 <节点>']),
          ipqs_key:'CANARY-secret',controller_secret:'CANARY-secret',sb_token:'CANARY-secret',
          history_path:'C:/private',raw:'CANARY-secret'};
          const text=P.exportText(k=>values[k]??null);
          console.log(JSON.stringify({text,values:P.parseImport(text)}));''')
        self.assertNotIn('CANARY', out['text'])
        self.assertEqual(out['values']['sb_theme'], 'dark')
        self.assertEqual(json.loads(out['values']['sb_favs']), ['中文 👋 <节点>'])

    def test_import_rejects_unknown_keys_schema_sizes_and_surrogates_without_echo(self):
        out = self.js('''const valid={format:P.FORMAT,version:1,values:{sb_theme:'light'}};
          const variants=[{...valid,version:true},{...valid,version:2},{...valid,raw:'CANARY-secret'},
            {...valid,values:{sb_theme:'dark',api_key:'CANARY-secret'}},
            {...valid,values:{sb_favs_v2:'["https://evil"]'}},
            {...valid,values:{sb_favs:JSON.stringify(['\\ud800'])}},
            {...valid,values:{sb_mode:'CANARY-secret'}},
            {...valid,values:JSON.parse('{"__proto__":{"polluted":true}}')}];
          const errors=variants.map(v=>{try{P.parseImport(JSON.stringify(v));return null;}catch(e){return e.message;}});
          try{P.parseImport('x'.repeat(P.MAX_BYTES+1));errors.push(null);}catch(e){errors.push(e.message);}
          console.log(JSON.stringify(errors));''')
        self.assertTrue(all(out))
        self.assertNotIn('CANARY', json.dumps(out))

    def test_import_merges_favorites_and_does_not_drop_unconfirmed_names(self):
        out = self.js('''const id='node_v2_'+'a'.repeat(32);
          console.log(JSON.stringify(P.merge({sb_favs:'["旧收藏"]',sb_favs_v2:JSON.stringify([id]),sb_mode:'quick'},
            {sb_favs:'["新收藏","旧收藏"]',sb_favs_v2:JSON.stringify([id]),sb_theme:'dark'})));''')
        self.assertEqual(json.loads(out['sb_favs']), ['旧收藏', '新收藏'])
        self.assertEqual(len(json.loads(out['sb_favs_v2'])), 1)
        self.assertEqual(out['sb_mode'], 'quick')

    def test_serial_mode_exports_and_merges_without_losing_favorites(self):
        out = self.js('''const values={sb_mode:'legacy',sb_favs:'["旧收藏"]'};
          const restored=P.parseImport(P.exportText(k=>values[k]??null));
          console.log(JSON.stringify(P.merge(restored,{sb_favs:'["新收藏"]'})));''')
        self.assertEqual(out['sb_mode'], 'legacy')
        self.assertEqual(json.loads(out['sb_favs']), ['旧收藏', '新收藏'])
        self.assertEqual(validate(out)['sb_mode'], 'legacy')
        self.assertEqual(json.loads(validate(out)['sb_favs']), json.loads(out['sb_favs']))

    def test_browser_storage_failure_rolls_back_only_touched_whitelist_keys(self):
        out = self.js('''const store=new Map([['sb_theme','light'],['outside','untouched']]);let fail=true;
          try{P.saveBrowser({sb_theme:'dark',sb_mode:'ip'},k=>store.get(k)??null,
            (k,v)=>{if(k==='sb_mode'&&fail){fail=false;throw Error('CANARY-private');}store.set(k,v);},k=>store.delete(k));}
          catch(e){console.log(JSON.stringify({error:e.message,values:Object.fromEntries(store)}));}''')
        self.assertEqual(out['values'], {'sb_theme':'light','outside':'untouched'})
        self.assertNotIn('CANARY', out['error'])

    def test_python_and_js_accept_same_whitelisted_transfer_values(self):
        values = {'sb_theme':'system','sb_mode':'deep','sb_target':'residential',
                  'sb_profile':'ipclean','sb_subs_days':'90','sb_notifications':'off',
                  'sb_favs':'["中文 👋"]','sb_favs_v2':'["node_v2_'+'a'*32+'"]'}
        out = self.js('console.log(JSON.stringify(P.validate('+json.dumps(values)+')));')
        self.assertEqual({k:json.loads(v) if k.startswith('sb_favs') else v for k,v in out.items()},
                         {k:json.loads(v) if k.startswith('sb_favs') else v for k,v in validate(values).items()})


class PreferenceUnicodeTest(unittest.TestCase):
    def test_unpaired_surrogate_is_a_safe_preference_error(self):
        with self.assertRaises(PreferenceError):
            validate({'sb_favs': json.dumps(['\ud800'])})


@unittest.skipUnless(NODE, 'Node unavailable')
class PreferenceTransferUiTest(unittest.TestCase):
    def app(self, script, desktop=False):
        data={'/api/preferences':{'ok':True,'values':{'sb_theme':'light','sb_favs':'["旧收藏"]'}},
              '/api/catalog':{'status':'ok','nodes':[],'sources':[]},
              '/api/data-status':{'ok':True,'data_home':'fixture','history':{}},
              '/api/run/status':{'running':False}}
        stub=STUB_JS.replace('__FETCH_MAP__',json.dumps(data))
        stub=stub.replace('async function fetch(url){',
            'async function fetch(url,options){if(options&&options.body)__requests.push({url,body:JSON.parse(options.body)});')
        code='const P=require('+json.dumps(str(MODULE))+');const __requests=[];'+stub
        if desktop:code+='window.SPEEDBENCH_ENV={client:"webview"};'
        code+=APP_JS.read_text(encoding='utf-8')+'\n(async()=>{await new Promise(r=>setTimeout(r,0));'+script+'})();'
        # Full shared app exceeds Windows' command-line limit; stdin is also
        # portable and avoids putting the fixture script in argv.
        result=subprocess.run([NODE,'-'],input=code,capture_output=True,text=True,encoding='utf-8',timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        return json.loads(result.stdout)

    def script(self):
        return '''const text=JSON.stringify({format:P.FORMAT,version:1,values:{sb_theme:'dark',sb_favs:'["新收藏"]'}});
          __el('preference-json').value=text;__el('btn-preferences-preview').__listeners.click[0]();
          await applyPreferenceImport();
          console.log(JSON.stringify({theme:__el('f-theme').value,names:[...favs],store:Object.fromEntries(__store),
            requests:__requests,status:__el('preference-transfer-status').textContent}));'''

    def test_browser_import_preserves_old_favorites_without_backend_preference_write(self):
        out=self.app("localStorage.setItem('sb_favs','[\"旧收藏\"]');"+self.script())
        self.assertEqual(out['theme'],'dark')
        self.assertEqual(out['names'],['旧收藏','新收藏'])
        self.assertEqual(out['store']['sb_theme'],'dark')
        self.assertFalse(any(r['url']=='/api/preferences' for r in out['requests']))

    def test_desktop_import_uses_atomic_backend_patch_not_browser_local_storage(self):
        out=self.app(self.script(),desktop=True)
        self.assertEqual(out['names'],['旧收藏','新收藏'])
        self.assertEqual(out['theme'],'dark')
        self.assertEqual(out['store'],{})
        patch=next(r['body'] for r in out['requests'] if r['url']=='/api/preferences')
        self.assertEqual(patch['sb_theme'],'dark')
        self.assertNotIn('sb_token',patch)

    def test_changed_preview_cannot_apply_stale_payload(self):
        out=self.app('''pendingPreferenceImport={text:'old',values:{sb_theme:'dark'}};
          __el('preference-json').value='changed';await applyPreferenceImport();
          console.log(JSON.stringify({store:Object.fromEntries(__store),requests:__requests}));''')
        self.assertNotIn('sb_theme',out['store'])
        self.assertFalse(any(r['url']=='/api/preferences' for r in out['requests']))


class DataStatusTest(WebServerCase):
    def test_path_diagnostic_error_is_generic_and_does_not_dump_private_input(self):
        with mock.patch.object(Path,'resolve',side_effect=RuntimeError('CANARY-private-path')):
            status,body=self.request('GET','/api/data-status',headers={'X-SpeedBench-Token':web.WEB_TOKEN})
            self.assertEqual(status,503)
            self.assertNotIn(b'CANARY',body)

    def test_invalid_body_and_surrogate_return_safe_error_without_saving(self):
        with mock.patch.object(web,'DATA_HOME',Path(self._task_temp.name)):
            for raw in (b'not-json',b'{"sb_favs":"[\\"\\ud800\\"]"}'):
                status,body=self.request('POST','/api/preferences',body=raw,headers={
                    'X-SpeedBench-Token':web.WEB_TOKEN,'Content-Type':'application/json'})
                self.assertEqual(status,400)
                self.assertNotIn(b'UnicodeEncodeError',body)
                self.assertFalse((Path(self._task_temp.name)/'ui-preferences.json').exists())

    def test_status_requires_token_and_origin_and_never_migrates_history(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            history = root/'speedbench-history.jsonl'
            history.write_bytes(b'old-raw-fixture-unchanged')
            with mock.patch.object(web,'DATA_HOME',root), mock.patch.object(web,'HISTORY',history):
                status,_=self.request('GET','/api/data-status')
                self.assertEqual(status,403)
                status,_=self.request('GET','/api/data-status',headers={
                    'X-SpeedBench-Token':web.WEB_TOKEN,'Origin':'https://evil.example'})
                self.assertEqual(status,403)
                status,raw=self.request('GET','/api/data-status',headers={'X-SpeedBench-Token':web.WEB_TOKEN})
                self.assertEqual(status,200)
                result=json.loads(raw)
                self.assertEqual(result['history']['jsonl_exists'],True)
                self.assertEqual(result['history']['database_exists'],False)
                self.assertFalse(result['automatic_import'])
                self.assertNotIn(b'old-raw-fixture',raw)
                self.assertFalse(history.with_suffix('.db').exists())
                self.assertEqual(history.read_bytes(),b'old-raw-fixture-unchanged')
