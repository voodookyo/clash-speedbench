#!/bin/bash
# Clash SpeedBench 一键启动器（macOS 双击运行）
# 双击后选择：1=终端全量测速  2=Web 面板
# 终端用法示例： ./speedbench.command --include '香港|HK' --mb 15

SPEEDTEST_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SPEEDTEST_DIR" || exit 1

# 带参数时直接跑测速，不出菜单
if [ $# -gt 0 ]; then
    python3 clash_speedbench.py --yes "$@"
    echo
    read -n 1 -s -r -p "按任意键关闭窗口..."
    echo
    exit 0
fi

echo "⚡ Clash SpeedBench"
echo
echo "  1) 终端全量测速"
echo "  2) 打开 Web 面板（http://127.0.0.1:8950）"
echo "  3) 测速并自动切换到冠军节点"
echo
read -n 1 -s -r -p "请选择 [1/2/3]: " choice
echo
echo

case "$choice" in
    2)
        echo "启动 Web 面板...（关闭此窗口即停止）"
        python3 speedbench_web.py
        ;;
    3)
        python3 clash_speedbench.py --yes --auto-switch
        ;;
    *)
        python3 clash_speedbench.py --yes
        ;;
esac

echo
echo "CSV 报告与历史记录保存在 $SPEEDTEST_DIR"
read -n 1 -s -r -p "按任意键关闭窗口..."
echo
