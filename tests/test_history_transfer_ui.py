import json
import re
import subprocess
import unittest

from tests.test_resume_js import NODE, STUB_JS, APP_JS


def _fresh_status(**over):
    status={'ok':True,'data_home':'/data/新目录','automatic_import':False,
        'history':{'jsonl_path':'/data/新目录/speedbench-history.jsonl','jsonl_exists':False,
                   'database_path':'/data/新目录/speedbench-history.db','database_exists':False},
        'alternate':None}
    status.update(over)
    return status


@unittest.skipUnless(NODE,'Node unavailable')
class HistoryTransferUiTest(unittest.TestCase):
    def app(self,script,*,preview=None,applied=None,data_status=None,desktop=False):
        responses={'/api/catalog':{'status':'ok','nodes':[],'sources':[]},
            '/api/run/status':{'running':False},'/api/history':[],
            '/api/data-status':data_status or {'ok':True,'data_home':'fixture','history':{}},
            '/api/history-import/status':{'ok':True,'can_rollback':True,'backup_id':'import_'+'a'*32},
            '/api/history-import/preview':preview or {'ok':True,'token':'fixture-token','source':'/fixture 旧目录',
                'destination':'/fixture new','new_runs':3,'duplicate_runs':2,'new_tasks':1,'conflicts':0,'can_apply':True},
            '/api/history-import/apply':applied or {'ok':True,'imported_runs':3,'imported_tasks':1,'backup_id':'import_'+'a'*32},
            '/api/history-import/rollback':{'ok':True}}
        stub=STUB_JS.replace('__FETCH_MAP__',json.dumps(responses,ensure_ascii=False))
        stub=stub.replace('async function fetch(url){',
            'async function fetch(url,options){__requests.push({url,method:options?.method||"GET",headers:options?.headers,body:options?.body?JSON.parse(options.body):null});')
        code='const SBPreferences=require('+json.dumps(str(APP_JS.parent/'preferences.js'))+');'
        code+='const SBTasks=require('+json.dumps(str(APP_JS.parent/'tasks.js'))+');const __requests=[];'+stub
        if desktop:code+='window.SPEEDBENCH_ENV={client:"webview"};'
        code+=APP_JS.read_text(encoding='utf-8')+'\n(async()=>{await new Promise(r=>setTimeout(r,0));'+script+'})();'
        result=subprocess.run([NODE,'-'],input=code,capture_output=True,text=True,encoding='utf-8',timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        return json.loads(result.stdout)

    def test_boot_only_loads_own_status_and_never_previews_or_imports(self):
        out=self.app('console.log(JSON.stringify({requests:__requests,directory:__el("history-import-directory").value}));')
        history=[r for r in out['requests'] if '/history-import/' in r['url']]
        self.assertEqual([r['url'] for r in history],['/api/history-import/status'])
        self.assertEqual(history[0]['headers']['X-SpeedBench-Token'],'stub-token')
        self.assertEqual(out['directory'],'')

    def test_closed_source_and_modal_confirmation_are_required_to_apply(self):
        out=self.app('''__el('history-import-directory').value='/fixture 旧目录';await previewHistoryImport();
          await finishHistoryImport('apply');const first=__requests.filter(r=>r.url.endsWith('/apply')).length;
          __el('history-import-closed').checked=true;historyImportButtons();
          __el('btn-history-import').__listeners.click[0]();
          const before=__requests.filter(r=>r.url.endsWith('/apply')).length;
          await modalYes();console.log(JSON.stringify({first,before,requests:__requests,status:__el('history-import-status').textContent}));''')
        self.assertEqual((out['first'],out['before']),(0,0))
        applied=[r for r in out['requests'] if r['url'].endswith('/apply')]
        self.assertEqual(len(applied),1)
        self.assertEqual(applied[0]['body'],{'token':'fixture-token'})
        self.assertIn('3 轮',out['status'])

    def test_changed_directory_or_conflict_cannot_apply_preview(self):
        script='''__el('history-import-directory').value='/fixture 旧目录';await previewHistoryImport();
          __el('history-import-closed').checked=true;historyImportButtons();
          __el('history-import-directory').value='/changed';__el('history-import-directory').__listeners.input[0]();
          await finishHistoryImport('apply');console.log(JSON.stringify({requests:__requests,disabled:__el('btn-history-import').disabled}));'''
        out=self.app(script)
        self.assertFalse(any(r['url'].endswith('/apply') for r in out['requests']))
        self.assertTrue(out['disabled'])
        out=self.app('''__el('history-import-directory').value='/fixture 旧目录';__el('history-import-closed').checked=true;
          await previewHistoryImport();await finishHistoryImport('apply');
          console.log(JSON.stringify({requests:__requests,disabled:__el('btn-history-import').disabled,status:__el('history-import-status').textContent}));''',
          preview={'ok':True,'token':'fixture-token','new_runs':0,'duplicate_runs':0,'new_tasks':0,'conflicts':1,'can_apply':False})
        self.assertTrue(out['disabled']);self.assertIn('冲突阻止',out['status'])
        self.assertFalse(any(r['url'].endswith('/apply') for r in out['requests']))

    def test_server_stale_rejection_clears_token_and_does_not_refresh_results(self):
        out=self.app('''__el('history-import-directory').value='/fixture 旧目录';await previewHistoryImport();
          __el('history-import-closed').checked=true;historyImportButtons();__requests.length=0;
          await finishHistoryImport('apply');console.log(JSON.stringify({requests:__requests,
            pending:pendingHistoryImport,disabled:__el('btn-history-import').disabled,status:__el('history-import-status').textContent}));''',
          applied={'ok':False,'msg':'源目录已改变，请重新预览'})
        self.assertIsNone(out['pending']);self.assertTrue(out['disabled']);self.assertIn('重新预览',out['status'])
        self.assertFalse(any(r['url']=='/api/latest' for r in out['requests']))

    def test_rollback_requires_its_own_modal_confirmation(self):
        out=self.app('''__requests.length=0;__el('btn-history-rollback').__listeners.click[0]();
          const before=__requests.filter(r=>r.url.endsWith('/rollback')).length;await modalYes();
          console.log(JSON.stringify({before,requests:__requests,status:__el('history-import-status').textContent}));''')
        self.assertEqual(out['before'],0)
        request=next(r for r in out['requests'] if r['url'].endswith('/rollback'))
        self.assertEqual(request['body'],{'backup_id':'import_'+'a'*32})
        self.assertIn('已撤回',out['status'])

    def test_rollback_clears_removed_detail_and_rejects_late_detail_response(self):
        out=self.app('''const id='job_'+'a'.repeat(32),realGet=getJSON;
          const task={job_id:id,status:'interrupted',mode:'quick',partial:true,results:[]};
          getJSON=async url=>url==='/api/tasks/'+id?task:realGet(url);
          await showTaskHistory(id);const opened=!__el('task-detail').hidden;
          let deliver;getJSON=async url=>url==='/api/tasks/'+id?new Promise(r=>{deliver=r;}):realGet(url);
          const pending=showTaskHistory(id);await finishHistoryImport('rollback');
          const cleared=__el('task-detail').hidden && __el('task-detail').innerHTML==='';
          deliver(task);await pending;
          console.log(JSON.stringify({opened,cleared,hidden:__el('task-detail').hidden,html:__el('task-detail').innerHTML}));''')
        self.assertTrue(out['opened']);self.assertTrue(out['cleared']);self.assertTrue(out['hidden']);self.assertEqual(out['html'],'')

    def test_nodes_guide_card_static_structure(self):
        html=(APP_JS.parent/'index.html').read_text(encoding='utf-8')
        for el_id in ('nodes-data-guide','nodes-data-guide-title','nodes-data-status','btn-open-history-import'):
            self.assertIn(f'id="{el_id}"',html)
        card=re.search(r'<div[^>]*id="nodes-data-guide"[^>]*>',html)
        self.assertIsNotNone(card)
        self.assertIn('hidden',card.group(0))

    def test_desktop_nodes_guide_reveals_fresh_and_existing_history(self):
        out=self.app('''console.log(JSON.stringify({hidden:__el('nodes-data-guide').hidden,
          text:__el('nodes-data-status').textContent,html:__el('nodes-data-status').innerHTML}));''',
          desktop=True,data_status=_fresh_status())
        self.assertFalse(out['hidden'])
        self.assertIn('/data/新目录',out['text'])
        self.assertIn('尚无历史',out['text'])
        self.assertEqual(out['html'],'')
        out=self.app('''console.log(JSON.stringify({text:__el('nodes-data-status').textContent}));''',
          desktop=True,data_status=_fresh_status(history={
            'jsonl_path':'/data/新目录/speedbench-history.jsonl','jsonl_exists':True,
            'database_path':'/data/新目录/speedbench-history.db','database_exists':True}))
        self.assertIn('已有历史文件',out['text'])
        self.assertNotIn('尚无历史',out['text'])

    def test_desktop_nodes_guide_reports_sqlite_only_alternate_as_unverified(self):
        alternate={'path':'/old/旧 源码目录','jsonl_exists':False,'database_exists':True}
        out=self.app('''console.log(JSON.stringify({text:__el('nodes-data-status').textContent,
          settings:__el('data-status').textContent}));''',
          desktop=True,data_status=_fresh_status(alternate=alternate))
        for text in (out['text'],out['settings']):
            self.assertIn('/old/旧 源码目录',text)
            self.assertIn('SQLite',text)
            self.assertIn('未核验',text)
        self.assertIn('不会自动导入',out['text'])
        self.assertIn('尚无历史',out['text'])

    def test_desktop_nodes_guide_failure_is_actionable_and_never_claims_no_history(self):
        out=self.app('''console.log(JSON.stringify({hidden:__el('nodes-data-guide').hidden,
          text:__el('nodes-data-status').textContent}));''',
          desktop=True,data_status={'ok':False,'msg':'boom'})
        self.assertFalse(out['hidden'])
        self.assertIn('无法读取',out['text'])
        self.assertIn('检查',out['text'])
        self.assertNotIn('尚无历史',out['text'])

    def test_nodes_guide_path_is_plain_text_not_html(self):
        hostile='/data/<img src=x onerror="alert(1)"> 目录'
        out=self.app('''console.log(JSON.stringify({text:__el('nodes-data-status').textContent,
          html:__el('nodes-data-status').innerHTML}));''',
          desktop=True,data_status=_fresh_status(data_home=hostile))
        self.assertIn(hostile,out['text'])
        self.assertEqual(out['html'],'')

    def test_browser_nodes_guide_card_stays_hidden(self):
        out=self.app('''console.log(JSON.stringify({hidden:__el('nodes-data-guide').hidden}));''',
          desktop=False,data_status=_fresh_status())
        self.assertTrue(out['hidden'])

    def test_guide_button_shows_settings_and_focuses_import_directory(self):
        out=self.app('''const dir=__el('history-import-directory');dir.value='我 的/旧 目录';
          let focusCount=0;dir.focus=()=>{focusCount++;};
          pendingHistoryImport={token:'keep',directory:'我 的/旧 目录'};
          __el('view-settings').style.display='none';__requests.length=0;
          __el('btn-open-history-import').__listeners.click[0]();
          console.log(JSON.stringify({hash:window.location.hash,settings:__el('view-settings').style.display,
            focusCount,value:dir.value,pending:pendingHistoryImport&&pendingHistoryImport.token,
            requests:__requests.map(r=>r.url)}));''',
          desktop=True,data_status=_fresh_status())
        self.assertEqual(out['hash'],'#/settings')
        self.assertEqual(out['settings'],'')
        self.assertEqual(out['focusCount'],1)
        self.assertEqual(out['value'],'我 的/旧 目录')
        self.assertEqual(out['pending'],'keep')
        self.assertFalse(any('/history-import/' in u for u in out['requests']))
