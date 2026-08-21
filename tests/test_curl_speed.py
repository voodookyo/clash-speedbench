# -*- coding: utf-8 -*-
"""curl_speed 的严格校验逻辑测试（subprocess.run 全部 mock，不调用真 curl）。"""
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import clash_speedbench as csb


def writeout(speed=50e6, total=1.6, size=8e6, code=200,
             connect=0.05, appconnect=0.2, start=0.9):
    """按 curl --write-out 的 7 字段顺序伪造一行输出。"""
    return "\t".join(str(x) for x in
                     (speed, total, size, code, connect, appconnect, start))


def fake_proc(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(args=["curl"], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


class CurlSpeedTest(unittest.TestCase):
    def run_curl(self, proc=None, side_effect=None):
        with mock.patch.object(csb.subprocess, "run",
                               return_value=proc, side_effect=side_effect) as m:
            result = csb.curl_speed("http://127.0.0.1:7897",
                                    "https://speed.cloudflare.com/__down?bytes=8000000",
                                    max_time=4.0, connect_timeout=3.0)
        return result, m

    def test_ok_returncode_0(self):
        (mbps, status, connect_ms, size_mb), _ = self.run_curl(
            proc=fake_proc(stdout=writeout(), returncode=0))
        self.assertEqual(status, "ok")
        self.assertAlmostEqual(mbps, 400.0)       # 50e6 B/s * 8 / 1e6
        self.assertAlmostEqual(size_mb, 8.0)
        self.assertEqual(connect_ms, 200.0)       # time_appconnect 0.2s

    def test_ok_returncode_28_max_time_truncated(self):
        # 28 = max-time 截断，仍视为有效样本
        (mbps, status, connect_ms, size_mb), _ = self.run_curl(
            proc=fake_proc(stdout=writeout(), returncode=28))
        self.assertEqual(status, "ok")
        self.assertAlmostEqual(mbps, 400.0)

    def test_other_returncode_fails(self):
        (mbps, status, connect_ms, size_mb), _ = self.run_curl(
            proc=fake_proc(stdout=writeout(code=0, speed=0, size=0),
                           stderr="curl: (7) Couldn't connect", returncode=7))
        self.assertIsNone(mbps)
        self.assertTrue(status.startswith("curl-7"), status)
        self.assertIn("Couldn't connect", status)
        # 失败时仍返回已知的 connect_ms 和 size_mb
        self.assertEqual(connect_ms, 200.0)
        self.assertEqual(size_mb, 0.0)

    def test_http_code_not_200_fails(self):
        (mbps, status, _, _), _ = self.run_curl(
            proc=fake_proc(stdout=writeout(code=403), returncode=0))
        self.assertIsNone(mbps)
        self.assertEqual(status, "http-403")

    def test_small_download_no_data(self):
        # 下载量 < 256KB 判失败
        (mbps, status, _, _), _ = self.run_curl(
            proc=fake_proc(stdout=writeout(speed=1e6, size=100_000), returncode=0))
        self.assertIsNone(mbps)
        self.assertEqual(status, "no-data")

    def test_download_size_just_below_threshold(self):
        (mbps, status, _, _), _ = self.run_curl(
            proc=fake_proc(stdout=writeout(size=256 * 1024 - 1), returncode=0))
        self.assertIsNone(mbps)
        self.assertEqual(status, "no-data")

    def test_download_size_at_threshold_ok(self):
        (mbps, status, _, _), _ = self.run_curl(
            proc=fake_proc(stdout=writeout(size=256 * 1024), returncode=0))
        self.assertEqual(status, "ok")
        self.assertIsNotNone(mbps)

    def test_zero_speed_no_data(self):
        (mbps, status, _, _), _ = self.run_curl(
            proc=fake_proc(stdout=writeout(speed=0, size=8e6), returncode=0))
        self.assertIsNone(mbps)
        self.assertEqual(status, "no-data")

    def test_no_data_appends_stderr_reason(self):
        (mbps, status, _, _), _ = self.run_curl(
            proc=fake_proc(stdout=writeout(size=100), stderr="weird\ntrace",
                           returncode=0))
        self.assertIsNone(mbps)
        self.assertEqual(status, "no-data: weird trace")

    def test_connect_ms_tls_uses_appconnect(self):
        (_, _, connect_ms, _), _ = self.run_curl(
            proc=fake_proc(stdout=writeout(connect=0.05, appconnect=0.2)))
        self.assertEqual(connect_ms, 200.0)

    def test_connect_ms_non_tls_falls_back_to_connect(self):
        (_, _, connect_ms, _), _ = self.run_curl(
            proc=fake_proc(stdout=writeout(connect=0.042, appconnect=0.0)))
        self.assertEqual(connect_ms, 42.0)

    def test_connect_ms_none_when_both_zero(self):
        (_, status, connect_ms, _), _ = self.run_curl(
            proc=fake_proc(stdout=writeout(connect=0.0, appconnect=0.0)))
        self.assertEqual(status, "ok")
        self.assertIsNone(connect_ms)

    def test_malformed_output_wrong_field_count(self):
        (mbps, status, connect_ms, size_mb), _ = self.run_curl(
            proc=fake_proc(stdout="0\t0\t0", stderr="", returncode=0))
        self.assertIsNone(mbps)
        self.assertTrue(status.startswith("curl-0"), status)
        self.assertIsNone(connect_ms)
        self.assertEqual(size_mb, 0.0)

    def test_parse_error_non_numeric(self):
        bad = writeout().split("\t")
        bad[0] = "not-a-number"
        (mbps, status, _, _), _ = self.run_curl(
            proc=fake_proc(stdout="\t".join(bad), returncode=0))
        self.assertIsNone(mbps)
        self.assertEqual(status, "parse-error")

    def test_subprocess_timeout(self):
        (mbps, status, connect_ms, size_mb), _ = self.run_curl(
            side_effect=subprocess.TimeoutExpired(cmd="curl", timeout=12))
        self.assertEqual((mbps, status, connect_ms, size_mb),
                         (None, "curl-timeout", None, 0.0))

    def test_curl_missing_raises(self):
        with self.assertRaises(RuntimeError):
            self.run_curl(side_effect=FileNotFoundError("curl"))

    def test_command_line_flags(self):
        _, m = self.run_curl(proc=fake_proc(stdout=writeout(), returncode=0))
        cmd = m.call_args[0][0]
        self.assertEqual(cmd[0], "curl")
        self.assertIn("--proxy", cmd)
        self.assertIn("4.0", cmd)   # --max-time
        self.assertIn("3.0", cmd)   # --connect-timeout
        self.assertIn("--write-out", cmd)


if __name__ == "__main__":
    unittest.main()
