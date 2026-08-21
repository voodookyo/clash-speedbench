# -*- coding: utf-8 -*-
"""共享测试基座：在 127.0.0.1 随机空闲端口上，用线程起一个真实的
speedbench_web 面板服务器（ThreadingHTTPServer），供 HTTP 级测试使用。

- 每个用例独立 server / 独立端口，tearDown 负责关闭；
- setUp/tearDown 快照并恢复模块级 STATE，避免用例间串扰；
- request()/post_json() 基于 http.client，只打 127.0.0.1 本机端口。

文件名故意不以 test 开头：unittest discover 不会把它当用例模块收集。
"""
import http.client
import json
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import speedbench_web as web

# 面板写操作令牌（模块级随机生成），测试里直接引用真值
TOKEN = web.WEB_TOKEN


class WebServerCase(unittest.TestCase):
    """每个用例一个独立端口的面板服务器；子类直接写 test_ 方法即可。"""

    def setUp(self):
        # 浅快照 + lines 换成副本：running/started/exit_code/proc 都是整体替换，
        # 只有 lines 会被原地 append，必须隔离
        with web.STATE_LOCK:
            self._state_snapshot = dict(web.STATE)
            web.STATE["lines"] = list(web.STATE["lines"])
        self.server = web.ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05},
            daemon=True)
        self.thread.start()

    def tearDown(self):
        try:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=5)
        finally:
            with web.STATE_LOCK:
                web.STATE.clear()
                web.STATE.update(self._state_snapshot)

    def set_state(self, **kw):
        with web.STATE_LOCK:
            web.STATE.update(kw)

    def request(self, method, path, body=None, headers=None):
        """发一次 HTTP 请求，返回 (status, raw_body bytes)。"""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    def post_json(self, path, obj=None, headers=None):
        h = {"Content-Type": "application/json"}
        h.update(headers or {})
        return self.request("POST", path,
                            body=json.dumps(obj if obj is not None else {}),
                            headers=h)

    def post_authorized(self, path, obj=None, headers=None):
        """携带合法令牌的 POST（Host 由 http.client 自动填 127.0.0.1:port）。"""
        h = {"X-SpeedBench-Token": TOKEN}
        h.update(headers or {})
        return self.post_json(path, obj, headers=h)
