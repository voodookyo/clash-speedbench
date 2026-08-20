#!/bin/bash
# <swiftbar.hideAbout>true</swiftbar.hideAbout>
# <swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>
# <swiftbar.hideLastUpdated>true</swiftbar.hideLastUpdated>
# <swiftbar.hideDisablePlugin>true</swiftbar.hideDisablePlugin>
# <swiftbar.hideSwiftBar>true</swiftbar.hideSwiftBar>

# Clash SpeedBench 菜单栏插件（SwiftBar）
# 菜单栏显示上次测速的冠军节点，下拉可切换节点 / 发起测速 / 打开 Web 面板。
#
# 解析规则（来自 SwiftBar 源码 MenuLineParameters）：
#   - 每行按第一个 ASCII "|" 分隔标题与参数，标题里绝不能出现 ASCII "|"
#     （节点名里的 " | " 一律替换为全角 "｜" 再显示）；
#   - 参数值用双引号包裹，引号内出现 "|" 是安全的；
#   - terminal 默认为 true，所有 shell 动作必须显式 terminal=false。
# 所有路径必须是绝对路径（SwiftBar 不保证 cwd，PATH 也不含 homebrew）。

set -u

REPO_DIR="/Users/admin/Documents/Kimiwork/clash-speedbench"
HISTORY="${SPEEDBENCH_HISTORY:-$REPO_DIR/speedbench-history.jsonl}"
SWITCH_PY="$REPO_DIR/speedbench_switch.py"
WEB_PY="$REPO_DIR/speedbench_web.py"
COMMAND_FILE="$REPO_DIR/speedbench.command"
WEB_PORT="8950"
PYTHON3="$(command -v python3 || echo /usr/bin/python3)"

# ---- 动态部分：标题 + 上次测速时间 + Top 5 节点（python3 解析 JSONL）----
# 任何异常都输出兜底标题，保证插件被反复执行时不报错刷屏。
HISTORY="$HISTORY" PYTHON3="$PYTHON3" SWITCH_PY="$SWITCH_PY" python3 - <<'PYEOF'
import json
import os

history = os.environ["HISTORY"]
pybin = os.environ["PYTHON3"]
switch_py = os.environ["SWITCH_PY"]


def disp(name):
    # 标题里不能有 ASCII "|"（会被 SwiftBar 当成参数分隔符），换成全角
    return name.replace("|", "｜")


def build():
    record = None
    try:
        last = ""
        with open(history, encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if raw:
                    last = raw
        if last:
            record = json.loads(last)
    except (OSError, json.JSONDecodeError):
        record = None

    results = (record or {}).get("results") or []
    if not results:
        return ["⚡ SpeedBench", "---", "还没有测速记录 | color=gray", "---"]

    def num(v):
        return f"{v:g}" if isinstance(v, (int, float)) else "--"

    best = results[0]
    best_name = best.get("name") or "?"
    title_name = disp(best_name)
    if len(title_name) > 12:  # 菜单栏标题过长时截断节点名到 12 字符
        title_name = title_name[:11] + "…"

    out = [f"⚡{title_name} {num(best.get('median_mbps'))}Mbps", "---"]
    out.append(f"上次测速：{record.get('ts') or '?'} | color=gray size=12")
    out.append("---")
    for r in results[:5]:
        name = r.get("name") or "?"
        line = (f"{r.get('stars') or ''} {disp(name)} — "
                f"{num(r.get('median_mbps'))}Mbps / {num(r.get('latency_ms'))}ms")
        # param3 传原始节点名（含 ASCII "|" 也安全：只按第一个 | 分隔标题/参数）
        line += (f' | shell="{pybin}" param1="{switch_py}" param2="--name"'
                 f' param3="{name}" terminal=false refresh=true')
        out.append(line)
    out.append("---")
    return out


try:
    lines = build()
except Exception:
    lines = ["⚡ SpeedBench", "---", "读取历史记录失败 | color=gray", "---"]
print("\n".join(lines))
PYEOF

# ---- 静态动作区 ----
printf '切换到冠军节点 | shell="%s" param1="%s" param2="--best" terminal=false refresh=true\n' \
    "$PYTHON3" "$SWITCH_PY"
printf '开始全量测速（终端） | shell="/usr/bin/open" param1="-a" param2="Terminal" param3="%s"\n' \
    "$COMMAND_FILE"
# shell 只能放一条命令，所以用 bash -c 组合：8950 未监听则后台起 Web 面板，再打开浏览器。
# 注意该命令未经 shell 转义地进入 param2，路径中含空格或引号会出问题——本仓库路径无空格。
WEB_CMD="if ! /usr/bin/nc -z 127.0.0.1 ${WEB_PORT} >/dev/null 2>&1; then nohup ${PYTHON3} ${WEB_PY} >/dev/null 2>&1 & sleep 1; fi; /usr/bin/open http://127.0.0.1:${WEB_PORT}"
printf '打开 Web 面板 | shell="/bin/bash" param1="-c" param2="%s" terminal=false\n' "$WEB_CMD"
printf '%s\n' '---'
printf '刷新 | refresh=true\n'
