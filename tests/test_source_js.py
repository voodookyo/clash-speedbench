import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.test_resume_js import STUB_JS, NODE, APP_JS, INDEX_HTML


@unittest.skipUnless(NODE, 'Node unavailable')
class SourceJsTest(unittest.TestCase):
    def run_js(self, driver):
        data = {'/api/catalog': {'status':'ok','sources':[
                    {'subscription_id':'subscription_v2_'+'a'*32,'name':'<机场甲>','loaded':True},
                    {'subscription_id':'subscription_v2_'+'b'*32,'name':'未加载乙','loaded':False}],
                    'nodes':[{'source_status':'verified'}]},
                '/api/run':{'ok':True}, '/api/run/status':{'running':False},
                '/api/switch/preview':{'ok':True,'plan':{'node_id':'node_v2_'+'c'*32,'runtime_name':'改名节点',
                    'identity_strength':'strong','source_status':'unknown','subscription_name':'','subscription_ids':[],
                    'subscriptions':[],'group':'选择','current':'old','root_revision':0}},
                '/api/switch':{'ok':True,'now':'改名节点'}}
        stub = STUB_JS.replace('__FETCH_MAP__', json.dumps(data,ensure_ascii=False))
        stub = stub.replace('async function fetch(url){',
                           'async function fetch(url, opts){ if(opts && opts.body) __requests.push({url,body:JSON.parse(opts.body)});')
        script = 'const __requests=[];\n' + stub + APP_JS.read_text(encoding='utf-8')
        script += '\n(async()=>{ await new Promise(r=>setTimeout(r,0)); '+driver+' })();'
        with tempfile.TemporaryDirectory() as td:
            path = Path(td)/'sources.js'
            path.write_text(script,encoding='utf-8')
            result = subprocess.run([NODE,str(path)],capture_output=True,encoding='utf-8',timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        return json.loads(result.stdout.strip())

    def test_source_options_escape_and_disable_unloaded(self):
        out = self.run_js("console.log(JSON.stringify({html:__el('f-source').innerHTML}));")
        self.assertIn('&lt;机场甲&gt;',out['html'])
        self.assertIn(' disabled',out['html'])
        self.assertNotIn('<机场甲>',out['html'])

    def test_start_run_transmits_opaque_subscription_id(self):
        out = self.run_js("__el('f-source').value='subscription_v2_'+'a'.repeat(32); await startRun(); console.log(JSON.stringify(__requests));")
        body = next(x['body'] for x in out if x['url']=='/api/run')
        self.assertEqual(body['subscription_ids'],['subscription_v2_'+'a'*32])
        self.assertNotIn('机场甲',json.dumps(body))

    def test_switch_uses_id_and_accepts_current_runtime_name(self):
        out = self.run_js("await switchNode('旧节点',{dataset:{nodeId:'node_v2_'+'c'.repeat(32)}}); const before=__requests.filter(x=>x.url==='/api/switch').length; const yes=modalYes; closeModal(); await yes(); console.log(JSON.stringify({before,requests:__requests,now:currentNode}));")
        self.assertEqual(out['before'],0)
        preview=next(x['body'] for x in out['requests'] if x['url']=='/api/switch/preview')
        self.assertEqual(preview,{'node_id':'node_v2_'+'c'*32})
        body = next(x['body'] for x in out['requests'] if x['url']=='/api/switch')
        self.assertEqual(body['node_id'],'node_v2_'+'c'*32)
        self.assertEqual(body['confirmation']['runtime_name'],'改名节点')
        self.assertEqual(out['now'],'改名节点')

    def test_expired_selection_is_not_silently_expanded_to_all(self):
        out = self.run_js("__el('f-source').value='expired'; await loadSourceCatalog(); console.log(JSON.stringify({value:__el('f-source').value,html:__el('f-source').innerHTML}));")
        self.assertEqual(out['value'],'expired')
        self.assertIn('原选订阅已失效',out['html'])


class SourceUiStructureTest(unittest.TestCase):
    def test_controls_really_exist_in_served_html(self):
        html = INDEX_HTML.read_text(encoding='utf-8')
        for element in ('f-source','source-status','btn-source-refresh'):
            self.assertIn('id="'+element+'"',html)
