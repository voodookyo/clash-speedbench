# -*- coding: utf-8 -*-
"""classify_ip / fetch_ip_info / ip_flag_score / ip_brief 测试。
IP 画像新口径：代理/VPN、机房托管、移动网络、ISP/非托管、未知 —— 不存在「住宅」。"""
import subprocess
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import clash_speedbench as csb

KINDS = {"代理/VPN", "机房托管", "移动网络", "ISP/非托管", "未知"}


def api_data(**over):
    base = {
        "status": "success",
        "query": "203.0.113.9",
        "country": "美国",
        "countryCode": "US",
        "regionName": "California",
        "city": "Los Angeles",
        "isp": "Example ISP",
        "org": "Example Org",
        "as": "AS64496 Example Net",
        "asname": "EXAMPLE-NET",
        "mobile": False,
        "proxy": False,
        "hosting": False,
    }
    base.update(over)
    return base


class ClassifyIpTest(unittest.TestCase):
    def test_proxy_flag(self):
        ip = csb.classify_ip(api_data(proxy=True))
        self.assertEqual(ip.kind, "代理/VPN")
        self.assertTrue(ip.proxy)
        self.assertTrue(ip.ok)

    def test_hosting_flag(self):
        ip = csb.classify_ip(api_data(hosting=True))
        self.assertEqual(ip.kind, "机房托管")

    def test_proxy_wins_over_hosting(self):
        ip = csb.classify_ip(api_data(proxy=True, hosting=True))
        self.assertEqual(ip.kind, "代理/VPN")

    def test_mobile_flag(self):
        ip = csb.classify_ip(api_data(mobile=True))
        self.assertEqual(ip.kind, "移动网络")

    def test_hosting_wins_over_mobile(self):
        ip = csb.classify_ip(api_data(hosting=True, mobile=True))
        self.assertEqual(ip.kind, "机房托管")

    def test_no_flags_with_isp_or_org_is_neutral_isp(self):
        # 三个标记全 false 不断言住宅，记中性的 ISP/非托管
        self.assertEqual(csb.classify_ip(api_data()).kind, "ISP/非托管")
        self.assertEqual(csb.classify_ip(api_data(isp="")).kind, "ISP/非托管")  # 仅 org
        self.assertEqual(csb.classify_ip(api_data(org="")).kind, "ISP/非托管")  # 仅 isp

    def test_no_flags_no_isp_org_is_unknown(self):
        ip = csb.classify_ip(api_data(isp="", org=""))
        self.assertEqual(ip.kind, "未知")

    def test_kind_never_residential(self):
        for combo in ({}, {"proxy": True}, {"hosting": True}, {"mobile": True},
                      {"isp": "", "org": ""}):
            ip = csb.classify_ip(api_data(**combo))
            self.assertIn(ip.kind, KINDS)
            self.assertNotEqual(ip.kind, "住宅")
            self.assertNotIn("住宅", ip.kind)

    def test_field_mapping(self):
        ip = csb.classify_ip(api_data(mobile=True))
        self.assertEqual(ip.exit_ip, "203.0.113.9")
        self.assertEqual(ip.country, "美国")
        self.assertEqual(ip.country_code, "US")
        self.assertEqual(ip.region, "California")
        self.assertEqual(ip.city, "Los Angeles")
        self.assertEqual(ip.isp, "Example ISP")
        self.assertEqual(ip.org, "Example Org")
        self.assertEqual(ip.asn, "AS64496 Example Net")
        self.assertEqual(ip.asname, "EXAMPLE-NET")
        self.assertTrue(ip.mobile)
        self.assertFalse(ip.proxy)
        self.assertFalse(ip.hosting)

    def test_missing_fields_default_empty(self):
        ip = csb.classify_ip({"proxy": True})
        self.assertEqual(ip.kind, "代理/VPN")
        self.assertEqual(ip.exit_ip, "")
        self.assertEqual(ip.country_code, "")


class FetchIpInfoTest(unittest.TestCase):
    """查询失败路径：fetch_ip_info 一律返回 None（调用方随后 ip=None）。"""

    def run_fetch(self, proc=None, side_effect=None):
        with mock.patch.dict(os.environ, {"SPEEDBENCH_DISABLE_IP_API": ""}, clear=False), \
                mock.patch.object(csb.subprocess, "run",
                               return_value=proc, side_effect=side_effect):
            return csb.fetch_ip_info("http://127.0.0.1:7897", 8.0)

    @staticmethod
    def proc(stdout="", stderr="", returncode=0):
        return subprocess.CompletedProcess(args=["curl"], returncode=returncode,
                                           stdout=stdout, stderr=stderr)

    def test_success_returns_dict(self):
        import json
        data = self.run_fetch(self.proc(stdout=json.dumps(api_data())))
        self.assertIsInstance(data, dict)
        self.assertEqual(data["countryCode"], "US")

    def test_nonzero_returncode_none(self):
        self.assertIsNone(self.run_fetch(self.proc(stdout="x", returncode=7)))

    def test_empty_stdout_none(self):
        self.assertIsNone(self.run_fetch(self.proc(stdout="", returncode=0)))

    def test_invalid_json_none(self):
        self.assertIsNone(self.run_fetch(self.proc(stdout="<html>blocked</html>")))

    def test_non_object_json_none(self):
        self.assertIsNone(self.run_fetch(self.proc(stdout='["success"]')))

    def test_status_fail_none(self):
        self.assertIsNone(self.run_fetch(self.proc(stdout='{"status":"fail"}')))

    def test_timeout_none(self):
        self.assertIsNone(self.run_fetch(
            side_effect=subprocess.TimeoutExpired(cmd="curl", timeout=13)))


class IpFlagScoreTest(unittest.TestCase):
    def test_no_ip_or_not_ok_is_unknown_not_clean(self):
        self.assertIsNone(csb.ip_flag_score(None))
        self.assertIsNone(csb.ip_flag_score(csb.IpInfo()))  # ok=False

    def test_flag_scores(self):
        self.assertEqual(csb.ip_flag_score(csb.IpInfo(proxy=True, ok=True)), 30.0)
        self.assertEqual(csb.ip_flag_score(csb.IpInfo(hosting=True, ok=True)), 55.0)
        self.assertEqual(csb.ip_flag_score(csb.IpInfo(mobile=True, ok=True)), 80.0)
        self.assertEqual(csb.ip_flag_score(csb.IpInfo(ok=True)), 100.0)

    def test_ordering_proxy_lt_hosting_lt_mobile_lt_clean(self):
        proxy = csb.ip_flag_score(csb.IpInfo(proxy=True, ok=True))
        hosting = csb.ip_flag_score(csb.IpInfo(hosting=True, ok=True))
        mobile = csb.ip_flag_score(csb.IpInfo(mobile=True, ok=True))
        clean = csb.ip_flag_score(csb.IpInfo(ok=True))
        self.assertLess(proxy, hosting)
        self.assertLess(hosting, mobile)
        self.assertLess(mobile, clean)


class IpBriefTest(unittest.TestCase):
    def test_full_format(self):
        ip = csb.IpInfo(country_code="US", kind="机房托管",
                        asname="CLOUDFLARENET", isp="Cloudflare", ok=True)
        self.assertEqual(csb.ip_brief(ip), "US·机房托管·CLOUDFLARENET")

    def test_isp_fallback_truncated_to_16(self):
        ip = csb.IpInfo(country_code="JP", kind="ISP/非托管",
                        asname="", isp="A-Very-Long-ISP-Name-Here", ok=True)
        self.assertEqual(csb.ip_brief(ip), "JP·ISP/非托管·" + "A-Very-Long-ISP-Name-Here"[:16])

    def test_country_fallback_when_no_country_code(self):
        ip = csb.IpInfo(country_code="", country="美国", kind="移动网络",
                        asname="CMNET", ok=True)
        self.assertEqual(csb.ip_brief(ip), "美国·移动网络·CMNET")

    def test_question_mark_when_no_country_at_all(self):
        ip = csb.IpInfo(kind="未知", asname="X", ok=True)
        self.assertEqual(csb.ip_brief(ip), "?·未知·X")

    def test_no_net_part_omitted(self):
        ip = csb.IpInfo(country_code="US", kind="未知", asname="", isp="", ok=True)
        self.assertEqual(csb.ip_brief(ip), "US·未知")


if __name__ == "__main__":
    unittest.main()
