#!/bin/bash
# 停止本地 Web 面板（供 SwiftBar 菜单栏插件调用）。
# 面板每次启动把随机写操作 token 写进数据目录 web-token（0600 仅本人可读），
# 这里读出来调 /api/quit；面板没在跑或 token 失效时静默退出（SwiftBar 会刷新菜单）。
set -u

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TOKEN="$(cat "$HOME/Library/Application Support/ClashSpeedBench/web-token" 2>/dev/null \
       || cat "$REPO_DIR/web-token" 2>/dev/null || true)"
[ -n "$TOKEN" ] || exit 0
/usr/bin/curl -s -X POST -H "X-SpeedBench-Token: $TOKEN" \
    "http://127.0.0.1:${SPEEDBENCH_PORT:-8950}/api/quit" >/dev/null 2>&1 || true
