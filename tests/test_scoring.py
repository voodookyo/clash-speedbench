# -*- coding: utf-8 -*-
"""Network/Overall score and make_tags regression tests."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import clash_speedbench as csb


def mk_result(median=None, latency=None, ip=None, speeds=None):
    return csb.Result(
        name="节点X", provider="p", proto="ss",
        latency_ms=latency,
        speeds_mbps=list(speeds or ([] if median is None else [median])),
        median_mbps=median,
        best_mbps=median,
        status="ok" if median is not None else "unreachable",
        ip=ip,
    )


def mk_ip(kind="", ok=True, **flags):
    return csb.IpInfo(kind=kind, ok=ok, **flags)


class ComputeScoreTest(unittest.TestCase):
    def test_zero_bandwidth_zero_score(self):
        self.assertEqual(csb.compute_score(mk_result(median=None, latency=100)), 0.0)
        self.assertEqual(csb.compute_score(mk_result(median=0.0, latency=100)), 0.0)
        self.assertEqual(csb.compute_score(mk_result(median=-1.0, latency=100)), 0.0)

    def test_full_marks(self):
        # 带宽 100（封顶）、延迟 80ms（满分）、无 IP 画像（按 100）
        r = mk_result(median=100.0, latency=80, ip=None)
        self.assertEqual(csb.compute_score(r), 100.0)

    def test_network_weights_and_unknown_ip_do_not_add_points(self):
        # 带宽 20Mbps→20 分；延迟 224ms→80 分；只有两个有效维度时归一化。
        r = mk_result(median=20.0, latency=224, ip=mk_ip(proxy=True))
        self.assertEqual(csb.compute_network_score(r), 41.8)
        self.assertEqual(csb.compute_score(r), 41.8)

    def test_bandwidth_capped_at_100(self):
        r = mk_result(median=250.0, latency=80, ip=None)
        self.assertEqual(csb.compute_score(r), 100.0)

    def test_latency_none_scores_zero_on_latency_part(self):
        # Missing latency is omitted and the valid bandwidth dimension is
        # normalized to 100; missing IP intelligence is not a 100-point bonus.
        r = mk_result(median=100.0, latency=None, ip=mk_ip(kind="ISP/非托管"))
        self.assertEqual(csb.compute_score(r), 100.0)

    def test_all_network_dimensions_full_marks(self):
        r = mk_result(median=100.0, latency=80)
        r.multi_mbps = 400.0
        r.jitter_ms = 5.0
        r.connect_ms = 100.0
        r.probe_attempts = 10
        r.probe_successes = 10
        r.probe_failures = 0
        r.probe_success_rate = 100.0
        r.probe_loss_pct = 0.0
        self.assertEqual(csb.compute_network_score(r), 100.0)

    def test_available_ip_quality_uses_80_20_overall(self):
        r = mk_result(median=100.0, latency=80)
        r.ip_quality_score = 40.0
        self.assertEqual(csb.compute_score(r), 88.0)

    def test_latency_extremes(self):
        self.assertEqual(csb.latency_score(80), 100.0)
        self.assertEqual(csb.latency_score(800), 0.0)
        self.assertEqual(csb.latency_score(None), 0.0)


class MakeTagsTest(unittest.TestCase):
    FORBIDDEN = ("住宅", "高风险", "低风险", "中风险")

    def assert_clean(self, tags):
        for bad in self.FORBIDDEN:
            self.assertNotIn(bad, tags)

    def test_unreachable(self):
        tags = csb.make_tags(mk_result(median=None))
        self.assertEqual(tags, "不通")
        self.assert_clean(tags)

    def test_unreachable_with_latency_has_no_latency_tag(self):
        # median None 时不进入延迟标签分支
        tags = csb.make_tags(mk_result(median=None, latency=50))
        self.assertEqual(tags, "不通")

    def test_slow(self):
        tags = csb.make_tags(mk_result(median=3.0, latency=200))
        self.assertIn("龟速", tags.split(","))
        self.assert_clean(tags)

    def test_slow_boundary(self):
        self.assertIn("龟速", csb.make_tags(mk_result(median=4.99)).split(","))
        self.assertNotIn("龟速", csb.make_tags(mk_result(median=5.0)).split(","))

    def test_high_bandwidth(self):
        tags = csb.make_tags(mk_result(median=50.0, latency=200))
        self.assertIn("高带宽", tags.split(","))

    def test_high_bandwidth_boundary(self):
        self.assertNotIn("高带宽", csb.make_tags(mk_result(median=49.99)).split(","))

    def test_low_latency(self):
        tags = csb.make_tags(mk_result(median=10.0, latency=100))
        self.assertIn("低延迟", tags.split(","))
        self.assertNotIn("低延迟", csb.make_tags(mk_result(median=10.0, latency=101)))

    def test_high_latency(self):
        self.assertIn("高延迟", csb.make_tags(mk_result(median=10.0, latency=301)).split(","))
        self.assertNotIn("高延迟", csb.make_tags(mk_result(median=10.0, latency=300)).split(","))

    def test_no_latency_tags_when_latency_none(self):
        self.assertEqual(csb.make_tags(mk_result(median=10.0, latency=None)), "")

    def test_combined_slow_low_latency(self):
        tags = csb.make_tags(mk_result(median=3.0, latency=50))
        self.assertEqual(tags, "龟速,低延迟")
        self.assert_clean(tags)

    def test_ip_kind_tags(self):
        base = dict(median=10.0, latency=200)
        self.assertIn("ISP/非托管",
                      csb.make_tags(mk_result(**base, ip=mk_ip("ISP/非托管"))).split(","))
        self.assertIn("机房托管",
                      csb.make_tags(mk_result(**base, ip=mk_ip("机房托管"))).split(","))
        self.assertIn("脏IP",
                      csb.make_tags(mk_result(**base, ip=mk_ip("代理/VPN"))).split(","))

    def test_mobile_and_unknown_kinds_have_no_tag(self):
        base = dict(median=10.0, latency=200)
        self.assertNotIn("移动", csb.make_tags(mk_result(**base, ip=mk_ip("移动网络"))))
        self.assertEqual(csb.make_tags(mk_result(**base, ip=mk_ip("未知"))), "")

    def test_ip_not_ok_or_none_no_ip_tags(self):
        base = dict(median=10.0, latency=200)
        self.assertEqual(csb.make_tags(mk_result(**base, ip=mk_ip("机房托管", ok=False))), "")
        self.assertEqual(csb.make_tags(mk_result(**base, ip=None)), "")

    def test_no_risk_wording_in_any_combo(self):
        combos = [
            mk_result(median=None),
            mk_result(median=1.0, latency=50, ip=mk_ip("代理/VPN", proxy=True)),
            mk_result(median=100.0, latency=500, ip=mk_ip("机房托管", hosting=True)),
            mk_result(median=10.0, latency=200, ip=mk_ip("ISP/非托管")),
        ]
        for r in combos:
            self.assert_clean(csb.make_tags(r))


if __name__ == "__main__":
    unittest.main()
