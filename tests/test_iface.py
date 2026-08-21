# -*- coding: utf-8 -*-
"""v0.4 网卡相关测试（全 mock，不起 mihomo、不连网）：

- is_virtual_iface：utun/ipsec/ppp/tun/tap 前缀、大小写敏感、None/空串
- Worker 配置生成（patch subprocess.Popen 与 MihomoAPI）：
  顶层全局 interface-name、节点 dict 不被逐条改写、节点原值保留、
  调用方 dict 不被修改（写入配置的是副本）
- run_pool：默认路由落在虚拟接口时抛 VirtualDefaultRoute（在 build_hosts 之前）；
  物理网卡/未知网卡则越过检测；build_hosts 收到依赖闭包后的节点列表
- Worker.start 失败路径与 stop 清理
"""
import contextlib
import copy
import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import speedbench_workers as sbw


class IsVirtualIfaceTest(unittest.TestCase):
    def test_virtual_prefixes_true(self):
        for name in ("utun", "utun0", "utun4", "utun10",
                     "ipsec", "ipsec0", "ppp", "ppp0",
                     "tun", "tun0", "tap", "tap9"):
            with self.subTest(name=name):
                self.assertTrue(sbw.is_virtual_iface(name))

    def test_physical_and_other_ifaces_false(self):
        for name in ("en0", "en1", "lo0", "eth0", "wlan0", "bridge0",
                     "gif0", "stf0", "anpi0", "awdl0", "llw0"):
            with self.subTest(name=name):
                self.assertFalse(sbw.is_virtual_iface(name))

    def test_none_and_empty_false(self):
        self.assertFalse(sbw.is_virtual_iface(None))
        self.assertFalse(sbw.is_virtual_iface(""))

    def test_case_sensitive(self):
        # macOS 接口名恒小写（route get default 输出的就是小写），
        # 大写形式不是真实接口命名，按非虚拟接口处理
        for name in ("UTUN0", "Utun0", "TUN0", "IPSEC0", "Tap0"):
            with self.subTest(name=name):
                self.assertFalse(sbw.is_virtual_iface(name))

    def test_prefixes_constant(self):
        self.assertEqual(set(sbw.VIRTUAL_IFACE_PREFIXES),
                         {"utun", "ipsec", "ppp", "tun", "tap"})


HOSTS = {"speed.cloudflare.com": "1.1.1.1",
         "cp.cloudflare.com": "1.1.1.1",
         "ip-api.com": "208.95.112.1"}


def start_worker(proxies, iface, poll_value=None, api_ok=True, time_frozen=False):
    """patch Popen / MihomoAPI（可选再冻结 time）后跑 Worker.start()。

    返回 (worker, popen_mock, api_cls_mock, cfg_spy, fake_proc)。
    cfg_spy 是传给 json.dumps 的内存 cfg 对象（写盘前捕获，可查对象身份）。
    """
    worker = sbw.Worker("/fake/mihomo", proxies, HOSTS, iface)
    fake_proc = mock.MagicMock(name="FakeMihomoProc")
    fake_proc.poll.return_value = poll_value

    captured = {}
    real_dumps = json.dumps

    def dumps_spy(obj, *a, **kw):
        captured["cfg"] = obj
        return real_dumps(obj, *a, **kw)

    api_cls = mock.MagicMock(name="MihomoAPIClass")
    if api_ok:
        api_cls.return_value.get.return_value = {"version": "mihomo fake"}
    else:
        api_cls.return_value.get.side_effect = RuntimeError("api not ready")

    stack = ExitStack()
    with stack:
        popen_mock = stack.enter_context(
            mock.patch.object(sbw.subprocess, "Popen", return_value=fake_proc))
        stack.enter_context(mock.patch.object(sbw, "MihomoAPI", api_cls))
        stack.enter_context(
            mock.patch.object(sbw.json, "dumps", side_effect=dumps_spy))
        if time_frozen:
            # deadline=t(0)+8；循环内 t(1) 进一次、API 未就绪；t(100) 出循环判超时
            stack.enter_context(
                mock.patch.object(sbw.time, "time", side_effect=[0.0, 1.0, 100.0]))
            stack.enter_context(mock.patch.object(sbw.time, "sleep"))
        try:
            worker.start()
        except Exception:
            if worker.dir is not None:
                worker.dir.cleanup()  # 失败路径也清掉临时目录，避免 ResourceWarning
            raise
    return worker, popen_mock, api_cls, captured.get("cfg"), fake_proc


class WorkerConfigTest(unittest.TestCase):
    def setUp(self):
        self.node_own = {"name": "自有接口节点", "type": "ss",
                         "server": "a.example.com", "port": 443,
                         "interface-name": "en7"}
        self.node_bare = {"name": "裸节点", "type": "trojan",
                          "server": "198.51.100.7", "port": 443}
        self.proxies = [self.node_own, self.node_bare]
        self.before = copy.deepcopy(self.proxies)

    def read_cfg(self, worker):
        cfg_path = Path(worker.dir.name) / "config.json"
        return json.loads(cfg_path.read_text(encoding="utf-8"))

    def test_top_level_interface_name_set(self):
        worker, *_ = start_worker(self.proxies, "en0")
        self.addCleanup(worker.stop)
        cfg = self.read_cfg(worker)
        self.assertEqual(cfg["interface-name"], "en0")

    def test_no_interface_name_when_iface_none(self):
        worker, *_ = start_worker(self.proxies, None)
        self.addCleanup(worker.stop)
        cfg = self.read_cfg(worker)
        self.assertNotIn("interface-name", cfg)

    def test_node_dicts_not_rewritten(self):
        # 节点自带 interface-name 原值保留；裸节点不补 interface-name
        worker, *_ = start_worker(self.proxies, "en0")
        self.addCleanup(worker.stop)
        cfg = self.read_cfg(worker)
        self.assertEqual(cfg["proxies"], self.before)
        self.assertEqual(cfg["proxies"][0]["interface-name"], "en7")
        self.assertNotIn("interface-name", cfg["proxies"][1])

    def test_caller_dicts_untouched(self):
        worker, *_ = start_worker(self.proxies, "en0")
        self.addCleanup(worker.stop)
        self.assertEqual(self.proxies, self.before)

    def test_config_uses_copied_node_dicts(self):
        # 写进配置的节点是副本，不是调用方共享的 dict 对象本身
        worker, _p, _a, cfg_spy, _proc = start_worker(self.proxies, "en0")
        self.addCleanup(worker.stop)
        self.assertIsNotNone(cfg_spy)
        for got, orig in zip(cfg_spy["proxies"], self.proxies):
            self.assertIsNot(got, orig)
            self.assertEqual(got, orig)

    def test_config_baseline_fields(self):
        worker, *_ = start_worker(self.proxies, "en0")
        self.addCleanup(worker.stop)
        cfg = self.read_cfg(worker)
        self.assertEqual(cfg["mode"], "global")
        self.assertEqual(cfg["hosts"], HOSTS)
        self.assertFalse(cfg["allow-lan"])
        self.assertFalse(cfg["ipv6"])
        self.assertEqual(cfg["log-level"], "warning")
        self.assertIsInstance(cfg["mixed-port"], int)
        ctl = cfg["external-controller"]
        self.assertTrue(ctl.startswith("127.0.0.1:"))
        self.assertNotEqual(cfg["mixed-port"], int(ctl.rsplit(":", 1)[1]))

    def test_popen_invocation(self):
        worker, popen_mock, *_ = start_worker(self.proxies, "en0")
        self.addCleanup(worker.stop)
        popen_mock.assert_called_once()
        args, kwargs = popen_mock.call_args
        cmd = args[0]
        self.assertEqual(cmd[0], "/fake/mihomo")
        self.assertEqual(cmd[1], "-f")
        self.assertTrue(cmd[2].endswith("config.json"))
        self.assertEqual(cmd[3], "-d")
        self.assertEqual(cmd[4], worker.dir.name)
        self.assertEqual(kwargs.get("stdout"), subprocess.DEVNULL)
        self.assertEqual(kwargs.get("stderr"), subprocess.DEVNULL)

    def test_api_and_proxy_url_point_at_ports(self):
        worker, _p, api_cls, _cfg, _proc = start_worker(self.proxies, "en0")
        self.addCleanup(worker.stop)
        api_args, api_kwargs = api_cls.call_args
        ctl_port = int(api_args[0].rsplit(":", 1)[1])
        self.assertTrue(api_args[0].startswith("http://127.0.0.1:"))
        self.assertEqual(api_kwargs.get("timeout"), 8.0)
        mix_port = int(worker.proxy_url.rsplit(":", 1)[1])
        self.assertTrue(worker.proxy_url.startswith("http://127.0.0.1:"))
        self.assertNotEqual(mix_port, ctl_port)

    def test_start_fails_if_proc_exits_immediately(self):
        with self.assertRaises(sbw.WorkerUnavailable) as cm:
            start_worker(self.proxies, "en0", poll_value=1)
        self.assertIn("立即退出", str(cm.exception))

    def test_start_times_out_if_api_never_ready(self):
        with self.assertRaises(sbw.WorkerUnavailable) as cm:
            start_worker(self.proxies, "en0", api_ok=False, time_frozen=True)
        self.assertIn("超时", str(cm.exception))

    def test_stop_terminates_and_cleans_tempdir(self):
        worker, _p, _a, _cfg, fake_proc = start_worker(self.proxies, "en0")
        dir_name = worker.dir.name
        self.assertTrue(os.path.isdir(dir_name))
        worker.stop()
        fake_proc.terminate.assert_called_once_with()
        fake_proc.wait.assert_called_once_with(timeout=3)
        self.assertFalse(os.path.exists(dir_name))

    def test_stop_skips_terminate_when_already_exited(self):
        worker, _p, _a, _cfg, fake_proc = start_worker(self.proxies, "en0")
        dir_name = worker.dir.name
        fake_proc.poll.return_value = 0   # 进程已退出
        worker.stop()
        fake_proc.terminate.assert_not_called()
        fake_proc.kill.assert_not_called()
        self.assertFalse(os.path.exists(dir_name))


class _ReachedBuildHosts(Exception):
    """build_hosts 被调到的哨兵异常（证明越过了虚拟接口检测点）。"""


class RunPoolVirtualRouteTest(unittest.TestCase):
    PROXIES = [{"name": "A", "type": "ss", "server": "a.example.com"}]

    def _run_until_hosts(self, iface, proxies, candidates=("A",)):
        """最小 patch 跑到 build_hosts 检测点；返回 (异常或 None, hosts_mock)。"""
        args = SimpleNamespace(config_file="/fake/clash-verge.yaml")
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                sbw, "find_mihomo_bin", return_value="/fake/mihomo"))
            stack.enter_context(mock.patch.object(
                sbw, "extract_proxies", return_value=proxies))
            stack.enter_context(mock.patch.object(
                sbw, "physical_interface", return_value=iface))
            hosts_mock = stack.enter_context(mock.patch.object(
                sbw, "build_hosts", side_effect=_ReachedBuildHosts))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            exc = None
            try:
                sbw.run_pool(list(candidates),
                             {n: "ss" for n in candidates}, args)
            except Exception as e:  # noqa: BLE001 - 测试里捕一切再断言类型
                exc = e
        return exc, hosts_mock

    def test_virtual_default_route_raises(self):
        for iface in ("utun4", "ipsec0", "ppp1", "tun0", "tap9"):
            with self.subTest(iface=iface):
                exc, hosts_mock = self._run_until_hosts(iface, self.PROXIES)
                self.assertIsInstance(exc, sbw.VirtualDefaultRoute)
                self.assertIn(iface, str(exc))
                hosts_mock.assert_not_called()  # 在 DoH 解析之前就拒绝启动

    def test_virtual_default_route_is_worker_unavailable(self):
        # 继承 WorkerUnavailable，main() 才能走同一条回退路径
        self.assertTrue(issubclass(sbw.VirtualDefaultRoute, sbw.WorkerUnavailable))

    def test_physical_iface_passes_virtual_check(self):
        exc, hosts_mock = self._run_until_hosts("en0", self.PROXIES)
        self.assertIsInstance(exc, _ReachedBuildHosts)
        hosts_mock.assert_called_once()

    def test_unknown_iface_passes_virtual_check(self):
        # 拿不到默认路由接口（None）只警告，不阻断
        exc, hosts_mock = self._run_until_hosts(None, self.PROXIES)
        self.assertIsInstance(exc, _ReachedBuildHosts)
        hosts_mock.assert_called_once()

    def test_build_hosts_receives_dependency_closure(self):
        # 依赖闭包里的前置节点 server 域名也要钉住，否则链式拨号拿到 fake-ip
        a = {"name": "A", "type": "ss", "server": "a.example.com",
             "dialer-proxy": "B"}
        b = {"name": "B", "type": "ss", "server": "b.example.com"}
        exc, hosts_mock = self._run_until_hosts("en0", [a, b])
        self.assertIsInstance(exc, _ReachedBuildHosts)
        got = hosts_mock.call_args[0][0]
        self.assertEqual([p["name"] for p in got], ["A", "B"])


if __name__ == "__main__":
    unittest.main()
