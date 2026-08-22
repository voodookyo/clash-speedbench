# -*- coding: utf-8 -*-
"""前端 boot 接管进行中测速（resumeRun）与进度文本（pollStatus）测试。

照 tests/test_profiles_js.py 的模式：stub 最小 DOM / localStorage / fetch /
定时器，把 web/app.js 原样跑一遍（末尾 boot() 自动执行 init + resumeRun 启动
链路），再在同一词法作用域追加驱动代码——先让异步微任务链跑完，再读 stub
状态，结果 JSON 打到 stdout 由 Python 断言：

- /api/run/status 报 running:true：boot 后 setRunUi(true)（btn-run 置灰、
  prog-wrap 露出）+ startPolling 起 1200ms 单例轮询；日志区填入已有 lines；
  prog-text 认「Phase 2 精测 [3/15]」显示 "Phase 2 精测 3/15"；进度条跳到 3/15
- running:true 但日志无 Phase 标签：prog-text 退化显示百分比
- running:false：不起轮询、不动运行态 UI
- 两处退出按钮（btn-quit / btn-quit-2）都绑了 click（quitPanel）
- index.html 静态结构：两个退出按钮文案含「退出」、prog-wrap/prog/prog-text
  存在（无 inline 事件属性已由 test_web_security.py::test_no_inline_event_handlers
  对 index.html 与 app.js 两份源覆盖，此处不重复）

系统没有 node（shutil.which）时执行类测试整组 skip；index.html 结构测试不需要 node。
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import speedbench_web as web

NODE = shutil.which("node")

WEB_DIR = Path(web.__file__).resolve().parent / "web"
APP_JS = WEB_DIR / "app.js"          # 被测对象即服务端分发的 app.js 原文
INDEX_HTML = WEB_DIR / "index.html"

# 只实现页面脚本实际用到的那一面的最小 stub（在 test_profiles_js 版本上加：
# 定时器记录、fetch 按 URL 返回预设、addEventListener 留痕）
STUB_JS = r"""
// ===== 最小 DOM / localStorage / fetch / 定时器 stub =====
const __ctx2d = {
  clearRect(){}, beginPath(){}, moveTo(){}, lineTo(){}, stroke(){}, fill(){},
  arc(){}, fillText(){}, fillStyle:"", strokeStyle:"", font:"", lineWidth:0,
};
const __elements = {};
function __el(id){
  if(!__elements[id]){
    __elements[id] = {
      style:{}, dataset:{}, innerHTML:"", textContent:"", value:"",
      checked:false, disabled:false, scrollTop:0, scrollHeight:0,
      clientWidth:800, clientHeight:240, width:0, height:0,
      __listeners:{},
      classList:{ add(){}, remove(){}, toggle(){}, contains(){ return false; } },
      addEventListener(ev, fn){
        (this.__listeners[ev] = this.__listeners[ev] || []).push(fn);
      },
      getContext(){ return __ctx2d; },
      querySelector(){ return {textContent:""}; },
      appendChild(){}, remove(){},
    };
  }
  return __elements[id];
}
const document = {
  querySelector(){ return {content:"stub-token", textContent:""}; },
  querySelectorAll(){ return []; },
  getElementById: __el,
  createElement(){ return { className:"", textContent:"",
    classList:{add(){},remove(){}}, remove(){} }; },
  body: {},
};
const window = { devicePixelRatio:1, location:{hash:""}, addEventListener(){} };
const __store = new Map();
const localStorage = {
  getItem(k){ return __store.has(k) ? __store.get(k) : null; },
  setItem(k,v){ __store.set(k, String(v)); },
  removeItem(k){ __store.delete(k); },
  clear(){ __store.clear(); },
};
// 定时器 stub：只记录不真调度（真调度会让 node 事件循环空转不退）
const __intervals = [];
const __cleared = [];
function setInterval(fn, ms){ __intervals.push({ms}); return __intervals.length; }
function clearInterval(id){ __cleared.push(id); }
// fetch 按 URL 返回预设（Python 注入 __FETCH_MAP__），未知 URL 给 {}
const __fetchMap = __FETCH_MAP__;
const __fetchCalls = [];
async function fetch(url){
  __fetchCalls.push(String(url));
  const payload = Object.prototype.hasOwnProperty.call(__fetchMap, url)
    ? __fetchMap[url] : {};
  return { json: async () => JSON.parse(JSON.stringify(payload)) };
}
"""

# 与页面脚本同一词法作用域：boot() 的 resumeRun/loadLatest 都是异步，
# 先用真 setTimeout 让微任务链跑完，再读 stub 状态
DRIVER_JS = r"""
// ===== 测试驱动 =====
(async ()=>{
  await new Promise(r=>setTimeout(r, 0));
  const listeners = id => (document.getElementById(id).__listeners.click || []).length;
  const R = {
    intervals: __intervals.map(i=>i.ms),
    status_calls: __fetchCalls.filter(u=>u==="/api/run/status").length,
    btn_run_disabled: document.getElementById('btn-run').disabled,
    btn_cancel_display: document.getElementById('btn-cancel').style.display,
    prog_wrap_display: document.getElementById('prog-wrap').style.display,
    prog_text: document.getElementById('prog-text').textContent,
    prog_value: document.getElementById('prog').value,
    log_text: document.getElementById('log').textContent,
    log_display: document.getElementById('log').style.display,
    quit1_click: listeners('btn-quit'),
    quit2_click: listeners('btn-quit-2'),
  };
  console.log("##RESULTS##"+JSON.stringify(R));
})();
"""

PHASE2_LINES = [
    "Phase 1 粗筛 [ 15/15] 节点A | 74±2 ms（主实例）",
    "Phase 1 粗筛完成，耗时 3.2s（15 节点）",
    "Phase 2 精测 [  3/15] 节点B | 80 ms | 85.0 Mbps",
]

FETCH_RUNNING_PHASE2 = {
    "/api/run/status": {"running": True, "exit_code": None,
                        "lines": PHASE2_LINES},
    "/api/latest": {},
    "/api/current": {"ok": False},
}

FETCH_RUNNING_NO_PHASE = {
    "/api/run/status": {"running": True, "exit_code": None,
                        "lines": ["正在测速", "[3/10] 节点X | 120 ms"]},
    "/api/latest": {},
    "/api/current": {"ok": False},
}

FETCH_IDLE = {
    "/api/run/status": {"running": False, "exit_code": 0, "lines": []},
    "/api/latest": {},
    "/api/current": {"ok": False},
}


def extract_app_js():
    return APP_JS.read_text(encoding="utf-8")


@unittest.skipIf(NODE is None, "系统没有 node，跳过前端 JS 执行测试")
class ResumeRunJsTest(unittest.TestCase):
    MARK = "##RESULTS##"

    def run_app_js(self, fetch_map):
        js = (STUB_JS.replace("__FETCH_MAP__",
                              json.dumps(fetch_map, ensure_ascii=False))
              + extract_app_js() + DRIVER_JS)
        with tempfile.TemporaryDirectory() as td:
            script = Path(td) / "app_resume_test.js"
            script.write_text(js, encoding="utf-8")
            proc = subprocess.run([NODE, str(script)], capture_output=True,
                                  encoding="utf-8", timeout=30)
        self.assertEqual(proc.returncode, 0,
                         msg=f"node 执行 app.js 失败:\n{proc.stderr}\n{proc.stdout[-2000:]}")
        for line in proc.stdout.splitlines():
            if line.startswith(self.MARK):
                return json.loads(line[len(self.MARK):])
        self.fail(f"node 输出里没有结果标记:\n{proc.stdout}\n{proc.stderr}")

    def test_running_true_resumes_polling_and_fills_ui(self):
        r = self.run_app_js(FETCH_RUNNING_PHASE2)
        self.assertEqual(r["intervals"], [1200])       # 1200ms 单例轮询已启动
        # resumeRun 查一次 + startPolling 立即 pollStatus 一次
        self.assertEqual(r["status_calls"], 2)
        self.assertIs(r["btn_run_disabled"], True)     # setRunUi(true)
        self.assertEqual(r["btn_cancel_display"], "")
        self.assertEqual(r["prog_wrap_display"], "flex")
        # 最新一条 Phase 标签行决定 prog-text
        self.assertEqual(r["prog_text"], "Phase 2 精测 3/15")
        self.assertAlmostEqual(r["prog_value"], 3 / 15 * 100)  # 进度条按 [N/M] 跳位
        # 日志区填入已有 lines
        self.assertEqual(r["log_text"], "\n".join(PHASE2_LINES))
        self.assertEqual(r["log_display"], "block")

    def test_running_true_without_phase_label_falls_back_to_percent(self):
        r = self.run_app_js(FETCH_RUNNING_NO_PHASE)
        self.assertEqual(r["intervals"], [1200])
        self.assertEqual(r["prog_text"], "30%")        # 无 Phase 标签退化百分比
        self.assertAlmostEqual(r["prog_value"], 30.0)

    def test_running_false_does_not_start_polling(self):
        r = self.run_app_js(FETCH_IDLE)
        self.assertEqual(r["intervals"], [])           # 不起轮询
        self.assertEqual(r["status_calls"], 1)         # resumeRun 查了一次就返回
        self.assertIs(r["btn_run_disabled"], False)    # 不动运行态 UI
        self.assertNotEqual(r.get("prog_wrap_display"), "flex")
        self.assertFalse(r.get("prog_text"))

    def test_quit_buttons_bound_to_click(self):
        r = self.run_app_js(FETCH_IDLE)
        self.assertEqual(r["quit1_click"], 1)          # 导航底部 → quitPanel
        self.assertEqual(r["quit2_click"], 1)          # 「关于」视图 → quitPanel


class IndexHtmlStructureTest(unittest.TestCase):
    """不需要 node：退出按钮与进度元素的静态结构断言。"""

    @classmethod
    def setUpClass(cls):
        cls.html = INDEX_HTML.read_text(encoding="utf-8")

    def button_tag(self, btn_id):
        m = re.search(r"<button[^>]*id=\"%s\"[^>]*>(.*?)</button>" % re.escape(btn_id),
                      self.html, re.S)
        self.assertIsNotNone(m, f"index.html 里没有 #{btn_id}")
        return m.group(0)

    def test_quit_buttons_exist_with_label(self):
        for btn_id in ("btn-quit", "btn-quit-2"):
            with self.subTest(btn_id=btn_id):
                tag = self.button_tag(btn_id)
                self.assertIn("退出", tag)
                # 按钮标签本身无 inline 事件属性（全文件扫描见
                # test_web_security.py::test_no_inline_event_handlers）
                opening = tag.split(">", 1)[0].lower()
                for h in ("onclick=", "oninput=", "onchange=",
                          "onload=", "onerror=", "onmouseover="):
                    self.assertNotIn(h, opening)

    def test_progress_elements_exist(self):
        for el_id in ("prog-wrap", "prog", "prog-text"):
            with self.subTest(el_id=el_id):
                self.assertIn(f'id="{el_id}"', self.html)


if __name__ == "__main__":
    unittest.main()
