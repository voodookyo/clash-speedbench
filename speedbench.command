#!/bin/bash
# Clash SpeedBench 一键启动器（macOS 双击运行）
# 把它放到仓库目录里，双击即可全量测速；也可以拖一份到桌面或 Dock。
# 终端用法示例： ./speedbench.command --include '香港|HK' --mb 15

SPEEDTEST_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SPEEDTEST_DIR" || exit 1

python3 clash_speedbench.py --yes "$@"

echo
echo "测速结束，CSV 报告已保存在 $SPEEDTEST_DIR"
read -n 1 -s -r -p "按任意键关闭窗口..."
echo
