# -*- coding: utf-8 -*-
"""Phase 1 延迟改走主实例 /delay 的测试（全 mock，不起 mihomo、不连网）：

- probe_latency_pool：10 路线程池并发打主实例 /delay、返回 {name: ProbeStats} map（可解包为旧二元组）、
  探测期间把 api.timeout 放宽到 delay_timeout/1000+3s（更宽的旧值不收窄）、
  失败节点值为 (None, None)、进度行「Phase 1 粗筛 [N/M] ...（主实例）」与
  Web 端 app.js 进度正则 /\\[\\s*(\\d+)\\/(\\d+)\\]/ 匹配
- _probe_node_in_worker：latency 传入时直接用不重测（worker.api.proxy_delay
  不调用）；None 时 worker 内兜底重测；主实例/worker 双重失败才标 unreachable；
  IP 画像照常执行（--no-ip 时跳过）
- run_pool：给了 main_api 先 probe_latency_pool 再进 worker（失败节点拿 None
  进 worker 兜底）；main_api=None 退回旧行为（不碰 probe_latency_pool，全量
  节点进 worker 测延迟）；--no-ip 且全通时 Phase 1 不起 worker，直接由
  latency_map 构建结果（全程仅 Phase 2 那一个 worker）
"""
import contextlib
import io
import re
import sys
import threading
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import clash_speedbench as csb
import speedbench_workers as sbw

# 与 web/app.js pollStatus 里的进度正则同式（通用 [N/M] 计数）
WEB_PROGRESS_RE = re.compile(r"\[\s*(\d+)/(\d+)\]")

HOSTS = {"speed.cloudflare.com": "1.1.1.1",
         "cp.cloudflare.com": "1.1.1.1",
         "ip-api.com": "208.95.112.1"}

IP_API_DATA = {"status": "success", "query": "203.0.113.9", "country": "Japan",
               "countryCode": "JP", "regionName": "Tokyo", "city": "Tokyo",
               "isp": "Example ISP", "org": "", "as": "AS1 Example",
               "asname": "EXAMPLE-NET", "proxy": True, "hosting": False,
               "mobile": False}


def mk_args(**over):
    args = SimpleNamespace(config_file="/fake/clash-verge.yaml",
                           delay_timeout=2000, no_ip=False, settle=0,
                           ip_timeout=5, workers=3, mb=10, max_time=8,
                           rounds=2, top_n=15, all=False, multi=False)
    for k, v in over.items():
        setattr(args, k, v)
    return args


class ProbeLatencyPoolTest(unittest.TestCase):
    def setUp(self):
        self.api = csb.MihomoAPI("http://127.0.0.1:9", timeout=5.0)

    def test_concurrent_calls_all_nodes_and_returns_map(self):
        names = [f"节点{i}" for i in range(6)]
        calls = []
        in_flight = {"cur": 0, "max": 0}
        lock = threading.Lock()

        def fake_probe(api, name, timeout_ms):
            with lock:
                in_flight["cur"] += 1
                in_flight["max"] = max(in_flight["max"], in_flight["cur"])
            time.sleep(0.05)
            with lock:
                in_flight["cur"] -= 1
                calls.append((api, name, timeout_ms))
            return (100 + names.index(name), 2.0)

        with mock.patch.object(sbw, "probe_latency", side_effect=fake_probe), \
                contextlib.redirect_stdout(io.StringIO()):
            out = sbw.probe_latency_pool(self.api, names, 2000)

        self.assertEqual(set(out), set(names))               # 全部节点都在 map 里
        self.assertEqual(out["节点0"], (100, 2.0))
        self.assertEqual(out["节点5"], (105, 2.0))
        self.assertEqual(len(calls), len(names))             # 每个节点恰好探测一次
        for api, _name, timeout_ms in calls:
            self.assertIs(api, self.api)                     # 都打主实例
            self.assertEqual(timeout_ms, 2000)
        self.assertGreater(in_flight["max"], 1)              # 确实并发而非串行

    def test_timeout_widened_during_probe(self):
        # 主实例默认 5s，delay_timeout 8000ms → 放宽到 8+3=11s
        with mock.patch.object(sbw, "probe_latency", return_value=(None, None)), \
                contextlib.redirect_stdout(io.StringIO()):
            sbw.probe_latency_pool(self.api, ["A"], 8000)
        self.assertAlmostEqual(self.api.timeout, 11.0)

    def test_timeout_not_narrowed(self):
        # 已有更宽的超时（30s）不被收窄
        self.api.timeout = 30.0
        with mock.patch.object(sbw, "probe_latency", return_value=(None, None)), \
                contextlib.redirect_stdout(io.StringIO()):
            sbw.probe_latency_pool(self.api, ["A"], 2000)
        self.assertAlmostEqual(self.api.timeout, 30.0)

    def test_failed_node_maps_to_none_pair(self):
        def fake_probe(_api, name, _t):
            return (None, None) if name == "B" else (50, 1.0)

        with mock.patch.object(sbw, "probe_latency", side_effect=fake_probe), \
                contextlib.redirect_stdout(io.StringIO()):
            out = sbw.probe_latency_pool(self.api, ["A", "B"], 2000)
        self.assertEqual(out["A"], (50, 1.0))
        self.assertEqual(out["B"], (None, None))   # 失败节点也在 map，值为 (None, None)

    def test_progress_lines_match_web_regex(self):
        buf = io.StringIO()
        with mock.patch.object(sbw, "probe_latency", return_value=(74, 2.0)), \
                contextlib.redirect_stdout(buf):
            sbw.probe_latency_pool(self.api, ["A", "B", "C"], 2000)

        lines = [l for l in buf.getvalue().splitlines() if "Phase 1 粗筛" in l]
        self.assertEqual(len(lines), 3)
        seen = set()
        for line in lines:
            self.assertTrue(line.startswith("Phase 1 粗筛 "), line)
            self.assertIn("（主实例", line)
            self.assertIn("74±2 ms", line)
            m = WEB_PROGRESS_RE.search(line)       # Web 端正则必须能解析
            self.assertIsNotNone(m, line)
            seen.add((int(m.group(1)), int(m.group(2))))
        self.assertEqual({n for n, _total in seen}, {1, 2, 3})  # 序号 1..M 全覆盖
        self.assertEqual({t for _n, t in seen}, {3})

    def test_progress_line_without_jitter(self):
        buf = io.StringIO()
        with mock.patch.object(sbw, "probe_latency", return_value=(120, 0.0)), \
                contextlib.redirect_stdout(buf):
            sbw.probe_latency_pool(self.api, ["A"], 2000)
        line = [l for l in buf.getvalue().splitlines() if "Phase 1 粗筛" in l][0]
        self.assertIn("120 ms", line)        # jitter 为 0 时不带 ±
        self.assertNotIn("±", line)

    def test_empty_names_returns_empty_map(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sbw.probe_latency_pool(self.api, [], 2000), {})


class ProbeNodeInWorkerTest(unittest.TestCase):
    def setUp(self):
        self.args = mk_args()
        self.worker = mock.MagicMock(name="Worker")
        self.worker.proxy_url = "http://127.0.0.1:10001"

    def test_given_latency_skips_reprobe(self):
        with mock.patch.object(sbw, "probe_latency") as pl, \
                mock.patch.object(sbw, "fetch_exit_ips", return_value=(None, None, None)):
            r = sbw._probe_node_in_worker(self.worker, "节点A", "ss",
                                          self.args, 120, 3.0)
        pl.assert_not_called()                          # 不在 worker 内重测
        self.worker.api.proxy_delay.assert_not_called()
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.latency_ms, 120)
        self.assertEqual(r.jitter_ms, 3.0)

    def test_ip_profile_runs_with_given_latency(self):
        with mock.patch.object(sbw, "probe_latency") as pl, \
                mock.patch.object(sbw, "fetch_exit_ips",
                                  return_value=("203.0.113.9", None,
                                                IP_API_DATA)) as fip:
            r = sbw._probe_node_in_worker(self.worker, "节点A", "ss",
                                          self.args, 120, 3.0)
        pl.assert_not_called()
        self.worker.select.assert_called_once_with("节点A")     # 画像要先切节点
        fip.assert_called_once_with(self.worker.proxy_url, self.args.ip_timeout)
        self.assertIsNotNone(r.ip)
        self.assertTrue(r.ip.ok)
        self.assertEqual(r.ip.country_code, "JP")
        self.assertEqual(r.ip.kind, "代理/VPN")          # classify_ip 真跑

    def test_none_latency_falls_back_to_worker_reprobe(self):
        with mock.patch.object(sbw, "probe_latency",
                               return_value=(88, 1.5)) as pl, \
                mock.patch.object(sbw, "fetch_exit_ips", return_value=(None, None, None)):
            r = sbw._probe_node_in_worker(self.worker, "节点A", "ss",
                                          self.args, None, None)
        pl.assert_called_once_with(self.worker.api, "节点A", self.args.delay_timeout,
                                   count=3)
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.latency_ms, 88)
        self.assertEqual(r.jitter_ms, 1.5)

    def test_double_failure_marks_unreachable(self):
        # 主实例失败（传入 None）且 worker 兜底也失败 → 不通，且不碰 IP 画像
        with mock.patch.object(sbw, "probe_latency",
                               return_value=(None, None)) as pl, \
                mock.patch.object(sbw, "fetch_ip_info") as fip:
            r = sbw._probe_node_in_worker(self.worker, "节点A", "ss",
                                          self.args, None, None)
        pl.assert_called_once()
        self.assertEqual(r.status, "unreachable")
        self.assertIsNone(r.latency_ms)
        self.assertIsNone(r.ip)
        self.worker.select.assert_not_called()
        fip.assert_not_called()

    def test_no_ip_skips_profile(self):
        args = mk_args(no_ip=True)
        with mock.patch.object(sbw, "probe_latency") as pl, \
                mock.patch.object(sbw, "fetch_ip_info") as fip:
            r = sbw._probe_node_in_worker(self.worker, "节点A", "ss",
                                          args, 120, 3.0)
        pl.assert_not_called()
        fip.assert_not_called()
        self.worker.select.assert_not_called()
        self.assertIsNone(r.ip)
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.latency_ms, 120)


class RunPoolMainApiTest(unittest.TestCase):
    PROXIES = [{"name": "A", "type": "ss", "server": "a.example.com"},
               {"name": "B", "type": "trojan", "server": "b.example.com"}]

    def _run(self, args, main_api, latency_map):
        """最小 patch 跑 run_pool 全程；返回 (results, calls, worker_cls, stdout)。

        calls 依序记录 ("pool", names) / ("probe", name, lat, jit) /
        ("speed", name)，用于断言编排顺序与参数传递。
        """
        calls = []

        def fake_pool(api, names, timeout_ms):
            calls.append(("pool", list(names), timeout_ms))
            return dict(latency_map)

        def fake_probe(worker, name, proto, a, lat=None, jit=None):
            calls.append(("probe", name, lat, jit))
            return csb.Result(name=name, provider="", proto=proto,
                              latency_ms=lat, speeds_mbps=[], median_mbps=None,
                              best_mbps=None,
                              status="ok" if lat is not None else "unreachable",
                              jitter_ms=jit)

        def fake_speed(worker, r, a):
            calls.append(("speed", r.name))
            r.speeds_mbps = [30.0]
            r.median_mbps = 30.0
            r.status = "ok"

        buf = io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                sbw, "find_mihomo_bin", return_value="/fake/mihomo"))
            stack.enter_context(mock.patch.object(
                sbw, "extract_proxies", return_value=list(self.PROXIES)))
            stack.enter_context(mock.patch.object(
                sbw, "physical_interface", return_value="en0"))
            stack.enter_context(mock.patch.object(
                sbw, "build_hosts", return_value=dict(HOSTS)))
            stack.enter_context(mock.patch.object(
                sbw, "probe_latency_pool", side_effect=fake_pool))
            worker_cls = stack.enter_context(mock.patch.object(sbw, "Worker"))
            stack.enter_context(mock.patch.object(
                sbw, "_probe_node_in_worker", side_effect=fake_probe))
            stack.enter_context(mock.patch.object(
                sbw, "_speed_node_in_worker", side_effect=fake_speed))
            stack.enter_context(contextlib.redirect_stdout(buf))
            results = sbw.run_pool(
                ["A", "B"], {"A": "ss", "B": "trojan"}, args, main_api=main_api)
        return results, calls, worker_cls, buf.getvalue()

    def test_no_ip_all_ok_builds_results_without_phase1_workers(self):
        main_api = mock.Mock(name="MainAPI")
        results, calls, worker_cls, out = self._run(
            mk_args(no_ip=True), main_api, {"A": (100, 2.0), "B": (200, 5.0)})

        # 先主实例探测，且 Phase 1 不再起 worker（仅 Phase 2 那一个）
        self.assertEqual(calls[0], ("pool", ["A", "B"], 2000))
        self.assertFalse(any(c[0] == "probe" for c in calls))
        worker_cls.assert_called_once()      # 只有 Phase 2 的 worker2
        self.assertNotIn("IP 画像", out)
        # 结果直接由 latency_map 构建
        by_name = {r.name: r for r in results}
        self.assertEqual(set(by_name), {"A", "B"})
        self.assertEqual(by_name["A"].latency_ms, 100)
        self.assertEqual(by_name["A"].jitter_ms, 2.0)
        self.assertEqual(by_name["A"].proto, "ss")
        self.assertEqual(by_name["A"].status, "ok")
        self.assertEqual(by_name["B"].latency_ms, 200)
        self.assertEqual(by_name["B"].jitter_ms, 5.0)
        # Phase 2 照常串行精测连通节点
        self.assertEqual([c[1] for c in calls if c[0] == "speed"], ["A", "B"])

    def test_no_ip_main_api_accepts_real_probe_stats(self):
        # Production probe_latency_pool returns ProbeStats rather than a tuple.
        # Keep this path covered because --no-ip skips the Phase 1 worker and
        # therefore inspects latency_map directly.
        main_api = mock.Mock(name="MainAPI")
        stats = {
            "A": csb.ProbeStats(100, 2.0, 3, 2, 1),
            "B": csb.ProbeStats(200, 5.0, 3, 3, 0),
        }
        results, calls, worker_cls, _out = self._run(
            mk_args(no_ip=True), main_api, stats)

        self.assertFalse(any(c[0] == "probe" for c in calls))
        by_name = {r.name: r for r in results}
        self.assertEqual(by_name["A"].latency_ms, 100)
        self.assertEqual(by_name["A"].probe_failures, 1)
        self.assertEqual(by_name["A"].probe_loss_pct, 33.3)
        self.assertEqual(by_name["B"].probe_successes, 3)
        worker_cls.assert_called_once()

    def test_main_api_failure_nodes_fall_back_to_worker(self):
        main_api = mock.Mock(name="MainAPI")
        results, calls, worker_cls, out = self._run(
            mk_args(), main_api, {"A": (100, 2.0), "B": (None, None)})

        # 调用序：先主实例 pool，再进 worker 探测（含兜底）
        self.assertEqual(calls[0], ("pool", ["A", "B"], 2000))
        probes = [c for c in calls if c[0] == "probe"]
        self.assertTrue(probes)
        self.assertGreater(calls.index(probes[0]), calls.index(("pool", ["A", "B"], 2000)))
        by_name = {c[1]: c for c in probes}
        self.assertEqual(by_name["A"], ("probe", "A", 100, 2.0))   # 主实例结果直接透传
        self.assertEqual(by_name["B"], ("probe", "B", None, None))  # 失败节点 None 进 worker 兜底
        self.assertTrue(worker_cls.called)     # 有 IP 画像/兜底活要干，起了 worker

    def test_no_main_api_falls_back_to_old_behavior(self):
        results, calls, worker_cls, out = self._run(mk_args(), None, {})

        self.assertFalse(any(c[0] == "pool" for c in calls))   # 不碰主实例探测
        probes = [c for c in calls if c[0] == "probe"]
        self.assertEqual({c[1] for c in probes}, {"A", "B"})
        # 没有 latency_map，全量节点拿 None 进 worker 内测延迟（旧行为）
        for c in probes:
            self.assertEqual(c[2:], (None, None))
        self.assertTrue(worker_cls.called)

    def test_no_main_api_no_ip_still_probes_all_in_worker(self):
        # main_api 缺省时 latency_map 为空：即使 --no-ip，全部节点也进 worker 测延迟
        results, calls, worker_cls, out = self._run(mk_args(no_ip=True), None, {})
        self.assertFalse(any(c[0] == "pool" for c in calls))
        self.assertEqual({c[1] for c in calls if c[0] == "probe"}, {"A", "B"})
        self.assertGreaterEqual(worker_cls.call_count, 2)  # Phase 1 分片 + Phase 2

    def test_phase2_scope_line_no_duplicated_count(self):
        # 回归：scope 曾含 f"Top {len}" 又拼「{len} 个节点」，打出「Top 15 15 个节点」
        results, calls, worker_cls, out = self._run(
            mk_args(no_ip=True), mock.Mock(name="MainAPI"),
            {"A": (100, 2.0), "B": (200, 5.0)})
        self.assertIn("Phase 2 精测: Top 2 个节点", out)
        self.assertNotIn("Top 2 2", out)

    def test_phase2_scope_line_all(self):
        results, calls, worker_cls, out = self._run(
            mk_args(no_ip=True, all=True), mock.Mock(name="MainAPI"),
            {"A": (100, 2.0), "B": (200, 5.0)})
        self.assertIn("Phase 2 精测: 全部 2 个节点", out)


class Phase1InterruptCleanupTest(unittest.TestCase):
    """Phase 1 池化阶段被 KeyboardInterrupt 打断时的临时 worker 清理。

    v0.8.0 Windows 真机验收实测复现：面板中断测速后残留 5 个孤儿
    verge-mihomo 进程——CTRL_BREAK 只打断主线程的 pool.map，分片线程感知
    不到、继续逐节点探测，with 池 __exit__ 的 shutdown(wait=True) 卡到
    面板 5 秒强杀兜底，worker 的 stop() 永远跑不到。修复 = 取消标志
    （节点间检查）+ 已启动 worker 注册表（中断即统停，在途探测立刻失败）。
    """

    PROXIES = [{"name": "A", "type": "ss", "server": "a.example.com"},
               {"name": "B", "type": "trojan", "server": "b.example.com"}]

    def test_keyboard_interrupt_stops_all_started_workers(self):
        started, stopped = [], []

        class FakeWorker:
            def __init__(self, *a, **k):
                self.api = None
                self.proxy_url = ""

            def start(self):
                self.api = object()
                started.append(self)

            def stop(self):
                if self not in stopped:  # 模拟真实 Worker.stop 的幂等语义
                    stopped.append(self)

        buf = io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                sbw, "find_mihomo_bin", return_value="/fake/mihomo"))
            stack.enter_context(mock.patch.object(
                sbw, "extract_proxies", return_value=list(self.PROXIES)))
            stack.enter_context(mock.patch.object(
                sbw, "physical_interface", return_value="en0"))
            stack.enter_context(mock.patch.object(
                sbw, "build_hosts", return_value=dict(HOSTS)))
            stack.enter_context(mock.patch.object(sbw, "Worker", FakeWorker))
            # 探测一跑就抛 KeyboardInterrupt：BaseException，guarded 的
            # except Exception 兜不住，经 future 抛回主线程的 pool.map
            stack.enter_context(mock.patch.object(
                sbw, "_probe_node_in_worker", side_effect=KeyboardInterrupt))
            stack.enter_context(contextlib.redirect_stdout(buf))
            with self.assertRaises(KeyboardInterrupt):
                sbw.run_pool(["A", "B"], {"A": "ss", "B": "trojan"},
                             mk_args(workers=2), main_api=None)
        # 孤儿进程回归保护：所有已启动的临时 worker 都必须被停掉
        self.assertTrue(started)
        self.assertEqual({id(w) for w in started}, {id(w) for w in stopped})

    def test_worker_stop_idempotent(self):
        # 中断统一清理 + shard_loop 的 finally 会重复 stop，必须只真正执行一次
        w = sbw.Worker("mihomo", [], {}, None)
        calls = []

        class FakeProc:
            def poll(self):
                return None

            def terminate(self):
                calls.append("terminate")

            def wait(self, timeout=None):
                calls.append("wait")
                return 0

            def kill(self):
                calls.append("kill")

        w.proc = FakeProc()
        w.dir = None
        w.stop()
        w.stop()
        self.assertEqual(calls, ["terminate", "wait"])

    def test_worker_start_failure_is_cleaned(self):
        """A failed startup still owns a temp process/config and must stop it."""
        started, stopped = [], []

        class FailingWorker:
            def __init__(self, *a, **k):
                self.api = None
                self.proxy_url = ""

            def start(self):
                started.append(self)
                raise RuntimeError("startup failed")

            def stop(self):
                stopped.append(self)

        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                sbw, "find_mihomo_bin", return_value="/fake/mihomo"))
            stack.enter_context(mock.patch.object(
                sbw, "extract_proxies", return_value=list(self.PROXIES)))
            stack.enter_context(mock.patch.object(
                sbw, "physical_interface", return_value="en0"))
            stack.enter_context(mock.patch.object(
                sbw, "build_hosts", return_value=dict(HOSTS)))
            stack.enter_context(mock.patch.object(sbw, "Worker", FailingWorker))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            results = sbw.run_pool(
                ["A", "B"], {"A": "ss", "B": "trojan"},
                mk_args(workers=2), main_api=None)

        self.assertEqual(len(results), 2)
        self.assertEqual({id(w) for w in started}, {id(w) for w in stopped})


if __name__ == "__main__":
    unittest.main()
