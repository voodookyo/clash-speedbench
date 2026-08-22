# -*- coding: utf-8 -*-
"""内置迷你 YAML 解析器（_parse_verge_yaml）与 extract_proxies fallback 链测试。

fixture 全部手工构造、脱敏（example.com / test-node-xx），不引用任何真实订阅信息。
覆盖：
- 典型 Verge 形态：顶层 keys + proxies indentless sequence（与键同级 0 缩进的
  "- name:"）、ss/vmess/trojan/hysteria2 风格字段组合、嵌套 ws-opts/reality-opts/
  headers、flow list alpn、引号密码、IPv6 server "[::1]"、含 : 与 # 的引号值、
  skip-cert-verify 布尔、带引号键、--- 文档标记
- 标量 resolver 边界（YAML 1.1，与 Psych/PyYAML 对齐）：yes/no/on/off/~、
  前导零八进制、0x/0b、六十进制、下划线数字、float 必须带小数点（1e3 是字符串）
- 注释：整行 / 行尾 / 引号内或 URL 内的 # 不算注释
- 错误路径：anchors、alias、多行 | >、merge key <<、Tab 缩进、未知转义、
  未闭合引号、多文档、跨行 flow → 一律抛 VergeYAMLError（不静默解析错）
- extract_proxies fallback 链：PyYAML 优先（装了才测）→ 非 win32 走 ruby →
  迷你解析器兜底；win32 跳过 ruby；全失败聚合 WorkerUnavailable
"""
import math
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import speedbench_workers as sbw

try:
    import yaml as _pyyaml
except ImportError:
    _pyyaml = None


# ---------------- 手工构造的脱敏 Verge 配置 fixture ----------------
# 形态对齐 Clash Verge Rev 机器生成的 clash-verge.yaml：
# 顶层 mapping、proxies 用 indentless sequence（- name: 与 proxies: 同缩进），
# 覆盖 ss/vmess(ws)/trojan/hysteria2/vmess(reality) 五种字段组合。
VERGE_CFG = """# Clash Verge 机器生成配置（脱敏假数据）
---
mixed-port: 7897
allow-lan: false
mode: rule
log-level: warning
external-controller: 127.0.0.1:9097
"quoted-key": quoted-value
dns:
  enable: true
  enhanced-mode: fake-ip
  nameserver:
  - 223.5.5.5
  - 1.1.1.1
proxies:
- name: "test-ss-01 #example"
  type: ss
  server: ss01.example.com
  port: 8388
  cipher: aes-128-gcm
  password: "p@ss:word#frag"   # 行尾注释（引号内 : 与 # 都是值的一部分）
  udp: true
- name: test-vmess-02
  type: vmess
  server: vm02.example.com
  port: 443
  uuid: "12345678-1234-1234-1234-123456789abc"
  alterId: 0
  cipher: auto
  tls: true
  network: ws
  servername: vm02.example.com
  ws-opts:
    path: "/ws?token=abc#frag"
    headers:
      Host: vm02.example.com
- name: test-trojan-03
  type: trojan
  server: tr03.example.com
  port: 443
  password: 'trojan''s quoted'
  sni: tr03.example.com
  skip-cert-verify: false
  alpn: [h2, http/1.1]
- name: test-hy2-04
  type: hysteria2
  server: "[::1]"
  port: 8443
  password: hy2pass
  sni: hy2.example.com
  skip-cert-verify: true
  alpn:
  - h3
  up: 30 Mbps
  down: 200 Mbps
- name: test-vmess-reality-05
  type: vmess
  server: rl05.example.com
  port: 443
  uuid: 12345678-1234-1234-1234-123456789abc
  tls: true
  network: tcp
  reality-opts:
    public-key: abcdef123456
    short-id: "0123abcd"
  client-fingerprint: chrome
proxy-groups:
- name: PROXY
  type: select
  proxies: [test-ss-01, test-vmess-02, test-trojan-03]
- name: AUTO
  type: url-test
  proxies:
  - test-ss-01
  - test-hy2-04
  url: http://www.gstatic.com/generate_204
  interval: 300
rules:
- DOMAIN-SUFFIX,example.com,PROXY
- MATCH,AUTO
"""

# serde_yaml 风格：序列缩进 2 格（非 indentless），与上面等价的最小变体
VERGE_CFG_INDENTED = """proxies:
  - name: test-ss-01
    type: ss
    server: ss01.example.com
    port: 8388
    udp: true
  - name: test-trojan-02
    type: trojan
    server: tr02.example.com
    port: 443
    alpn: [h2, http/1.1]
rules:
  - MATCH,DIRECT
"""


class VergeShapeTest(unittest.TestCase):
    """典型 Verge 形态的端到端解析（fixture 见模块顶部）。"""

    @classmethod
    def setUpClass(cls):
        cls.doc = sbw._parse_verge_yaml(VERGE_CFG)

    def test_top_level_scalars(self):
        doc = self.doc
        self.assertEqual(doc["mixed-port"], 7897)
        self.assertIs(doc["allow-lan"], False)
        self.assertEqual(doc["mode"], "rule")
        self.assertEqual(doc["external-controller"], "127.0.0.1:9097")
        # 带引号的键
        self.assertEqual(doc["quoted-key"], "quoted-value")

    def test_nested_mapping_and_sequence(self):
        dns = self.doc["dns"]
        self.assertIs(dns["enable"], True)
        self.assertEqual(dns["enhanced-mode"], "fake-ip")
        # block 标量序列；IP 形如 223.5.5.5 保持字符串（不是 float）
        self.assertEqual(dns["nameserver"], ["223.5.5.5", "1.1.1.1"])
        for ns in dns["nameserver"]:
            self.assertIsInstance(ns, str)

    def test_proxies_indentless_sequence(self):
        proxies = self.doc["proxies"]
        self.assertEqual([p["name"] for p in proxies],
                         ["test-ss-01 #example", "test-vmess-02", "test-trojan-03",
                          "test-hy2-04", "test-vmess-reality-05"])

    def test_ss_node_quoted_password_with_colon_and_hash(self):
        ss = self.doc["proxies"][0]
        self.assertEqual(ss["type"], "ss")
        self.assertEqual(ss["port"], 8388)
        # 双引号值里的 : 与 # 原样保留；行尾注释被剥掉
        self.assertEqual(ss["password"], "p@ss:word#frag")
        self.assertIs(ss["udp"], True)

    def test_vmess_ws_nested_opts(self):
        vm = self.doc["proxies"][1]
        self.assertEqual(vm["alterId"], 0)               # int
        self.assertIs(vm["tls"], True)
        self.assertEqual(vm["ws-opts"]["path"], "/ws?token=abc#frag")
        self.assertEqual(vm["ws-opts"]["headers"]["Host"], "vm02.example.com")
        # 双引号 uuid 与纯文本 uuid 解析结果一致（都是字符串）
        self.assertEqual(vm["uuid"], self.doc["proxies"][4]["uuid"])

    def test_trojan_squote_escape_and_flow_list(self):
        tr = self.doc["proxies"][2]
        self.assertEqual(tr["password"], "trojan's quoted")   # '' 转义
        self.assertIs(tr["skip-cert-verify"], False)          # false 是布尔不是字符串
        self.assertEqual(tr["alpn"], ["h2", "http/1.1"])      # flow list

    def test_hysteria2_ipv6_and_block_list(self):
        hy = self.doc["proxies"][3]
        self.assertEqual(hy["server"], "[::1]")               # 引号 IPv6 保持字符串
        self.assertIs(hy["skip-cert-verify"], True)
        self.assertEqual(hy["alpn"], ["h3"])                  # 值后续缩进的 block 序列
        self.assertEqual(hy["up"], "30 Mbps")                 # 带空格 plain 标量

    def test_reality_opts_nested(self):
        rl = self.doc["proxies"][4]
        self.assertEqual(rl["reality-opts"]["public-key"], "abcdef123456")
        self.assertEqual(rl["reality-opts"]["short-id"], "0123abcd")

    def test_proxy_groups_flow_and_block_forms(self):
        groups = self.doc["proxy-groups"]
        self.assertEqual(groups[0]["proxies"],
                         ["test-ss-01", "test-vmess-02", "test-trojan-03"])  # flow
        self.assertEqual(groups[1]["proxies"], ["test-ss-01", "test-hy2-04"])  # block
        self.assertEqual(groups[1]["interval"], 300)

    def test_rules_are_plain_strings(self):
        self.assertEqual(self.doc["rules"],
                         ["DOMAIN-SUFFIX,example.com,PROXY", "MATCH,AUTO"])

    def test_indented_sequence_variant_equal(self):
        """serde_yaml 的 2 格缩进序列与 indentless 写法结果一致。"""
        doc = sbw._parse_verge_yaml(VERGE_CFG_INDENTED)
        self.assertEqual(len(doc["proxies"]), 2)
        self.assertEqual(doc["proxies"][0]["name"], "test-ss-01")
        self.assertIs(doc["proxies"][0]["udp"], True)
        self.assertEqual(doc["proxies"][1]["alpn"], ["h2", "http/1.1"])
        self.assertEqual(doc["rules"], ["MATCH,DIRECT"])

    def test_document_markers_accepted(self):
        # --- 开头 / ... 结尾都被接受
        doc = sbw._parse_verge_yaml("---\na: 1\n...\n")
        self.assertEqual(doc, {"a": 1})


class ScalarResolverTest(unittest.TestCase):
    """plain 标量 resolver 的 YAML 1.1 边界（期望值与 Psych/PyYAML 逐一对拍过）。"""

    CASES = {
        # YAML 1.1 布尔（yes/no/on/off 也算）
        "yes": True, "Yes": True, "YES": True,
        "on": True, "On": True, "ON": True,
        "no": False, "No": False, "NO": False,
        "off": False, "Off": False, "OFF": False,
        "true": True, "false": False,
        # null
        "~": None, "null": None, "Null": None, "NULL": None,
        # int：十进制 / 前导零八进制 / 0x / 0b / 六十进制 / 下划线
        "0": 0, "+12": 12, "-3": -3,
        "017": 15, "-017": -15,
        "0x1F": 31, "-0x1f": -31,
        "0b101": 5,
        "12:34": 754, "-1:02": -62,
        "1_000": 1000,
        # float：必须带小数点（1e3 是字符串；指数须带符号才是 float）
        "1.5": 1.5, "-0.5": -0.5, ".5": 0.5, "1.": 1.0,
        "1.5e+2": 150.0,
        ".inf": math.inf, "-.inf": -math.inf,
    }
    STRINGS = [
        # 这些在 YAML 1.1 里是字符串，不是数字
        "1e3",        # float 必须带小数点
        "0o17",       # 0o 前缀是 YAML 1.2 写法，1.1 不认
        "089",        # 前导零但含 8/9，不是合法八进制
        "0:30",       # 六十进制首位不能是 0
        "223.5.5.5",  # IP 地址
        "30 Mbps",
        "test-node",
    ]

    def test_scalar_values(self):
        for text, want in self.CASES.items():
            with self.subTest(text=text):
                got = sbw._resolve_plain_scalar(text)
                if isinstance(want, float) and math.isinf(want):
                    self.assertTrue(math.isinf(got) and (got > 0) == (want > 0))
                else:
                    self.assertEqual(got, want)
                    self.assertIs(type(got), type(want))

    def test_nan(self):
        got = sbw._resolve_plain_scalar(".nan")
        self.assertTrue(math.isnan(got))

    def test_strings_not_numbers(self):
        for text in self.STRINGS:
            with self.subTest(text=text):
                self.assertEqual(sbw._resolve_plain_scalar(text), text)

    def test_quoted_scalars_stay_strings(self):
        # 引号内的 yes/123 不参与 plain resolver
        doc = sbw._parse_verge_yaml("a: 'yes'\nb: \"123\"\nc: '12:34'\n")
        self.assertEqual(doc, {"a": "yes", "b": "123", "c": "12:34"})

    def test_dquote_escapes(self):
        doc = sbw._parse_verge_yaml(
            'a: "x\\ty\\n"\n'      # \t \n
            'b: "\\x41 B"\n'       # \xNN
            'c: "\\u4e2d\\u6587"\n'  # \uNNNN
            'd: "say \\"hi\\""\n'  # \" 转义
        )
        self.assertEqual(doc["a"], "x\ty\n")
        self.assertEqual(doc["b"], "A B")
        self.assertEqual(doc["c"], "中文")
        self.assertEqual(doc["d"], 'say "hi"')


class CommentTest(unittest.TestCase):
    def test_full_line_and_trailing_comments(self):
        doc = sbw._parse_verge_yaml(
            "# 整行注释\n"
            "a: 1  # 行尾注释\n"
            "b: 2\n"
        )
        self.assertEqual(doc, {"a": 1, "b": 2})

    def test_hash_without_leading_space_kept(self):
        # # 前没有空白：plain 标量 / URL 里的 # 不是注释
        doc = sbw._parse_verge_yaml(
            "name: node#01\n"
            "web: http://example.com/#/dashboard\n"
        )
        self.assertEqual(doc["name"], "node#01")
        self.assertEqual(doc["web"], "http://example.com/#/dashboard")

    def test_hash_inside_quotes_kept(self):
        doc = sbw._parse_verge_yaml(
            "a: \"x#y\"  # 真注释\n"
            "b: 'p#q'\n"
        )
        self.assertEqual(doc, {"a": "x#y", "b": "p#q"})

    def test_single_quote_in_plain_scalar(self):
        # plain 标量中间的撇号不会被当成引号起始
        doc = sbw._parse_verge_yaml("name: it's-here\n")
        self.assertEqual(doc["name"], "it's-here")


class ErrorPathTest(unittest.TestCase):
    """超出语法子集一律抛 VergeYAMLError（报错而非静默解析错）。"""

    def assert_verge_error(self, text, *msg_parts):
        with self.assertRaises(sbw.VergeYAMLError) as cm:
            sbw._parse_verge_yaml(text)
        for part in msg_parts:
            self.assertIn(part, str(cm.exception))

    def test_anchor_rejected(self):
        self.assert_verge_error("a: &x 1\nb: 2\n", "anchor")

    def test_alias_rejected(self):
        self.assert_verge_error("a: *x\n", "anchor")

    def test_multiline_block_scalars_rejected(self):
        self.assert_verge_error("notes: |\n  line1\n", "多行字符串")
        self.assert_verge_error("notes: >\n  line1\n", "多行字符串")

    def test_merge_key_rejected(self):
        self.assert_verge_error("foo:\n  <<: *base\n", "merge key")

    def test_tag_rejected(self):
        self.assert_verge_error("a: !foo bar\n", "tag")

    def test_tab_indentation_rejected(self):
        self.assert_verge_error("a: 1\n\tb: 2\n", "Tab")

    def test_unknown_dquote_escape_rejected(self):
        self.assert_verge_error('p: "a\\qb"\n', "不支持的转义")

    def test_unclosed_quotes_rejected(self):
        self.assert_verge_error("p: 'abc\n", "未闭合")
        self.assert_verge_error('p: "abc\n', "未闭合")

    def test_multi_document_rejected(self):
        self.assert_verge_error("a: 1\n---\nb: 2\n", "多文档")

    def test_flow_spanning_lines_rejected(self):
        # flow 集合不允许跨行；未闭合即报错
        with self.assertRaises(sbw.VergeYAMLError):
            sbw._parse_verge_yaml("alpn: [h2,\n  http/1.1]\n")

    def test_trailing_content_after_value_rejected(self):
        self.assert_verge_error("a: [1] junk\n", "多余内容")
        self.assert_verge_error("a: 'x' junk\n", "多余内容")

    def test_empty_config_rejected(self):
        self.assert_verge_error("# 只有注释\n", "为空")

    def test_top_level_sequence_rejected(self):
        self.assert_verge_error("- a\n- b\n", "顶层必须是 mapping")

    def test_non_mapping_line_rejected(self):
        self.assert_verge_error("just a string\n", "key: value")

    def test_flow_collections(self):
        # 合法 flow：空集合、嵌套、带引号元素
        doc = sbw._parse_verge_yaml(
            "a: []\n"
            "b: {}\n"
            "c: {x: [1, 2], y: {}}\n"
            "d: ['it''s', \"say \\\"hi\\\"\"]\n"
        )
        self.assertEqual(doc["a"], [])
        self.assertEqual(doc["b"], {})
        self.assertEqual(doc["c"], {"x": [1, 2], "y": {}})
        self.assertEqual(doc["d"], ["it's", 'say "hi"'])


class FloatExponentSignTest(unittest.TestCase):
    """回归：float 的指数必须带显式符号（YAML 1.1，与 Psych/PyYAML 对齐）。
    修复前 "1.5e2" 被误解析为 150.0（PyYAML/ruby 都把它当字符串）。"""

    FLOAT_CASES = {"1.5e+2": 150.0, "1.5e-2": 0.015, "1.5E+2": 150.0,
                   "-1.5e-2": -0.015, "1.5": 1.5}
    STRING_CASES = ("1.5e2", "1e5", "1E5")

    def test_signed_exponent_is_float(self):
        for text, want in self.FLOAT_CASES.items():
            with self.subTest(text=text):
                got = sbw._resolve_plain_scalar(text)
                self.assertEqual(got, want)
                self.assertIsInstance(got, float)

    def test_unsigned_exponent_is_string(self):
        for text in self.STRING_CASES:
            with self.subTest(text=text):
                self.assertEqual(sbw._resolve_plain_scalar(text), text)

    def test_in_mapping_values(self):
        doc = sbw._parse_verge_yaml("a: 1.5e2\nb: 1.5e+2\nc: 1e5\nd: 1.5\n")
        self.assertEqual(doc, {"a": "1.5e2", "b": 150.0, "c": "1e5", "d": 1.5})

    @unittest.skipUnless(_pyyaml is not None, "本机未安装 PyYAML，跳过对拍")
    def test_matches_pyyaml(self):
        for text in list(self.FLOAT_CASES) + list(self.STRING_CASES):
            with self.subTest(text=text):
                self.assertEqual(sbw._resolve_plain_scalar(text),
                                 _pyyaml.safe_load(text))


class BareDashItemTest(unittest.TestCase):
    """回归：裸 "-" 序列项（行内无内容）的值应为 null，同缩进的下一个 "-" 行
    是它的兄弟项而非子序列。修复前 "a:\\n-\\n- b" 被静默解析成 [["b"]]。"""

    CASES = {
        # 同缩进兄弟项：裸项为 null，"- b" 属于外层序列
        "a:\n-\n- b\n": {"a": [None, "b"]},
        # 更深缩进的块仍是裸项的子内容（此行为不变）
        "a:\n-\n  - b\n": {"a": [["b"]]},
        # 末尾裸项
        "a:\n- b\n-\n": {"a": ["b", None]},
        # 单个裸项
        "a:\n-\n": {"a": [None]},
        # 裸项后接 mapping 兄弟项
        "a:\n-\n- k: v\n": {"a": [None, {"k": "v"}]},
    }

    def test_bare_dash_semantics(self):
        for text, want in self.CASES.items():
            with self.subTest(text=text):
                self.assertEqual(sbw._parse_verge_yaml(text), want)

    @unittest.skipUnless(_pyyaml is not None, "本机未安装 PyYAML，跳过对拍")
    def test_matches_pyyaml(self):
        for text in self.CASES:
            with self.subTest(text=text):
                self.assertEqual(sbw._parse_verge_yaml(text),
                                 _pyyaml.safe_load(text))


class MergeKeyInFlowTest(unittest.TestCase):
    """回归：flow dict 内未加引号的 << 键与 block 级一样抛 VergeYAMLError；
    带引号的 '<<' 是普通键。"""

    def test_flow_bare_merge_key_rejected(self):
        with self.assertRaises(sbw.VergeYAMLError) as cm:
            sbw._parse_verge_yaml("foo: {<<: 1}\n")
        self.assertIn("merge key", str(cm.exception))

    def test_flow_quoted_merge_key_is_plain_key(self):
        doc = sbw._parse_verge_yaml("foo: {'<<': 1}\n")
        self.assertEqual(doc, {"foo": {"<<": 1}})

    def test_block_quoted_merge_key_still_rejected(self):
        # 锁定一个有意保留的已知差异：block 级带引号键 '<<'/"<<" 目前仍按
        # merge key 报错（PyYAML 把它当普通键）。方向是「报错而非静默错」，
        # 且 Verge 机器生成配置不会写出带引号的 << 键；若未来对齐 PyYAML，
        # 本测试需一并更新。
        for text in ("foo:\n  '<<': 1\n", 'foo:\n  "<<": 1\n'):
            with self.subTest(text=text):
                with self.assertRaises(sbw.VergeYAMLError) as cm:
                    sbw._parse_verge_yaml(text)
                self.assertIn("merge key", str(cm.exception))


class ExtractProxiesFallbackTest(unittest.TestCase):
    """extract_proxies 三级 fallback 链（PyYAML -> ruby(非 win32) -> 迷你解析器）。"""

    def write_cfg(self, text):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        p = Path(td.name) / "clash-verge.yaml"
        p.write_text(text, encoding="utf-8")
        return str(p)

    @staticmethod
    def no_pyyaml():
        """sys.modules['yaml']=None 让函数内的 import yaml 抛 ImportError，
        模拟未安装 PyYAML 的环境（不依赖本机是否真装了 PyYAML）。"""
        return mock.patch.dict(sys.modules, {"yaml": None})

    def test_mini_parser_fallback_after_ruby_fails(self):
        path = self.write_cfg(VERGE_CFG)
        with self.no_pyyaml(), \
                mock.patch.object(sbw, "_extract_proxies_ruby",
                                  side_effect=sbw.WorkerUnavailable("no ruby")):
            proxies = sbw.extract_proxies(path)
        self.assertEqual(len(proxies), 5)
        self.assertEqual(proxies[0]["name"], "test-ss-01 #example")
        self.assertEqual(proxies[3]["server"], "[::1]")

    def test_all_paths_fail_raises_worker_unavailable(self):
        path = self.write_cfg("notes: |\n  超出子集\n")
        with self.no_pyyaml(), \
                mock.patch.object(sbw, "_extract_proxies_ruby",
                                  side_effect=sbw.WorkerUnavailable("no ruby")):
            with self.assertRaises(sbw.WorkerUnavailable) as cm:
                sbw.extract_proxies(path)
        msg = str(cm.exception)
        # ruby 分支在 win32 上按设计整体跳过，聚合信息里没有 "no ruby"
        if sys.platform == "win32":
            self.assertNotIn("no ruby", msg)
        else:
            self.assertIn("no ruby", msg)          # ruby 失败原因被聚合
        self.assertIn("内置 YAML 解析失败", msg)     # 迷你解析器失败原因被聚合

    def test_win32_skips_ruby_branch(self):
        path = self.write_cfg(VERGE_CFG)
        ruby = mock.MagicMock(name="ruby_path", side_effect=sbw.WorkerUnavailable("x"))
        with self.no_pyyaml(), \
                mock.patch.object(sys, "platform", "win32"), \
                mock.patch.object(sbw, "_extract_proxies_ruby", ruby):
            proxies = sbw.extract_proxies(path)
        ruby.assert_not_called()                   # win32 没有自带 ruby，直接跳过
        self.assertEqual(len(proxies), 5)

    def test_posix_uses_ruby_before_mini_parser(self):
        path = self.write_cfg(VERGE_CFG)
        ruby_nodes = [{"name": "from-ruby", "type": "ss", "server": "r.example.com"}]
        ruby = mock.MagicMock(name="ruby_path", return_value=ruby_nodes)
        with self.no_pyyaml(), \
                mock.patch.object(sys, "platform", "darwin"), \
                mock.patch.object(sbw, "_extract_proxies_ruby", ruby), \
                mock.patch.object(sbw, "_parse_verge_yaml",
                                  side_effect=AssertionError("不应走迷你解析器")):
            proxies = sbw.extract_proxies(path)
        ruby.assert_called_once_with(path)
        self.assertEqual(proxies, ruby_nodes)

    def test_missing_proxies_key_raises(self):
        path = self.write_cfg("mixed-port: 7897\n")
        with self.no_pyyaml(), \
                mock.patch.object(sys, "platform", "win32"):
            with self.assertRaises(sbw.WorkerUnavailable) as cm:
                sbw.extract_proxies(path)
        self.assertIn("proxies", str(cm.exception))

    def test_empty_proxies_raises(self):
        path = self.write_cfg("proxies: []\n")
        with self.no_pyyaml(), \
                mock.patch.object(sys, "platform", "win32"):
            with self.assertRaises(sbw.WorkerUnavailable) as cm:
                sbw.extract_proxies(path)
        self.assertIn("proxies 为空", str(cm.exception))

    def test_config_file_missing_raises(self):
        with self.no_pyyaml(), \
                mock.patch.object(sys, "platform", "win32"):
            with self.assertRaises(sbw.WorkerUnavailable):
                sbw.extract_proxies("/nonexistent-x/clash-verge.yaml")

    @unittest.skipUnless(_pyyaml is not None, "本机未安装 PyYAML，跳过优先级测试")
    def test_pyyaml_preferred_when_available(self):
        path = self.write_cfg(VERGE_CFG)
        with mock.patch.object(sbw, "_extract_proxies_ruby",
                               side_effect=AssertionError("不应走 ruby")), \
                mock.patch.object(sbw, "_parse_verge_yaml",
                                  side_effect=AssertionError("不应走迷你解析器")):
            proxies = sbw.extract_proxies(path)
        self.assertEqual(len(proxies), 5)
        self.assertEqual(proxies[2]["password"], "trojan's quoted")

    @unittest.skipUnless(_pyyaml is not None, "本机未安装 PyYAML，跳过对拍")
    def test_mini_parser_matches_pyyaml_on_verge_shape(self):
        """迷你解析器与 PyYAML 在同一 fixture 上结果完全一致（等价性对拍）。"""
        for text in (VERGE_CFG, VERGE_CFG_INDENTED):
            with self.subTest(text=text[:30]):
                self.assertEqual(sbw._parse_verge_yaml(text),
                                 _pyyaml.safe_load(text))


if __name__ == "__main__":
    unittest.main()
