import json
import subprocess
import unittest

from tests.test_resume_js import NODE, STUB_JS, APP_JS


@unittest.skipUnless(NODE,'Node unavailable')
class HistoryTransferUiTest(unittest.TestCase):
    def app(self,script,*,preview=None,applied=None):
        responses={'/api/catalog':{'status':'ok','nodes':[],'sources':[]},
            '/api/run/status':{'running':False},'/api/history':[],
            '/api/data-status':{'ok':True,'data_home':'fixture','history':{}},
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
