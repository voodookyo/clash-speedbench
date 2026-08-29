# -*- coding: utf-8 -*-
"""Windows（win32）平台分支测试：在 macOS/Linux 上用 unittest.mock 模拟
sys.platform="win32" 运行，不起真子进程、不碰真文件系统、不改真信号状态以外的
进程资源（SIGBREAK 用例注册到真实信号号上但 finally 恢复原 handler）。

覆盖：
- DEFAULT_CONTROLLERS：win32 剔除 unix:// 候选（import 时常量，reload 重算，
  finally 再 reload 恢复，不污染其他测试）
- find_mihomo_bin：候选顺序、os.access 过滤、which 兜底顺序（verge-mihomo 优先）
- MIHOMO_BIN_CANDIDATES/CONFIG_CANDIDATES 的 expandvars 展开与 % 残留过滤
  （os.path.expandvars 按 %VAR% 语法 fake，模拟环境变量缺失场景）
- physical_interface：首选 Find-NetRoute（路由栈真实选择；mihomo TUN 双 0/0
  路由同 RouteMetric 时 Get-NetRoute 排序乱序会漏检，真机实测），失败回退
  Get-NetRoute；输出解析（含中文「以太网」）、encoding/errors/timeout 参数、
  非零/超时/空输出/进程不存在 → None
- is_virtual_iface：win32 大小写不敏感 + WIN_VIRTUAL_IFACE_PREFIXES 并集；
  posix 分支行为不变
- cancel_benchmark：win32 写哨兵文件（子进程 cancel_requested 轮询）、
  5s 等待、terminate→kill 兜底；posix 首发 SIGINT 不变
- run_benchmark 的 Popen：win32 带 CREATE_NO_WINDOW，posix 不带；
  两平台 stdin=DEVNULL、env 带 SPEEDBENCH_CANCEL_FILE
- _no_window_kwargs：win32/posix 两分支 + curl_speed 集成
- main() 的 SIGBREAK 注册：win32 注册 KeyboardInterrupt 转换 handler；posix 不注册
- SpeedBench.bat：纯 ASCII / 无 BOM / CRLF / 优先 pythonw 无窗口启动且
  保留 python 最小化控制台兜底
  （UTF-8 中文 .bat 会被 cmd.exe 错乱解析的真机回归保护）
- pipe:// 命名管道 controller：候选按平台过滤（win32 含 pipe、posix 剔除），
  scheme 解析、假 plumbing 上的 HTTP 往返（Content-Length + chunked）、
  posix 明确报错、_PipeSock 关闭幂等
- 子进程文本输出钉 UTF-8（curl_speed / fetch_ip_info / doh_resolve）：
  中文 Windows 的 GBK 默认解码会炸 UnicodeDecodeError（真机实测）
- 进程内托盘（speedbench_tray.py，ctypes/Win32）：posix 纯 no-op、
  web main() 的启动/摘除接线、speedbench.ico 结构合法、release.yml
  Windows 打包清单收录托盘模块与图标
"""
import contextlib
import importlib
import io
import os
import re
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import clash_speedbench as csb
import speedbench_workers as sbw
import speedbench_web as web

TIMEOUT = subprocess.TimeoutExpired(cmd="fake", timeout=5)


@contextlib.contextmanager
def reload_with_platform(module, platform):
    """以指定 sys.platform 重载模块（import 时常量按新平台重算）；
    退出时再按真实平台 reload 一次恢复，避免污染其他测试。"""
    with mock.patch.object(sys, "platform", platform):
        importlib.reload(module)
    try:
        yield module
    finally:
        importlib.reload(module)


class DefaultControllersTest(unittest.TestCase):
    def test_win32_filters_out_unix_controllers(self):
        with reload_with_platform(csb, "win32"):
            self.assertTrue(csb._ALL_CONTROLLERS[0].startswith("unix://"))  # 全量表不变
            self.assertNotEqual(csb.DEFAULT_CONTROLLERS, csb._ALL_CONTROLLERS)
            self.assertEqual([c for c in csb.DEFAULT_CONTROLLERS
                              if c.startswith("unix://")], [])
            # win32：命名管道候选在最前（Verge Windows 默认/唯一通道），TCP 候选兜底
            self.assertTrue(csb.DEFAULT_CONTROLLERS[0].startswith("pipe://"))
            self.assertEqual(csb.DEFAULT_CONTROLLERS,
                             tuple(c for c in csb._ALL_CONTROLLERS
                                   if not c.startswith("unix://")))
            # detect_controller 的候选来自 DEFAULT_CONTROLLERS：报错信息里不含 unix
            with mock.patch.object(csb.MihomoAPI, "get",
                                   side_effect=csb.ApiError("down")):
                with self.assertRaises(csb.ApiError) as cm:
                    csb.detect_controller("", None)
            self.assertNotIn("unix://", str(cm.exception))
            self.assertIn("pipe://verge-mihomo", str(cm.exception))
            self.assertIn("http://127.0.0.1:9097", str(cm.exception))

    def test_posix_keeps_unix_controller_first(self):
        with reload_with_platform(csb, "darwin"):
            self.assertTrue(csb.DEFAULT_CONTROLLERS[0].startswith("unix://"))
            # pipe:// 是 Windows 专属通道，posix 候选里剔除；其余全量保留
            self.assertEqual(csb.DEFAULT_CONTROLLERS,
                             tuple(c for c in csb._ALL_CONTROLLERS
                                   if not c.startswith("pipe://")))


# 模拟 Windows 环境变量：故意不设 ProgramFiles(x86)，验证 % 残留项被过滤
FAKE_WIN_ENV = {
    "LOCALAPPDATA": r"C:\Users\tester\AppData\Local",
    "ProgramFiles": r"C:\Program Files",
    "APPDATA": r"C:\Users\tester\AppData\Roaming",
}


def fake_win_expandvars(env):
    """按 Windows %VAR% 语法展开的 expandvars fake；未设置的变量原样保留（残留 %）。"""
    def expand(s):
        return re.sub(r"%([^%]+)%",
                      lambda m: env.get(m.group(1), m.group(0)), s)
    return expand


class WinCandidatePathsTest(unittest.TestCase):
    """win32 下 MIHOMO_BIN_CANDIDATES / CONFIG_CANDIDATES 的构造（import 时常量）。"""

    @contextlib.contextmanager
    def reload_workers_win32(self, env):
        with mock.patch.object(sys, "platform", "win32"), \
                mock.patch("os.path.expandvars", fake_win_expandvars(env)):
            importlib.reload(sbw)
        try:
            yield sbw
        finally:
            importlib.reload(sbw)

    def test_candidates_expanded_and_residue_filtered(self):
        with self.reload_workers_win32(FAKE_WIN_ENV):
            self.assertEqual(sbw.MIHOMO_BIN_CANDIDATES, (
                r"C:\Users\tester\AppData\Local\Programs\Clash Verge\verge-mihomo.exe",
                r"C:\Program Files\Clash Verge\verge-mihomo.exe",
            ))  # %ProgramFiles(x86)% 未设置 → 残留 % → 被过滤
            self.assertEqual(sbw.CONFIG_CANDIDATES, (
                r"C:\Users\tester\AppData\Roaming"
                r"\io.github.clash-verge-rev.clash-verge-rev\clash-verge.yaml",
            ))

    def test_all_env_missing_yields_empty_candidates(self):
        with self.reload_workers_win32({}):
            self.assertEqual(sbw.MIHOMO_BIN_CANDIDATES, ())
            self.assertEqual(sbw.CONFIG_CANDIDATES, ())


class FindMihomoBinWinTest(unittest.TestCase):
    CANDIDATES = (r"C:\a\verge-mihomo.exe", r"C:\b\verge-mihomo.exe")

    def run_find(self, isfile_side, access_side=None, which_side=None,
                 platform="win32"):
        with mock.patch.object(sbw, "MIHOMO_BIN_CANDIDATES", self.CANDIDATES), \
                mock.patch.object(sys, "platform", platform), \
                mock.patch("os.path.isfile", side_effect=isfile_side) as m_isfile, \
                mock.patch("os.access",
                           side_effect=access_side or (lambda p, m: True)) as m_access, \
                mock.patch.object(sbw.shutil, "which",
                                  side_effect=which_side) as m_which:
            return sbw.find_mihomo_bin(), m_isfile, m_access, m_which

    def test_first_candidate_hit(self):
        got, m_isfile, m_access, m_which = self.run_find([True])
        self.assertEqual(got, self.CANDIDATES[0])
        m_which.assert_not_called()

    def test_candidate_order_preserved(self):
        got, m_isfile, m_access, _ = self.run_find([False, True])
        self.assertEqual(got, self.CANDIDATES[1])
        # 先查第一个（不存在）再查第二个；access 只对存在的文件调用
        self.assertEqual([c.args[0] for c in m_isfile.call_args_list],
                         list(self.CANDIDATES))
        m_access.assert_called_once_with(self.CANDIDATES[1], os.X_OK)

    def test_non_executable_candidate_skipped(self):
        got, _f, _a, m_which = self.run_find(
            [True, False], access_side=lambda p, m: False,
            which_side=lambda name: r"C:\tools\verge-mihomo.exe")
        self.assertEqual(got, r"C:\tools\verge-mihomo.exe")
        m_which.assert_called_once_with("verge-mihomo")

    def test_which_fallback_verge_mihomo_first(self):
        # PATH 兜底：verge-mihomo 命中则不再查 mihomo
        got, _f, _a, m_which = self.run_find(
            [False, False], which_side=lambda name: r"C:\tools\verge-mihomo.exe")
        self.assertEqual(got, r"C:\tools\verge-mihomo.exe")
        self.assertEqual([c.args[0] for c in m_which.call_args_list],
                         ["verge-mihomo"])

    def test_which_fallback_mihomo_second(self):
        got, _f, _a, m_which = self.run_find(
            [False, False],
            which_side=lambda name: {"verge-mihomo": None,
                                     "mihomo": r"C:\tools\mihomo.exe"}[name])
        self.assertEqual(got, r"C:\tools\mihomo.exe")
        self.assertEqual([c.args[0] for c in m_which.call_args_list],
                         ["verge-mihomo", "mihomo"])  # 兜底顺序：自带内核名优先

    def test_nothing_found_returns_none(self):
        got, *_ = self.run_find([False, False], which_side=lambda name: None)
        self.assertIsNone(got)

    def test_posix_never_tries_verge_mihomo_name(self):
        got, _f, _a, m_which = self.run_find(
            [False, False], which_side=lambda name: "/usr/local/bin/mihomo",
            platform="darwin")
        self.assertEqual(got, "/usr/local/bin/mihomo")
        m_which.assert_called_once_with("mihomo")  # posix 只查 mihomo


class PhysicalInterfaceWinTest(unittest.TestCase):
    def run_iface(self, run_side_effect=None, run_return=None, which=None,
                  platform="win32"):
        with mock.patch.object(sys, "platform", platform), \
                mock.patch.object(sbw.shutil, "which", return_value=which), \
                mock.patch.object(sbw.subprocess, "run",
                                  side_effect=run_side_effect,
                                  return_value=run_return) as m_run:
            return sbw.physical_interface(), m_run

    @staticmethod
    def cp(stdout="", returncode=0):
        return subprocess.CompletedProcess(args=["powershell"], returncode=returncode,
                                           stdout=stdout)

    def test_parses_powershell_output(self):
        got, m_run = self.run_iface(run_return=self.cp("Ethernet\n"), which=None)
        self.assertEqual(got, "Ethernet")
        cmd, kwargs = m_run.call_args
        self.assertEqual(cmd[0][0], "powershell.exe")   # which 未命中时用 .exe 兜底
        # 首选 Find-NetRoute 问路由栈的真实选择（mihomo TUN 双 0/0 路由同
        # RouteMetric 时 Sort-Object 乱序会漏检，真机实测踩过）
        self.assertIn("Find-NetRoute", cmd[0][-1])
        # 中文系统网卡名解码依赖这两个参数
        self.assertEqual(kwargs["encoding"], "utf-8")
        self.assertEqual(kwargs["errors"], "replace")
        self.assertEqual(kwargs["timeout"], 10)
        self.assertTrue(kwargs["capture_output"])

    def test_falls_back_to_get_netroute_when_find_fails(self):
        # Find-NetRoute 拿不到（离线/老系统）时回退旧 Get-NetRoute 命令
        got, m_run = self.run_iface(run_side_effect=[
            self.cp(""), self.cp("以太网\r\n")])
        self.assertEqual(got, "以太网")
        self.assertEqual(m_run.call_count, 2)
        self.assertIn("Find-NetRoute", m_run.call_args_list[0][0][0][-1])
        self.assertIn("Get-NetRoute", m_run.call_args_list[1][0][0][-1])

    def test_chinese_iface_name(self):
        got, _ = self.run_iface(run_return=self.cp("以太网\r\n"),
                                which=r"C:\Windows\powershell.exe")
        self.assertEqual(got, "以太网")   # CRLF 与中文都正确处理

    def test_nonzero_returncode_returns_none(self):
        got, _ = self.run_iface(run_return=self.cp("Ethernet", returncode=1))
        self.assertIsNone(got)

    def test_empty_output_returns_none(self):
        for out in ("", "  \r\n"):
            with self.subTest(out=out):
                got, _ = self.run_iface(run_return=self.cp(out))
                self.assertIsNone(got)

    def test_timeout_returns_none(self):
        got, _ = self.run_iface(
            run_side_effect=subprocess.TimeoutExpired(cmd="powershell", timeout=10))
        self.assertIsNone(got)

    def test_powershell_missing_returns_none(self):
        got, _ = self.run_iface(run_side_effect=FileNotFoundError("powershell"))
        self.assertIsNone(got)

    def test_posix_route_get_default_unchanged(self):
        got, m_run = self.run_iface(
            platform="darwin",
            run_return=subprocess.CompletedProcess(
                args=["route"], returncode=0,
                stdout="   route to: default\n    interface: en0\n"))
        self.assertEqual(got, "en0")
        self.assertEqual(m_run.call_args[0][0][:2], ["route", "get"])


class IsVirtualIfaceWinTest(unittest.TestCase):
    def check(self, name, platform):
        with mock.patch.object(sys, "platform", platform):
            return sbw.is_virtual_iface(name)

    def test_win_virtual_prefixes_constant(self):
        self.assertEqual(set(sbw.WIN_VIRTUAL_IFACE_PREFIXES),
                         {"wintun", "mihomo", "clash", "openvpn", "wireguard",
                          "tailscale", "loopback", "vethernet"})

    def test_win32_virtual_ifaces_true(self):
        for name in ("Wintun", "wintun", "Mihomo", "mihomo-tun", "Clash",
                     "clash-tun", "TAP-Windows Adapter V9", "tun0", "OpenVPN TAP",
                     "WireGuard Tunnel", "Tailscale", "Loopback Pseudo-Interface 1",
                     "vEthernet (Default Switch)", "VETHERNET (WSL)"):
            with self.subTest(name=name):
                self.assertTrue(self.check(name, "win32"))

    def test_win32_physical_ifaces_false(self):
        for name in ("Ethernet", "以太网", "WLAN", "本地连接", "Wi-Fi"):
            with self.subTest(name=name):
                self.assertFalse(self.check(name, "win32"))

    def test_win32_case_insensitive(self):
        # posix 下大写 UTUN0 判非虚拟；win32 下接口别名大小写不固定，统一 lower
        self.assertTrue(self.check("UTUN0", "win32"))
        self.assertFalse(self.check("UTUN0", "darwin"))

    def test_none_and_empty_false_both_platforms(self):
        for platform in ("win32", "darwin"):
            with self.subTest(platform=platform):
                self.assertFalse(self.check(None, platform))
                self.assertFalse(self.check("", platform))

    def test_posix_branch_byte_identical_behavior(self):
        # posix：精确小写前缀匹配，posix 集合之外的（wintun 等）不算虚拟
        for name, want in (("utun4", True), ("tap9", True), ("en0", False),
                           ("wintun", False), ("mihomo", False), ("Wintun", False)):
            with self.subTest(name=name):
                self.assertIs(self.check(name, "darwin"), want)


def make_proc(alive=True, wait_side_effect=None):
    """造假测速子进程（与 tests/test_cancel.py 同款）。"""
    proc = mock.MagicMock(name="FakeBenchmarkProc")
    proc.poll.return_value = None if alive else 0
    if wait_side_effect is not None:
        proc.wait.side_effect = wait_side_effect
    else:
        proc.wait.return_value = 0
    return proc


class WebStateCase(unittest.TestCase):
    """快照/恢复 web.STATE 的基座（同 test_cancel.py 的隔离手法）。"""

    def setUp(self):
        with web.STATE_LOCK:
            self._snapshot = dict(web.STATE)
            web.STATE["lines"] = list(web.STATE["lines"])

    def tearDown(self):
        with web.STATE_LOCK:
            web.STATE.clear()
            web.STATE.update(self._snapshot)

    def arm(self, proc, running=True):
        with web.STATE_LOCK:
            web.STATE["running"] = running
            web.STATE["proc"] = proc


class CancelBenchmarkWinTest(WebStateCase):
    """win32：面板无控制台（pythonw），取消改写哨兵文件——测速子进程在
    节点/轮次间隙经 cancel_requested 发现后转 KeyboardInterrupt 优雅退出；
    5s 等待、terminate→kill 兜底不变，不再发 CTRL_BREAK_EVENT。"""

    @contextlib.contextmanager
    def win32_cancel(self, proc):
        with tempfile.TemporaryDirectory() as td:
            sentinel = Path(td) / "cancel-request"
            with mock.patch.object(sys, "platform", "win32"), \
                    mock.patch.object(web, "CANCEL_FILE", sentinel):
                self.arm(proc)
                yield sentinel

    def test_win32_writes_sentinel_no_signal(self):
        proc = make_proc()
        with self.win32_cancel(proc) as sentinel:
            r = web.cancel_benchmark()
            self.assertTrue(r["ok"])
            self.assertTrue(sentinel.exists())
            proc.send_signal.assert_not_called()
            proc.wait.assert_called_once_with(timeout=5)   # 写文件后等 5s
            proc.terminate.assert_not_called()             # 优雅退出后不强杀
            proc.kill.assert_not_called()

    def test_win32_escalates_to_terminate(self):
        proc = make_proc(wait_side_effect=[TIMEOUT, 0])
        with self.win32_cancel(proc) as sentinel:
            r = web.cancel_benchmark()
            self.assertTrue(r["ok"])
            self.assertTrue(sentinel.exists())
            proc.send_signal.assert_not_called()
            proc.terminate.assert_called_once_with()
            proc.kill.assert_not_called()
            self.assertEqual(proc.wait.call_count, 2)

    def test_win32_escalates_to_kill(self):
        proc = make_proc(wait_side_effect=[TIMEOUT, TIMEOUT])
        with self.win32_cancel(proc):
            r = web.cancel_benchmark()
            self.assertTrue(r["ok"])
            proc.send_signal.assert_not_called()
            proc.terminate.assert_called_once_with()
            proc.kill.assert_called_once_with()

    def test_posix_first_signal_is_sigint(self):
        proc = make_proc()
        self.arm(proc)
        with mock.patch.object(sys, "platform", "darwin"):
            r = web.cancel_benchmark()
        self.assertTrue(r["ok"])
        proc.send_signal.assert_called_once_with(signal.SIGINT)


class BenchmarkPopenFlagsTest(WebStateCase):
    """run_benchmark 的 Popen：win32 加 CREATE_NO_WINDOW，posix 不加；
    两平台 stdin=DEVNULL、env 带 SPEEDBENCH_CANCEL_FILE 哨兵路径。"""

    FAKE_NO_WINDOW = 0x08000000

    def run_web(self, platform):
        proc = make_proc()
        proc.stdout = []          # 无输出，直接结束
        proc.wait.return_value = 0
        with mock.patch.object(sys, "platform", platform), \
                mock.patch.object(web.subprocess, "Popen",
                                  return_value=proc) as m_popen, \
                mock.patch.object(web, "sync_db"), \
                mock.patch.object(subprocess, "CREATE_NO_WINDOW",
                                  self.FAKE_NO_WINDOW, create=True), \
                contextlib.redirect_stdout(io.StringIO()):
            web.run_benchmark({})
        return m_popen

    def test_win32_sets_create_no_window(self):
        m_popen = self.run_web("win32")
        kwargs = m_popen.call_args.kwargs
        self.assertEqual(kwargs.get("creationflags"), self.FAKE_NO_WINDOW)

    def test_posix_no_creationflags(self):
        m_popen = self.run_web("darwin")
        self.assertNotIn("creationflags", m_popen.call_args.kwargs)

    def test_stdin_devnull_and_cancel_env_both_platforms(self):
        # stdin=DEVNULL：pythonw 的 stdin 句柄无效，子进程不能继承；
        # env 带哨兵文件路径：子进程 cancel_requested 靠它定位哨兵
        for platform in ("win32", "darwin"):
            with self.subTest(platform=platform):
                kwargs = self.run_web(platform).call_args.kwargs
                self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
                self.assertEqual(kwargs["env"].get("SPEEDBENCH_CANCEL_FILE"),
                                 str(web.CANCEL_FILE))

    def test_baseline_popen_kwargs_both_platforms(self):
        # 既有参数两平台都不变：-u 无缓冲、输出走管道、面板数据目录作 cwd
        for platform in ("win32", "darwin"):
            with self.subTest(platform=platform):
                m_popen = self.run_web(platform)
                cmd, kwargs = m_popen.call_args.args[0], m_popen.call_args.kwargs
                self.assertEqual(cmd[0], sys.executable)
                self.assertIn("-u", cmd)
                self.assertIn("--yes", cmd)
                self.assertEqual(kwargs["stdout"], subprocess.PIPE)
                self.assertEqual(kwargs["stderr"], subprocess.STDOUT)


class NoWindowKwargsTest(unittest.TestCase):
    FAKE_FLAG = 0x08000000

    def test_win32_with_constant(self):
        with mock.patch.object(sys, "platform", "win32"), \
                mock.patch.object(subprocess, "CREATE_NO_WINDOW",
                                  self.FAKE_FLAG, create=True):
            self.assertEqual(csb._no_window_kwargs(),
                             {"creationflags": self.FAKE_FLAG})

    def test_win32_without_constant(self):
        # 常量缺失/为 0 时退回空 dict（getattr 默认值路径）
        with mock.patch.object(sys, "platform", "win32"), \
                mock.patch.object(subprocess, "CREATE_NO_WINDOW", 0, create=True):
            self.assertEqual(csb._no_window_kwargs(), {})

    def test_posix_always_empty(self):
        # 即使环境里混入了该常量，posix 也恒返回空 dict
        with mock.patch.object(sys, "platform", "darwin"), \
                mock.patch.object(subprocess, "CREATE_NO_WINDOW",
                                  self.FAKE_FLAG, create=True):
            self.assertEqual(csb._no_window_kwargs(), {})

    def test_curl_speed_applies_creationflags_on_win32(self):
        out = "50000000\t1.0\t8000000\t200\t0.05\t0.2\t0.9"
        proc = subprocess.CompletedProcess(args=["curl"], returncode=0,
                                           stdout=out, stderr="")
        with mock.patch.object(sys, "platform", "win32"), \
                mock.patch.object(subprocess, "CREATE_NO_WINDOW",
                                  self.FAKE_FLAG, create=True), \
                mock.patch.object(csb.subprocess, "run", return_value=proc) as m_run:
            csb.curl_speed("http://127.0.0.1:7897", "https://example.com/x",
                           max_time=4.0, connect_timeout=3.0)
        self.assertEqual(m_run.call_args.kwargs.get("creationflags"), self.FAKE_FLAG)

    def test_curl_speed_no_creationflags_on_posix(self):
        out = "50000000\t1.0\t8000000\t200\t0.05\t0.2\t0.9"
        proc = subprocess.CompletedProcess(args=["curl"], returncode=0,
                                           stdout=out, stderr="")
        with mock.patch.object(sys, "platform", "darwin"), \
                mock.patch.object(csb.subprocess, "run", return_value=proc) as m_run:
            csb.curl_speed("http://127.0.0.1:7897", "https://example.com/x",
                           max_time=4.0, connect_timeout=3.0)
        self.assertNotIn("creationflags", m_run.call_args.kwargs)


class SigbreakRegistrationTest(unittest.TestCase):
    """main() 在 win32 给 SIGBREAK 注册 KeyboardInterrupt 转换 handler。

    macOS 上 signal 没有 SIGBREAK：patch 一个真实可用的信号号进去
    （Windows 的 SIGBREAK 编号恰好是 21，posix 上 21=SIGTTIN，可注册可恢复），
    测试结束 finally 恢复原 handler，进程信号状态不留痕。
    """

    FAKE_SIGBREAK = getattr(signal, "SIGBREAK", 21)

    def run_main(self, platform):
        with mock.patch.object(sys, "platform", platform), \
                mock.patch.object(signal, "SIGBREAK", self.FAKE_SIGBREAK,
                                  create=True), \
                mock.patch.object(sys, "argv", ["clash_speedbench.py", "--yes"]), \
                mock.patch.object(csb, "detect_controller",
                                  side_effect=csb.ApiError("停止于 controller 探测")), \
                contextlib.redirect_stderr(io.StringIO()):
            return csb.main()

    def test_win32_registers_keyboard_interrupt_handler(self):
        old_handler = signal.getsignal(self.FAKE_SIGBREAK)
        try:
            rc = self.run_main("win32")
            self.assertEqual(rc, 1)  # detect_controller 失败路径，注册已发生
            handler = signal.getsignal(self.FAKE_SIGBREAK)
            self.assertTrue(callable(handler))
            self.assertNotIn(handler, (signal.SIG_DFL, signal.SIG_IGN,
                                       signal.default_int_handler))
            # handler 语义：CTRL_BREAK_EVENT → KeyboardInterrupt（与 Ctrl+C 同路径）
            with self.assertRaises(KeyboardInterrupt):
                handler(self.FAKE_SIGBREAK, None)
        finally:
            signal.signal(self.FAKE_SIGBREAK, old_handler)
        self.assertIs(signal.getsignal(self.FAKE_SIGBREAK), old_handler)

    def test_posix_does_not_touch_sigbreak(self):
        with mock.patch.object(csb.signal, "signal") as m_signal:
            rc = self.run_main("darwin")
        self.assertEqual(rc, 1)
        m_signal.assert_not_called()  # posix 不注册任何自定义 handler


class PipeControllerTest(unittest.TestCase):
    """pipe:// 命名管道 controller：Windows 版 Clash Verge Rev 的默认（服务模式时
    甚至是唯一）API 通道。真机验收实测：生成的运行配置里 external-controller 为空，
    9097/9090 均不监听，只有 \\\\.\\pipe\\verge-mihomo 可用。

    传输层（CreateFileW/NtCreateFile/ReadFile/WriteFile）只在真实 Windows 上执行；
    这里借 reload_with_platform 让 win32 分支代码定义出来，再 patch 四个
    plumbing 函数喂假报文，验证 scheme 解析与 HTTP 往返逻辑（含 mihomo 实际
    使用的 chunked 编码）。
    """

    CANNED = (
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
        b"Content-Length: 22\r\nConnection: close\r\n\r\n"
        b'{"version":"v1.19.29"}'
    )

    def _run_over_fake_pipe(self, canned: bytes):
        """在假 plumbing 上跑一次完整 HTTP 往返，返回 (API 返回值, 写出的请求字节)。"""
        with reload_with_platform(csb, "win32"):
            writes = []
            reads = [canned[i:i + 4096] for i in range(0, len(canned), 4096)] + [b""]

            def fake_read(_handle, size):
                if not reads:
                    return b""
                return reads.pop(0)[:size]

            with mock.patch.object(csb, "_open_pipe_handle", return_value=123) as m_open, \
                    mock.patch.object(csb, "_pipe_write_all",
                                      side_effect=lambda _h, d: writes.append(bytes(d))), \
                    mock.patch.object(csb, "_pipe_read", side_effect=fake_read), \
                    mock.patch.object(csb, "_pipe_close") as m_close:
                result = csb.MihomoAPI("pipe://verge-mihomo", timeout=5.0).get("/version")
            m_open.assert_called_once_with("verge-mihomo")
            m_close.assert_called_once_with(123)  # 句柄只被关一次（fp/sock 不双关）
        return result, writes

    def test_pipe_scheme_parsed(self):
        api = csb.MihomoAPI("pipe://verge-mihomo")
        self.assertEqual(api.pipe_name, "verge-mihomo")
        self.assertIsNone(api.unix_path)
        self.assertEqual(api.base, "http://localhost")

    def test_pipe_transport_round_trip(self):
        result, writes = self._run_over_fake_pipe(self.CANNED)
        self.assertEqual(result, {"version": "v1.19.29"})
        self.assertIn(b"GET /version HTTP/1.1", b"".join(writes))

    def test_pipe_transport_chunked_encoding(self):
        canned = (
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            b"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
            b"5\r\n{\"a\":\r\n2\r\n1}\r\n0\r\n\r\n"
        )
        result, _ = self._run_over_fake_pipe(canned)
        self.assertEqual(result, {"a": 1})

    def test_pipe_scheme_on_posix_raises_clear_error(self):
        with mock.patch.object(sys, "platform", "darwin"):
            api = csb.MihomoAPI("pipe://verge-mihomo")
            with self.assertRaises(csb.ApiError) as cm:
                api.get("/version")
        self.assertIn("仅支持 Windows", str(cm.exception))

    def test_pipesock_close_idempotent_and_rb_only(self):
        with reload_with_platform(csb, "win32"):
            with mock.patch.object(csb, "_pipe_close") as m_close:
                sock = csb._PipeSock(456)
                with self.assertRaises(ValueError):
                    sock.makefile("wb")
                sock.close()
                sock.close()
            m_close.assert_called_once_with(456)


class SubprocessEncodingTest(unittest.TestCase):
    """子进程文本输出必须钉 encoding="utf-8" + errors="replace"：中文 Windows
    默认按 GBK 解码，遇到 UTF-8/非 GBK 字节（ip-api 的中文地名、curl 报错里
    的服务端字节等）会在 subprocess 读取线程里炸 UnicodeDecodeError，表现为
    IP 画像列空、日志刷异常（v0.8.0 真机验收实测复现）。"""

    @staticmethod
    def _last_run_kwargs(module, func, *args):
        proc = subprocess.CompletedProcess(args=["curl"], returncode=0,
                                           stdout="x", stderr="")
        with mock.patch.object(module.subprocess, "run", return_value=proc) as m:
            try:
                func(*args)
            except Exception:
                pass  # 只关心 subprocess.run 的调用参数，不关心后续解析
        return m.call_args.kwargs

    def test_curl_speed_pins_utf8(self):
        kw = self._last_run_kwargs(csb, csb.curl_speed, "http://127.0.0.1:7897",
                                   "https://example.com/x", 4.0, 3.0)
        self.assertEqual(kw.get("encoding"), "utf-8")
        self.assertEqual(kw.get("errors"), "replace")

    def test_fetch_ip_info_pins_utf8(self):
        kw = self._last_run_kwargs(csb, csb.fetch_ip_info,
                                   "http://127.0.0.1:7897", 8.0)
        self.assertEqual(kw.get("encoding"), "utf-8")
        self.assertEqual(kw.get("errors"), "replace")

    def test_doh_resolve_pins_utf8(self):
        kw = self._last_run_kwargs(sbw, sbw.doh_resolve, "example.com")
        self.assertEqual(kw.get("encoding"), "utf-8")
        self.assertEqual(kw.get("errors"), "replace")


class SpeedBenchBatTest(unittest.TestCase):
    """SpeedBench.bat 启动器回归保护。

    v0.8.0 Windows 真机验收实测：UTF-8 编码 + 中文注释的 .bat 在中文 Windows
    上会被 cmd.exe 错乱解析（chcp 65001 提前到文件最前也无效），中文注释/回显
    的碎片被当成命令执行（“不是内部或外部命令”刷屏）；改成纯 ASCII 后彻底消失。
    因此启动器必须保持纯 ASCII、无 BOM、CRLF 行尾。
    """

    BAT = Path(__file__).resolve().parents[1] / "SpeedBench.bat"

    def test_bat_is_pure_ascii_no_bom(self):
        data = self.BAT.read_bytes()
        self.assertFalse(data.startswith(b"\xef\xbb\xbf"),
                         "SpeedBench.bat 不允许带 BOM")
        try:
            data.decode("ascii")
        except UnicodeDecodeError as e:
            self.fail(f"SpeedBench.bat 偏移 {e.start} 处出现非 ASCII 字节："
                      "cmd.exe 会错乱解析含中文的 .bat（真机实测），启动器必须纯 ASCII")

    def test_bat_crlf_line_endings(self):
        data = self.BAT.read_bytes()
        self.assertIn(b"\r\n", data)
        self.assertNotIn(b"\n", data.replace(b"\r\n", b""), "存在孤立的 LF 行尾")

    def test_bat_prefers_pythonw_keeps_python_fallback(self):
        # 防手滑改掉启动方式：面板优先 pythonw 无窗口启动（取消走哨兵文件，
        # 不再依赖控制台），pythonw 缺失时保留 python 最小化控制台兜底
        text = self.BAT.read_bytes().decode("ascii")
        self.assertIn('pythonw "%~dp0speedbench_web.py"', text)
        self.assertIn('python "%~dp0speedbench_web.py"', text)


class TrayModuleTest(unittest.TestCase):
    """进程内托盘（speedbench_tray.py，ctypes/Win32）的跨平台静态约束。

    Win32 窗口/消息循环本身靠真机验证；这里守：
    - posix 上 start_tray/stop_tray 是纯 no-op（模块 import 不炸、行为不变）
    - web main() 的接线：启动时调 start_tray、退出时 finally 调 stop_tray
    - speedbench.ico 结构合法；release.yml 的 Windows 打包清单收录托盘模块与图标
    """

    ROOT = Path(__file__).resolve().parents[1]

    def test_start_tray_noop_on_posix(self):
        import speedbench_tray
        with mock.patch.object(sys, "platform", "darwin"):
            self.assertIsNone(speedbench_tray.start_tray("x.ico", lambda: None, lambda: None))
            speedbench_tray.stop_tray(None)  # 不应抛异常

    def test_web_main_starts_and_stops_tray(self):
        import speedbench_tray

        class FakeServer:
            server_port = 8950

            def serve_forever(self):
                return None

            def shutdown(self):
                return None

        with mock.patch.object(web, "ThreadingHTTPServer", return_value=FakeServer()), \
                mock.patch.object(web, "sync_db", return_value=0), \
                mock.patch.object(web, "write_token_file"), \
                mock.patch.object(web.webbrowser, "open"), \
                mock.patch.object(sys, "argv", ["speedbench_web.py", "--no-browser"]), \
                mock.patch.object(speedbench_tray, "start_tray", return_value=object()) as m_start, \
                mock.patch.object(speedbench_tray, "stop_tray") as m_stop:
            self.assertEqual(web.main(), 0)
        m_start.assert_called_once()          # 接线：图标路径 + on_open/on_quit 回调
        _, kwargs = m_start.call_args
        self.assertTrue(callable(kwargs["on_open"]) and callable(kwargs["on_quit"]))
        m_stop.assert_called_once()           # finally 摘图标，不留僵尸图标

    def test_icon_is_valid_ico(self):
        import struct
        data = (self.ROOT / "speedbench.ico").read_bytes()
        reserved, itype, count = struct.unpack_from("<HHH", data, 0)
        self.assertEqual((reserved, itype), (0, 1), "ICO 头 reserved/type 不对")
        self.assertGreaterEqual(count, 1)
        for i in range(count):  # 每个条目声明的数据区间都必须落在文件内
            _, _, _, _, _, _, size, offset = struct.unpack_from("<BBBBHHII", data, 6 + 16 * i)
            self.assertLessEqual(offset + size, len(data))

    def test_release_packages_tray_files(self):
        yml = (self.ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        for fname in ("speedbench_tray.py", "speedbench.ico"):
            self.assertIn(fname, yml, f"release.yml 的 Windows 打包清单缺少 {fname}")
        sh = (self.ROOT / "build_app.sh").read_text(encoding="utf-8")
        self.assertIn("speedbench_tray.py", sh,
                      "build_app.sh 的 macOS 打包清单缺少 speedbench_tray.py")

    def test_release_packages_ip_intelligence_and_leak_modules(self):
        """发布包必须带上 v1.0 的新增运行时模块。

        这两个文件分别被 CLI/Web 面板导入；漏拷贝会让源码运行正常、
        但 macOS App 或 Windows zip 在用户机器上启动即失败。因此这里
        同时钉住 macOS copy/完整性校验、Windows Copy-Item 和 CI AST 清单。
        """
        modules = ("speedbench_ip_intel.py", "speedbench_leak.py")
        release = (self.ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8")
        build = (self.ROOT / "build_app.sh").read_text(encoding="utf-8")
        ci = (self.ROOT / ".github" / "workflows" / "test.yml").read_text(
            encoding="utf-8")
        for module in modules:
            self.assertIn(module, release,
                          f"release.yml 的 Windows 打包清单缺少 {module}")
            self.assertIn(module, build,
                          f"build_app.sh 的 macOS 打包清单缺少 {module}")
            self.assertIn(module, ci,
                          f"test.yml 的 AST 语法清单缺少 {module}")

        # 这些是当前发行包的运行时 Python 闭包；静态检查防止将新增模块
        # 加进注释/测试说明，却没有真正加入 copy 命令。
        win_copy = release[release.index("Copy-Item clash_speedbench.py"):]
        for module in modules:
            self.assertIn(module, win_copy)
        mac_copy = build[build.index('cp "$ROOT/clash_speedbench.py"'):]
        for module in modules:
            self.assertIn(module, mac_copy)


if __name__ == "__main__":
    unittest.main()
