# -*- coding: utf-8 -*-
"""前端 JS（web/app.js 全文）逻辑测试：抽取后用 node 真执行。

做法：stub 最小 DOM / localStorage / fetch，把 app.js 原样跑一遍（含末尾的
init() 事件绑定与 loadLatest/loadHistory/loadCurrent 启动链路），再在同一词法
作用域追加驱动代码直接调用其顶层函数，结果 JSON 打到 stdout 由 Python 断言：

- 5 个评分 Profile（含多源 ipclean/residential）公式数值样例 + 沉底规则
- regionOf 地区启发式：country_code 优先 > 国旗 emoji > 中/英关键词 > '??' 兜底
- Profile 选择与收藏节点的 localStorage 持久化

系统没有 node（shutil.which）时执行类测试整组 skip；抽取/结构测试不需要 node。
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import speedbench_web as web

NODE = shutil.which("node")

# v0.6 起前端从内嵌 PAGE 抽成静态文件；被测对象即服务端分发的 app.js 原文
APP_JS = Path(web.__file__).resolve().parent / "web" / "app.js"

# 只实现页面脚本实际用到的那一面的最小 stub
STUB_JS = r"""
// ===== 最小 DOM / localStorage / fetch stub =====
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
      classList:{ add(){}, remove(){}, toggle(){}, contains(){ return false; } },
      addEventListener(){}, getContext(){ return __ctx2d; },
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
const window = { devicePixelRatio:1, addEventListener(){} };
const __store = new Map();
const localStorage = {
  getItem(k){ return __store.has(k) ? __store.get(k) : null; },
  setItem(k,v){ __store.set(k, String(v)); },
  removeItem(k){ __store.delete(k); },
  clear(){ __store.clear(); },
};
for(const [k,v] of Object.entries(__PRESEED__)) localStorage.setItem(k,v);
async function fetch(){ return { json: async () => ({}) }; }
"""

# 与页面脚本同一词法作用域：可直接读写其顶层 let/const/function 绑定
DRIVER_JS = r"""
// ===== 测试驱动 =====
const R = {};
const N = v => (v === null || v === undefined) ? "__NULL__"
             : (typeof v === "number" && Number.isNaN(v)) ? "__NAN__" : v;
const prof = (p, r) => { currentProfile = p; return N(profileScore(r)); };

R.initial_profile = currentProfile;   // 启动时从 localStorage 恢复（或默认 all）
R.fav_seeded = favs.has("节点A");

// —— Profile 公式数值样例与沉底规则 ——
R.daily_perfect         = prof("daily", {latency_ms:80,  jitter_ms:10,  median_mbps:100});
R.daily_worst           = prof("daily", {latency_ms:800, jitter_ms:200, median_mbps:0});
R.daily_mid_null_jitter = prof("daily", {latency_ms:440, jitter_ms:null, median_mbps:50});
R.daily_no_latency      = prof("daily", {latency_ms:null, jitter_ms:10, median_mbps:50});
R.download_capped       = prof("download", {median_mbps:300, multi_mbps:500});
R.download_half         = prof("download", {median_mbps:150, multi_mbps:250});
R.download_multi_fallback = prof("download", {median_mbps:30});   // multi 缺失用 median 兜底
R.download_no_median    = prof("download", {median_mbps:null, multi_mbps:100});
R.ipclean_proxy         = prof("ipclean", {ip:{ok:true, proxy:true, hosting:true, mobile:true}});
R.ipclean_hosting       = prof("ipclean", {ip:{ok:true, hosting:true, mobile:true}});
R.ipclean_mobile        = prof("ipclean", {ip:{ok:true, mobile:true}});
R.ipclean_clean         = prof("ipclean", {ip:{ok:true}});
R.ipclean_legacy_neutral = prof("ipclean", {ip:{ok:true, kind:"ISP/非托管",
  proxy:false, hosting:false, mobile:false}});
R.ipclean_not_ok        = prof("ipclean", {ip:{ok:false}});
R.ipclean_missing       = prof("ipclean", {});

// —— 多源 IP intelligence fixtures ——
const intel = (category, confidence, quality, extra={}) => Object.assign({
  ip: "203.0.113.10",
  classification: {category, confidence},
  confidence,
  ip_quality_score: quality,
  ip_grade: quality>=90 ? "S" : quality>=75 ? "A" : quality>=60 ? "B" : quality>=40 ? "C" : "D",
  ipqs_fraud_score: 5,
  scamalytics_score: 5,
  hosting: false,
  proxy: false,
  vpn: false,
  tor: false,
  mobile: false,
  residential_proxy: false,
  ipqs_recent_abuse: false,
  scamalytics_blacklisted: false,
  scamalytics_datacenter: false,
}, extra);
const intelResidential = intel("residential", 94, 92);
const intelCorporate = intel("corporate", 90, 86);
const intelMobile = intel("mobile", 88, 80, {mobile:true});
const intelResidentialProxy = intel("residential_proxy", 89, 76, {residential_proxy:true});
const intelDatacenter = intel("datacenter", 88, 82, {hosting:true, scamalytics_datacenter:true});
const intelVpn = intel("vpn_proxy", 85, 72);
const intelHighRisk = intel("residential_proxy", 90, 12, {
  residential_proxy:true, proxy:true, ipqs_fraud_score:96,
  scamalytics_score:91, ipqs_recent_abuse:true, scamalytics_blacklisted:true,
});
const intelUnknown = intel("unknown", 0, 100, {ip_grade:"S"});
R.ipclean_intel_clean = prof("ipclean", {intel_v4:intelResidential});
R.ipclean_intel_high_fraud = prof("ipclean", {intel_v4:intelHighRisk});
R.ipclean_intel_unknown = prof("ipclean", {intel_v4:intelUnknown});
R.ipclean_dual_stack_worst = prof("ipclean", {
  intel_v4:intelResidential, intel_v6:intelHighRisk,
});
R.residential_dual_stack_worst = prof("residential", {
  intel_v4:intelResidential, intel_v6:intelDatacenter,
});
const residentialRows = [
  {name:"unknown", intel_v4:intelUnknown},
  {name:"high-risk", intel_v4:intelHighRisk},
  {name:"vpn", intel_v4:intelVpn},
  {name:"datacenter", intel_v4:intelDatacenter},
  {name:"residential-proxy", intel_v4:intelResidentialProxy},
  {name:"mobile", intel_v4:intelMobile},
  {name:"corporate", intel_v4:intelCorporate},
  {name:"residential", intel_v4:intelResidential},
];
currentProfile = "residential";
sortRows(residentialRows, "score", false);
R.residential_order = residentialRows.map(x=>x.name);
R.residential_legacy_clean = prof("residential", {ip:{ok:true, kind:"ISP/非托管", hosting:false, mobile:false}});

// 🚀下载必须只依赖带宽字段；相同带宽下改变 IP intelligence 不得改变分数/顺序。
const downloadClean = prof("download", {median_mbps:150, multi_mbps:250,
  intel_v4:intelResidential});
const downloadRisk = prof("download", {median_mbps:150, multi_mbps:250,
  intel_v4:intelHighRisk});
R.download_ip_independent = downloadClean===downloadRisk;
const downloadRows = [
  {name:"risk", median_mbps:150, multi_mbps:250, intel_v4:intelHighRisk},
  {name:"clean", median_mbps:150, multi_mbps:250, intel_v4:intelResidential},
];
currentProfile = "download";
sortRows(downloadRows, "score", false);
R.download_order_same = downloadRows.map(x=>x.name);

currentProfile = "all";
R.all_passthrough       = N(profileScore({score:66.6}));  // 综合推荐：后端分数原样
R.all_null              = N(profileScore({}));

// —— regionOf 地区启发式 ——
R.cc_wins_over_keyword   = regionOf({ip:{ok:true, country_code:"jp"}, name:"美国节点"});
R.flag_emoji             = regionOf({name:"🇯🇵 东京 03"});
R.flag_beats_keyword     = regionOf({ip:{ok:false}, name:"🇭🇰 美国 01"});
R.keyword_chinese        = regionOf({name:"新加坡 02"});
R.keyword_english        = regionOf({name:"US West 01"});
R.keyword_case_sensitive = regionOf({name:"Plus Ultra"});   // 小写 us 不该误中 US
R.fallback               = regionOf({name:"神秘节点"});
R.no_name                = regionOf({});
R.cc_ignored_when_not_ok = regionOf({ip:{ok:false, country_code:"US"}, name:"节点"});
R.flag_too_short         = N(flagCode("a"));   // 不足两个码点
R.keyword_plus           = N(keywordCode("Plus"));

// —— localStorage 持久化 ——
toggleFav("节点B");
R.favs_json = localStorage.getItem("sb_favs");
setProfile("daily");
R.persisted_profile = localStorage.getItem("sb_profile");

console.log("##RESULTS##" + JSON.stringify(R));
"""


def extract_app_js():
    """web/app.js 全文：顶层绑定名与旧版内嵌 <script> 一致，可整文件直接执行。"""
    return APP_JS.read_text(encoding="utf-8")


class AppJsExtractionTest(unittest.TestCase):
    """不需要 node：验证 app.js 可读且被测函数都在。"""

    def test_app_js_has_target_functions(self):
        js = extract_app_js()
        for fn in ("function profileScore", "function regionOf",
                   "function flagCode", "function keywordCode",
                   "function setProfile", "function toggleFav"):
            self.assertIn(fn, js)


@unittest.skipIf(NODE is None, "系统没有 node，跳过前端 JS 执行测试")
class AppJsLogicTest(unittest.TestCase):
    MARK = "##RESULTS##"

    PROFILE_EXPECTED = {
        # daily = 0.5×lat + 0.3×jitter + 0.2×min(mbps,100)
        "daily_perfect": 100.0,
        "daily_worst": 0.0,
        "daily_mid_null_jitter": 50.0,   # jitter 缺失按中性 50 计
        "daily_no_latency": "__NULL__",  # 无延迟 → 沉底
        # download = 0.7×min(mbps,300)/3 + 0.3×min(multi,500)/5
        "download_capped": 100.0,
        "download_half": 50.0,
        "download_multi_fallback": 8.8,  # 0.7×10 + 0.3×6
        "download_no_median": "__NULL__",
        # ipclean = 代理 20 / 托管 50 / 移动 75 / legacy 中性 60
        "ipclean_proxy": 20,
        "ipclean_hosting": 50,
        "ipclean_mobile": 75,
        "ipclean_clean": 60,
        "ipclean_legacy_neutral": 60,
        "ipclean_not_ok": "__NULL__",
        "ipclean_missing": "__NULL__",
        # structured intelligence uses quality/fraud/flags conservatively;
        # unknown/N/A is never promoted to a clean 100.
        "ipclean_intel_clean": 92.0,
        "ipclean_intel_high_fraud": 4.0,
        "ipclean_intel_unknown": 0.0,
        "ipclean_dual_stack_worst": 4.0,
        "residential_dual_stack_worst": 395.0,
        "residential_legacy_clean": 500.0,
        "download_ip_independent": True,
        # all：后端 score 原样；无 score → null 沉底
        "all_passthrough": 66.6,
        "all_null": "__NULL__",
    }

    REGION_EXPECTED = {
        "cc_wins_over_keyword": "JP",   # country_code 最优先，且转大写
        "flag_emoji": "JP",             # 🇯🇵 → JP
        "flag_beats_keyword": "HK",     # 国旗优先于「美国」关键词
        "keyword_chinese": "SG",
        "keyword_english": "US",        # \bUS\b 词边界
        "keyword_case_sensitive": "??", # 小写 "us"（Plus）不误中 US
        "fallback": "??",
        "no_name": "??",
        "cc_ignored_when_not_ok": "??", # ip.ok=false 时 country_code 不可用
        "flag_too_short": "__NULL__",
        "keyword_plus": "__NULL__",
    }

    def run_app_js(self, preseed=None):
        js = (STUB_JS.replace("__PRESEED__", json.dumps(preseed or {}))
              + extract_app_js() + DRIVER_JS)
        with tempfile.TemporaryDirectory() as td:
            script = Path(td) / "app_test.js"
            script.write_text(js, encoding="utf-8")
            proc = subprocess.run([NODE, str(script)], capture_output=True,
                                  encoding="utf-8", timeout=30)
        self.assertEqual(proc.returncode, 0,
                         msg=f"node 执行 app.js 失败:\n{proc.stderr}\n{proc.stdout[-2000:]}")
        for line in proc.stdout.splitlines():
            if line.startswith(self.MARK):
                return json.loads(line[len(self.MARK):])
        self.fail(f"node 输出里没有结果标记:\n{proc.stdout}\n{proc.stderr}")

    def check_expected(self, results, expected):
        for key, want in expected.items():
            with self.subTest(key=key):
                got = results.get(key, "<missing>")
                if isinstance(want, str):
                    self.assertEqual(got, want)
                else:
                    self.assertIsInstance(got, (int, float),
                                          f"{key} 应为数值，实际 {got!r}")
                    self.assertAlmostEqual(got, want, places=6)

    def test_profile_formulas_and_null_rule(self):
        r = self.run_app_js()
        self.check_expected(r, self.PROFILE_EXPECTED)
        self.assertLess(r["ipclean_legacy_neutral"], 100,
                        "仅 ip-api 的中性 all-false 信号不得冒充 clean intelligence")
        self.assertEqual(r["residential_order"], [
            "residential", "corporate", "mobile", "residential-proxy",
            "datacenter", "vpn", "high-risk", "unknown",
        ])
        self.assertEqual(r["download_order_same"], ["risk", "clean"])
        self.assertEqual(r["initial_profile"], "all")  # 空存储默认综合推荐

    def test_region_of_heuristics(self):
        r = self.run_app_js()
        self.check_expected(r, self.REGION_EXPECTED)

    def test_localstorage_persistence(self):
        r = self.run_app_js(preseed={"sb_profile": "download",
                                     "sb_favs": '["节点A"]'})
        self.assertEqual(r["initial_profile"], "download")  # 启动恢复上次 Profile
        self.assertIs(r["fav_seeded"], True)                # 启动恢复收藏集
        self.assertIsInstance(r["favs_json"], str)
        self.assertEqual(set(json.loads(r["favs_json"])), {"节点A", "节点B"})
        self.assertEqual(r["persisted_profile"], "daily")   # setProfile 写回


if __name__ == "__main__":
    unittest.main()
