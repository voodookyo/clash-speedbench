# -*- coding: utf-8 -*-
"""with_dependencies：dialer-proxy 依赖闭包展开的全场景测试。
覆盖：链式 A→B→C、共享依赖（菱形去重）、循环依赖、自引用、悬空引用、
空选、入选顺序保持（依赖追加尾部）、入参不被修改、非 dict/无名条目安全；
同时保护 v1.0 新增运行时模块在零 pip 依赖环境下可直接导入。"""
import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from speedbench_workers import with_dependencies


class RuntimeDependencyClosureTest(unittest.TestCase):
    """新增 runtime 模块不得把可选 provider 变成硬依赖。"""

    def test_ip_intel_and_leak_modules_import_without_optional_packages(self):
        import speedbench_ip_intel
        import speedbench_leak

        self.assertTrue(callable(speedbench_ip_intel.IpApiProvider))
        self.assertTrue(callable(speedbench_ip_intel.IpInfoProvider))
        self.assertTrue(callable(speedbench_ip_intel.IpqsProvider))
        self.assertTrue(callable(speedbench_ip_intel.ScamalyticsProvider))
        self.assertTrue(callable(speedbench_leak.evaluate_webrtc))
        self.assertTrue(callable(speedbench_leak.DnsLeakProvider))


def node(name, dep=None, **extra):
    d = {"name": name, "type": "ss", "server": f"{name}.example.com", "port": 443}
    if dep is not None:
        d["dialer-proxy"] = dep
    d.update(extra)
    return d


def names(proxies):
    return [p["name"] for p in proxies]


class WithDependenciesTest(unittest.TestCase):
    def test_no_dependency_returns_selected_in_order(self):
        a, b, c = node("A"), node("B"), node("C")
        merged = with_dependencies([a, b], [a, b, c])
        self.assertEqual(names(merged), ["A", "B"])

    def test_chain_appended_at_tail(self):
        # A→B→C：依赖按发现顺序追加尾部；all_proxies 的顺序不影响结果
        a = node("A", dep="B")
        b = node("B", dep="C")
        c = node("C")
        merged = with_dependencies([a], [c, a, b])
        self.assertEqual(names(merged), ["A", "B", "C"])

    def test_shared_dependency_deduped(self):
        # 菱形：A→B→D 且 C→D，共享的 D 只出现一次
        a = node("A", dep="B")
        b = node("B", dep="D")
        c = node("C", dep="D")
        d = node("D")
        merged = with_dependencies([a, c], [a, b, c, d])
        self.assertEqual(names(merged), ["A", "C", "B", "D"])

    def test_cycle_ab_ba_terminates(self):
        a = node("A", dep="B")
        b = node("B", dep="A")
        merged = with_dependencies([a], [a, b])
        self.assertEqual(names(merged), ["A", "B"])

    def test_cycle_between_deps_terminates(self):
        # 循环发生在依赖层（B→C→B），也要在 visited 处停下
        a = node("A", dep="B")
        b = node("B", dep="C")
        c = node("C", dep="B")
        merged = with_dependencies([a], [a, b, c])
        self.assertEqual(names(merged), ["A", "B", "C"])

    def test_self_reference(self):
        a = node("A", dep="A")
        self.assertEqual(names(with_dependencies([a], [a])), ["A"])

    def test_dangling_reference_ignored(self):
        # 依赖的名字在 all_proxies 里不存在：跳过，不报错
        a = node("A", dep="不存在的节点")
        merged = with_dependencies([a], [a, node("B")])
        self.assertEqual(names(merged), ["A"])

    def test_empty_selection(self):
        self.assertEqual(with_dependencies([], [node("A")]), [])

    def test_selected_order_preserved_deps_at_tail(self):
        # 入选节点保持原顺序在前，依赖一律追加尾部（不插队）
        a = node("A", dep="C")
        b = node("B")
        c = node("C")
        merged = with_dependencies([b, a], [a, b, c])
        self.assertEqual(names(merged), ["B", "A", "C"])

    def test_duplicate_selected_deduped(self):
        a1 = node("A")
        a2 = node("A")  # 同名的另一个 dict
        merged = with_dependencies([a1, a2], [a1])
        self.assertEqual(names(merged), ["A"])

    def test_dependency_already_selected_not_duplicated(self):
        a = node("A", dep="B")
        b = node("B")
        merged = with_dependencies([a, b], [a, b])
        self.assertEqual(names(merged), ["A", "B"])

    def test_inputs_not_mutated(self):
        a = node("A", dep="B")
        b = node("B")
        c = node("C")
        selected = [a]
        all_proxies = [b, a, c]
        sel_before = copy.deepcopy(selected)
        all_before = copy.deepcopy(all_proxies)
        merged = with_dependencies(selected, all_proxies)
        self.assertEqual(selected, sel_before)          # 内容不变
        self.assertEqual(all_proxies, all_before)
        self.assertEqual(len(selected), 1)              # 原列表没有被 append
        self.assertIsNot(merged, selected)              # 返回新列表
        self.assertIsNot(merged, all_proxies)

    def test_non_dict_and_nameless_entries_in_all_proxies_safe(self):
        a = node("A", dep="B")
        b = node("B")
        all_proxies = [a, b, None, "junk", {"type": "ss"}, 42]
        merged = with_dependencies([a], all_proxies)
        self.assertEqual(names(merged), ["A", "B"])

    def test_non_dict_selected_entries_skipped(self):
        a = node("A")
        merged = with_dependencies([a, None, "junk"], [a])
        self.assertEqual(names(merged), ["A"])

    def test_dep_value_none_or_empty_ignored(self):
        a = node("A")
        a["dialer-proxy"] = None
        b = node("B")
        b["dialer-proxy"] = ""
        merged = with_dependencies([a, b], [a, b])
        self.assertEqual(names(merged), ["A", "B"])


if __name__ == "__main__":
    unittest.main()
