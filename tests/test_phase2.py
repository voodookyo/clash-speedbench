# -*- coding: utf-8 -*-
"""Phase 2 Top-N 选择（select_phase2_nodes）与「未精测」改标（relabel_unmeasured）测试。
这两个纯函数抽自 run_pool（行为不变的重构），此处直接测函数级逻辑。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import clash_speedbench as csb
import speedbench_workers as sbw


def mk(name, latency):
    return csb.Result(name=name, provider="", proto="ss",
                      latency_ms=latency, speeds_mbps=[],
                      median_mbps=None, best_mbps=None,
                      status="ok" if latency is not None else "unreachable")


class SelectPhase2NodesTest(unittest.TestCase):
    def setUp(self):
        self.results = [mk("慢", 300), mk("不通", None),
                        mk("快", 100), mk("中", 200)]

    def names(self, rs):
        return [r.name for r in rs]

    def test_excludes_unreachable_sorts_by_latency_takes_top_n(self):
        chosen = sbw.select_phase2_nodes(self.results, top_n=2)
        self.assertEqual(self.names(chosen), ["快", "中"])

    def test_top_n_larger_than_reachable(self):
        chosen = sbw.select_phase2_nodes(self.results, top_n=15)
        self.assertEqual(self.names(chosen), ["快", "中", "慢"])

    def test_measure_all_returns_every_reachable(self):
        chosen = sbw.select_phase2_nodes(self.results, top_n=1, measure_all=True)
        self.assertEqual(self.names(chosen), ["快", "中", "慢"])

    def test_top_n_clamped_to_at_least_1(self):
        chosen = sbw.select_phase2_nodes(self.results, top_n=0)
        self.assertEqual(self.names(chosen), ["快"])

    def test_empty_or_all_unreachable(self):
        self.assertEqual(sbw.select_phase2_nodes([], 15), [])
        self.assertEqual(sbw.select_phase2_nodes([mk("a", None), mk("b", None)], 15), [])

    def test_does_not_mutate_input(self):
        original = list(self.results)
        sbw.select_phase2_nodes(self.results, top_n=2)
        self.assertEqual(self.results, original)


class RelabelUnmeasuredTest(unittest.TestCase):
    def test_reachable_unmeasured_becomes_unmeasured(self):
        r = mk("节点", 120)
        r.tags = csb.make_tags(r)          # 连通但没测带宽 → 不通
        self.assertEqual(r.tags, "不通")
        sbw.relabel_unmeasured(r)
        self.assertEqual(r.tags, "未精测")

    def test_unreachable_stays_unreachable(self):
        r = mk("节点", None)
        r.tags = csb.make_tags(r)
        sbw.relabel_unmeasured(r)
        self.assertEqual(r.tags, "不通")

    def test_other_tags_preserved(self):
        r = mk("节点", 120)
        r.ip = csb.IpInfo(kind="ISP/非托管", ok=True)
        r.tags = csb.make_tags(r)          # 不通,ISP/非托管
        sbw.relabel_unmeasured(r)
        self.assertEqual(r.tags, "未精测,ISP/非托管")

    def test_measured_node_untouched(self):
        r = mk("节点", 120)
        r.median_mbps = 30.0
        r.speeds_mbps = [30.0]
        r.tags = csb.make_tags(r)
        sbw.relabel_unmeasured(r)
        self.assertNotIn("不通", r.tags)
        self.assertNotIn("未精测", r.tags)


if __name__ == "__main__":
    unittest.main()
