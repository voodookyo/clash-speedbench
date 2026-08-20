#!/bin/bash
# Clash SpeedBench Web 面板启动器（macOS 双击运行）
SPEEDTEST_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SPEEDTEST_DIR" || exit 1

echo "启动 Clash SpeedBench Web 面板...（浏览器会自动打开 http://127.0.0.1:8950）"
echo "关闭此窗口即停止面板。"
python3 speedbench_web.py
