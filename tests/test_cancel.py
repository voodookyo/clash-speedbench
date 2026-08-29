# -*- coding: utf-8 -*-
"""cancel_benchmark 状态机 + /api/quit 先中断再关停 的测试。

- 测速子进程一律用 Mock：断言 SIGINT 优先、5s 等待、terminate → kill 兜底、
  已退出不重复发信号；
- /api/quit 路径用真实 HTTP server（127.0.0.1 随机端口）+ mock cancel_benchmark
  验证调用编排，不起真测速进程。
"""
import json
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import clash_speedbench as csb
import speedbench_web as web
from tests.web_server_case import WebServerCase


def make_proc(alive=True, wait_side_effect=None):
    """造一个假测速子进程。alive=False 表示 poll() 返回已退出。"""
    proc = mock.MagicMock(name="FakeBenchmarkProc")
    proc.poll.return_value = None if alive else 0
    if wait_side_effect is not None:
        proc.wait.side_effect = wait_side_effect
    else:
        proc.wait.return_value = 0
    return proc


TIMEOUT = subprocess.TimeoutExpired(cmd="clash_speedbench.py", timeout=5)


class CancelStateMachineTest(unittest.TestCase):
    """直接调 cancel_benchmark()，不开 server。"""

    def setUp(self):
        with web.STATE_LOCK:
            self._snapshot = dict(web.STATE)
            web.STATE["lines"] = list(web.STATE["lines"])  # 副本供测试污染

    def tearDown(self):
        with web.STATE_LOCK:
            web.STATE.clear()
            web.STATE.update(self._snapshot)

    def arm(self, proc, running=True):
        with web.STATE_LOCK:
            web.STATE["running"] = running
            web.STATE["proc"] = proc

    def test_no_task_returns_not_ok(self):
        self.arm(None, running=False)
        r = web.cancel_benchmark()
        self.assertFalse(r["ok"])
        self.assertIn("没有正在进行的测速", r["msg"])

    def test_running_but_proc_missing_returns_not_ok(self):
        self.arm(None, running=True)
        r = web.cancel_benchmark()
        self.assertFalse(r["ok"])

    def test_proc_already_exited_not_signalled(self):
        proc = make_proc(alive=False)
        self.arm(proc)
        r = web.cancel_benchmark()
        self.assertFalse(r["ok"])
        proc.send_signal.assert_not_called()
        proc.terminate.assert_not_called()
        proc.kill.assert_not_called()

    def test_sigint_preferred_no_force_when_process_exits(self):
        proc = make_proc()
        self.arm(proc)
        # 钉住 posix 平台：win32 分支改写哨兵文件（见 tests/test_windows.py），
        # 本用例只验证 posix 的 SIGINT 优先语义
        with mock.patch.object(sys, "platform", "darwin"):
            r = web.cancel_benchmark()
        self.assertTrue(r["ok"])
        proc.send_signal.assert_called_once_with(signal.SIGINT)
        proc.wait.assert_called_once_with(timeout=5)
        proc.terminate.assert_not_called()
        proc.kill.assert_not_called()
        self.assertIn("!! 测速已被手动中断", web.STATE["lines"])

    def test_sigint_timeout_escalates_to_terminate(self):
        proc = make_proc(wait_side_effect=[TIMEOUT, 0])
        self.arm(proc)
        with mock.patch.object(sys, "platform", "darwin"):  # 钉住 posix 分支
            r = web.cancel_benchmark()
        self.assertTrue(r["ok"])
        proc.send_signal.assert_called_once_with(signal.SIGINT)
        proc.terminate.assert_called_once_with()
        proc.kill.assert_not_called()
        self.assertEqual(proc.wait.call_count, 2)

    def test_terminate_timeout_escalates_to_kill(self):
        proc = make_proc(wait_side_effect=[TIMEOUT, TIMEOUT])
        self.arm(proc)
        with tempfile.TemporaryDirectory() as td:
            sentinel = Path(td) / "cancel-request"
            with mock.patch.object(web, "CANCEL_FILE", sentinel):
                r = web.cancel_benchmark()
            self.assertTrue(r["ok"])
            if sys.platform == "win32":
                self.assertTrue(sentinel.exists())
            else:
                self.assertFalse(sentinel.exists())
            self.assertEqual(sentinel.parent, Path(td))
            proc.terminate.assert_called_once_with()
            proc.kill.assert_called_once_with()
        self.assertFalse(sentinel.exists())

    def test_send_signal_failure_returns_not_ok(self):
        proc = make_proc()
        proc.send_signal.side_effect = OSError("No such process")
        self.arm(proc)
        # 钉住 posix 分支：真 Windows 上 win32 分支改写哨兵文件、不发信号
        with mock.patch.object(sys, "platform", "darwin"):
            r = web.cancel_benchmark()
        self.assertFalse(r["ok"])
        self.assertIn("中断失败", r["msg"])
        proc.terminate.assert_not_called()
        self.assertNotIn("!! 测速已被手动中断", web.STATE["lines"])


class CancelSentinelFileTest(unittest.TestCase):
    """测速子进程侧的哨兵文件检测（clash_speedbench.cancel_requested）。

    面板无控制台（pythonw）后 CTRL_BREAK_EVENT 无处可投，Windows 取消改走
    SPEEDBENCH_CANCEL_FILE 环境变量指向的哨兵文件；这里守住检测与
    启动时清理残留哨兵的行为。
    """

    def test_no_env_never_cancelled(self):
        with mock.patch.object(csb, "_CANCEL_FILE", ""):
            self.assertFalse(csb.cancel_requested())
            csb.clear_cancel_request()  # 无环境变量时是纯 no-op，不报错

    def test_file_presence_toggles(self):
        with tempfile.TemporaryDirectory() as td:
            sentinel = Path(td) / "cancel-request"
            with mock.patch.object(csb, "_CANCEL_FILE", str(sentinel)):
                self.assertFalse(csb.cancel_requested())
                sentinel.write_text("1", encoding="utf-8")
                self.assertTrue(csb.cancel_requested())

    def test_clear_removes_stale_sentinel(self):
        with tempfile.TemporaryDirectory() as td:
            sentinel = Path(td) / "cancel-request"
            sentinel.write_text("stale", encoding="utf-8")
            with mock.patch.object(csb, "_CANCEL_FILE", str(sentinel)):
                csb.clear_cancel_request()
                self.assertFalse(csb.cancel_requested())
            self.assertFalse(sentinel.exists())
            # 文件本就不存在时清理不报错（幂等）
            with mock.patch.object(csb, "_CANCEL_FILE", str(sentinel)):
                csb.clear_cancel_request()


class CancelEndpointTest(WebServerCase):
    """HTTP 层：/api/run/cancel 与 /api/quit 的编排（真 server，假子进程）。"""

    def test_cancel_endpoint_sigints_running_proc(self):
        proc = make_proc()
        self.set_state(running=True, proc=proc)
        with mock.patch.object(sys, "platform", "darwin"):  # 钉住 posix 分支
            status, body = self.post_authorized("/api/run/cancel", {})
        data = json.loads(body)
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        proc.send_signal.assert_called_once_with(signal.SIGINT)

    def test_cancel_endpoint_no_task(self):
        status, body = self.post_authorized("/api/run/cancel", {})
        data = json.loads(body)
        self.assertEqual(status, 200)
        self.assertFalse(data["ok"])

    def test_quit_cancels_running_benchmark_first(self):
        proc = make_proc()
        self.set_state(running=True, proc=proc)
        with mock.patch.object(web, "cancel_benchmark",
                               return_value={"ok": True, "msg": "已中断"}) as cancel_mock:
            status, body = self.post_authorized("/api/quit", {})
        data = json.loads(body)
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertIn("已先中断", data["msg"])
        cancel_mock.assert_called_once_with()
        # cancel 被 mock 掉，真子进程不应收到任何信号
        proc.send_signal.assert_not_called()

    def test_quit_idle_skips_cancel(self):
        with mock.patch.object(web, "cancel_benchmark") as cancel_mock:
            status, body = self.post_authorized("/api/quit", {})
        data = json.loads(body)
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["msg"], "面板已停止")
        cancel_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
