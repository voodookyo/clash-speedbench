#!/bin/bash
# 退出 SwiftBar（菜单栏图标本体属于 SwiftBar，退出它图标才消失）。
set -u
/usr/bin/osascript -e 'tell application "SwiftBar" to quit' >/dev/null 2>&1 || true
