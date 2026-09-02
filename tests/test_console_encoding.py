# -*- coding: utf-8 -*-
"""控制台编码回归测试：GBK/cp1252 控制台无法编码节点名 emoji 时不得崩溃。

v1.0.1 实测：中文 Windows 控制台（GBK）下，print 含 🇭🇰 区域指示符的节点名
抛 UnicodeEncodeError，测速中断。修复方式与 Web 面板一致：
main() 入口把 stdout/stderr 的 errors 钉成 replace。
"""
import io
import sys
import unittest
from unittest import mock

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import clash_speedbench as csb


class ReconfigureStdioTest(unittest.TestCase):
    def _gbk_stream(self):
        # 模拟中文 Windows 控制台：GBK 编码、严格错误处理
        return io.TextIOWrapper(io.BytesIO(), encoding="gbk", errors="strict")

    def test_emoji_node_name_does_not_crash_gbk_console(self):
        fake_out, fake_err = self._gbk_stream(), self._gbk_stream()
        with mock.patch.object(sys, "stdout", fake_out), \
                mock.patch.object(sys, "stderr", fake_err):
            csb._reconfigure_stdio_for_console()
            # 与主循环相同形态的输出：序号 + 带国旗 emoji 的节点名 + 警告符号
            print("[  1/25] 🇭🇰 香港 01 | 42 ms | ⚠️ 恢复原模式失败",
                  file=sys.stdout)
            print("⚠️ 警告", file=sys.stderr)
            fake_out.flush()
            fake_err.flush()
        text = fake_out.buffer.getvalue().decode("gbk")
        self.assertIn("香港", text)          # GBK 能表示的中文不受影响
        self.assertIn("?", text)             # emoji 退化为 ? 而不是抛异常

    def test_non_textiowrapper_streams_are_skipped(self):
        class OddStream:  # 没有 reconfigure，模拟嵌入式/自定义捕获环境
            def write(self, s):
                return len(s)

            def flush(self):
                pass

        with mock.patch.object(sys, "stdout", OddStream()), \
                mock.patch.object(sys, "stderr", OddStream()):
            csb._reconfigure_stdio_for_console()  # 不应抛任何异常

    def test_main_reconfigures_before_any_output(self):
        # main() 必须在 argparse 之前完成 reconfigure（argparse 自身也会
        # 打印中文帮助/错误，同样可能踩编码）。用源码顺序锁定这一约定。
        src = (Path(csb.__file__)).read_text(encoding="utf-8")
        main_body = src.split("def main() -> int:", 1)[1]
        self.assertLess(main_body.index("_reconfigure_stdio_for_console()"),
                        main_body.index("argparse.ArgumentParser"))


if __name__ == "__main__":
    unittest.main()
