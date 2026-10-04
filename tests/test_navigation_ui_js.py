"""Production navigation handlers; native button activation is checked in browser QA."""
from html.parser import HTMLParser
import unittest
from tests.test_resume_js import NODE, INDEX_HTML
from tests import test_task_ui_js as task_ui


class Tags(HTMLParser):
    def __init__(self, html):
        super().__init__(); self.tags=[]; self.feed(html)
    def handle_starttag(self, tag, attrs):self.tags.append((tag,dict(attrs)))


@unittest.skipUnless(NODE,'Node unavailable')
class NavigationUiJsTest(unittest.TestCase):
    def run_app(self, driver):return task_ui.TaskUiJsTest.run_app(self,driver)

    def test_history_run_click_retains_stable_id_and_focus_when_no_champion(self):
        out=self.run_app("""
          (async()=>{await new Promise(r=>setTimeout(r,0));
            const nid='node_v2_'+'a'.repeat(32);let trends=[],focus=[],renders=0;
            histData=[{ts:'fixture <旧> " 🇭🇰',results:[{name:'same',node_id:nid,status:'timeout',score:0}]}];
            histSelRun=-1;renderHistTable=()=>{renders++;};fetchNodeTrend=name=>trends.push([name,histSelNodeId]);
            const box=__el('hist-list');box.querySelectorAll=()=>[{dataset:{i:'0'},focus:()=>focus.push('0')}];
            const item={dataset:{i:'0'}};
            const e={target:{closest:()=>item}};
            box.__listeners.click[0](e);
            console.log(JSON.stringify({html:box.innerHTML,trends,focus,renders,clicks:box.__listeners.click.length,
              keys:(box.__listeners.keydown||[]).length}));})();
        """)
        self.assertEqual(out['trends'],[['same','node_v2_'+'a'*32]])
        self.assertEqual(out['focus'],['0']);self.assertEqual(out['renders'],1)
        self.assertEqual((out['clicks'],out['keys']),(1,0))
        tags=Tags(out['html']).tags
        button=next(attrs for tag,attrs in tags if tag=='button')
        self.assertEqual(button['type'],'button');self.assertEqual(button['aria-current'],'true')
        self.assertNotIn('<旧>',out['html']);self.assertIn('&lt;旧&gt;',out['html'])

    def test_subscription_native_button_selects_once_and_returns_focus_without_selector_interpolation(self):
        out=self.run_app("""
          (async()=>{await new Promise(r=>setTimeout(r,0));
            const key='legacy:<" fixture 🇭🇰';let selected=[],focused=[];
            subsLoaded=true;subsSel='';subsData=[{provider:'fixture <订阅> " 🇭🇰',selection_key:key}];
            const realSelect=selectSub;selectSub=name=>{selected.push(name);subsSel=name;renderSubsTable();};
            const box=__el('subs-tbody');box.querySelectorAll=()=>[{dataset:{provider:key},focus:()=>focused.push(key)}];
            box.__listeners.click[0]({target:{closest:()=>({dataset:{provider:key}})}});
            console.log(JSON.stringify({selected,focused,html:box.innerHTML,
              clicks:box.__listeners.click.length,keys:(box.__listeners.keydown||[]).length}));})();
        """)
        key='legacy:<" fixture 🇭🇰'
        self.assertEqual(out['selected'],[key]);self.assertEqual(out['focused'],[key])
        self.assertEqual((out['clicks'],out['keys']),(1,0))
        button=next(attrs for tag,attrs in Tags(out['html']).tags if tag=='button')
        self.assertEqual(button['data-provider'],key);self.assertEqual(button['aria-pressed'],'true')
        self.assertNotIn('<订阅>',out['html'])

    def test_sort_state_nav_current_and_board_expansion_follow_production_handlers(self):
        out=self.run_app("""
          (async()=>{await new Promise(r=>setTimeout(r,0));
            const attrs=()=>({});const navs=['nodes','history'].map(view=>({dataset:{view},classList:{toggle(){}},
              attrs:attrs(),setAttribute(k,v){this.attrs[k]=v;},removeAttribute(k){delete this.attrs[k];}}));
            const cols=['name','network_score'].map(k=>({dataset:{k},arr:{},attrs:attrs(),
              querySelector(){return this.arr;},setAttribute(k,v){this.attrs[k]=v;}}));
            document.querySelectorAll=s=>s==='.nav-item'?navs:cols;
            updateSortArrows('th.sort','name',true);
            histLoaded=true;window.location.hash='#/history';route();
            window.location.hash='#/nodes';route();
            const body=__el('board-body'),button=__el('board-toggle');body.style.display='none';button.attrs={};
            button.setAttribute=(k,v)=>button.attrs[k]=v;
            button.__listeners.click[0]({currentTarget:button});const open=body.style.display;
            button.__listeners.click[0]({currentTarget:button});
            console.log(JSON.stringify({navs:navs.map(n=>n.attrs),sort:cols.map(c=>c.attrs),open,
              expanded:button.attrs['aria-expanded'],keys:(button.__listeners.keydown||[]).length}));})();
        """)
        self.assertEqual(out['navs'],[{'aria-current':'page'},{}])
        self.assertEqual(out['sort'],[{'aria-sort':'ascending'},{'aria-sort':'none'}])
        self.assertEqual(out['open'],'');self.assertEqual(out['expanded'],'false');self.assertEqual(out['keys'],0)

    def test_nested_button_keys_do_not_activate_result_rows_again(self):
        out=self.run_app("""
          (async()=>{await new Promise(r=>setTimeout(r,0));let clicks=0,prevent=0;
            const handler=__el('tbody').__listeners.keydown[0];
            for(const key of ['Enter',' ']){
              handler({key,target:{tagName:'BUTTON',click:()=>clicks++},preventDefault:()=>prevent++});
              handler({key,target:{tagName:'TR',click:()=>clicks++},preventDefault:()=>prevent++});
            }
            console.log(JSON.stringify({clicks,prevent}));})();
        """)
        self.assertEqual(out,{'clicks':2,'prevent':2})

    def test_history_row_keys_use_same_click_selection_without_changing_round(self):
        out=self.run_app("""
          (async()=>{await new Promise(r=>setTimeout(r,0));histSelRun=0;let clicks=0,trends=0;
            gotoTrend=()=>{trends++;histSelRun=2;};
            const handler=__el('hist-tbody').__listeners.keydown[0];
            handler({key:'Enter',target:{tagName:'TR',dataset:{name:'fixture'},click:()=>clicks++},preventDefault(){}});
            console.log(JSON.stringify({clicks,trends,run:histSelRun}));})();
        """)
        self.assertEqual(out,{'clicks':1,'trends':0,'run':0})

    def test_leak_button_recovers_disabled_focus_without_stealing_user_moved_focus(self):
        out=self.run_app("""
          (async()=>{await new Promise(r=>setTimeout(r,0));const run=__el('btn-leak-run');let focuses=0;
            run.focus=()=>{focuses++;document.activeElement=run;};
            post=async()=>({status:'unknown'});collectWebRTCCandidates=async()=>({candidates:[],policy_blocked:true});
            document.activeElement=run;browserExitIp=async()=>{document.activeElement=document.body;return null;};
            await runLeakAudit();const recovered=document.activeElement===run;
            const other=__el('dns-status');document.activeElement=run;
            browserExitIp=async()=>{document.activeElement=other;return null;};await runLeakAudit();
            console.log(JSON.stringify({focuses,recovered,kept:document.activeElement===other,disabled:run.disabled}));})();
        """)
        self.assertEqual(out,{'focuses':1,'recovered':True,'kept':True,'disabled':False})


class NavigationStructureTest(unittest.TestCase):
    def test_sort_buttons_preserve_table_headers_and_controls_have_labels(self):
        html=INDEX_HTML.read_text(encoding='utf-8');tags=Tags(html).tags
        headers=[attrs for tag,attrs in tags if tag=='th' and attrs.get('class') in ('sort','hsort')]
        buttons=[attrs for tag,attrs in tags if tag=='button' and attrs.get('class')=='th-sort']
        self.assertEqual(len(headers),len(buttons));self.assertEqual(len(buttons),9)
        self.assertTrue(all(a['type']=='button' for a in buttons))
        toggle=next(a for t,a in tags if a.get('id')=='board-toggle')
        self.assertEqual(toggle['aria-controls'],'board-body');self.assertEqual(toggle['aria-expanded'],'false')
        self.assertTrue(any(t=='label' and a.get('for')=='subs-days' for t,a in tags))
