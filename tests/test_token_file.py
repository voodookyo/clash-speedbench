"""web-token 文件：面板启动时把 WEB_TOKEN 写入数据目录（0600），供 SwiftBar 等本机脚本调写 API。"""

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import speedbench_web as web


class TokenFileTest(unittest.TestCase):
    def test_writes_token_with_0600(self):
        if os.name == "nt":
            self.skipTest("Windows 无 POSIX 权限位语义，os.chmod 0600 不适用")
        with tempfile.TemporaryDirectory() as td:
            tf = Path(td) / "web-token"
            with mock.patch.object(web, "TOKEN_FILE", tf):
                web.write_token_file()
            self.assertEqual(tf.read_text(encoding="utf-8"), web.WEB_TOKEN)
            mode = stat.S_IMODE(os.stat(tf).st_mode)
            self.assertEqual(mode, 0o600, f"权限应为 0600，实际 {oct(mode)}")

    def test_overwrites_on_each_launch(self):
        with tempfile.TemporaryDirectory() as td:
            tf = Path(td) / "web-token"
            tf.write_text("stale-token")
            with mock.patch.object(web, "TOKEN_FILE", tf):
                web.write_token_file()
            self.assertEqual(tf.read_text(encoding="utf-8"), web.WEB_TOKEN)

    def test_unwritable_dir_is_silent(self):
        with mock.patch.object(web, "TOKEN_FILE", Path("/nonexistent-dir-x/web-token")):
            web.write_token_file()  # 不抛异常


if __name__ == "__main__":
    unittest.main()
