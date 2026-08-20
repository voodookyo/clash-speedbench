#!/usr/bin/env python3
"""speedbench_switch.py — 把 Clash 主 Selector 组一键切换到指定节点。

供 SwiftBar 菜单栏插件和命令行使用：

    python3 speedbench_switch.py --best            # 切换到上次测速的冠军节点
    python3 speedbench_switch.py --name '节点名'   # 切换到指定节点

历史记录默认读取同目录的 speedbench-history.jsonl，
可用环境变量 SPEEDBENCH_HISTORY 覆盖；API secret 用环境变量 MIHOMO_SECRET 提供。
"""

import argparse
import json
import os
import sys
from pathlib import Path

from clash_speedbench import (
    ApiError,
    MihomoAPI,
    build_selectable_graph,
    detect_controller,
    pick_switch_group,
)

REPO_DIR = Path(__file__).resolve().parent
DEFAULT_HISTORY = REPO_DIR / "speedbench-history.jsonl"
ROOT_GROUP = "GLOBAL"


def fail(msg: str) -> int:
    print(f"❌ {msg}", file=sys.stderr)
    return 1


def load_best_name(history: Path) -> str:
    """读取 JSONL 历史最后一条记录，返回冠军节点名。"""
    if not history.exists():
        raise ApiError(f"找不到历史记录 {history}，请先运行 clash_speedbench.py 测速。")
    last = ""
    with history.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                last = line
    if not last:
        raise ApiError(f"{history} 里还没有测速记录，请先运行 clash_speedbench.py 测速。")
    try:
        record = json.loads(last)
    except json.JSONDecodeError as e:
        raise ApiError(f"历史记录最后一行不是合法 JSON: {e}") from e
    results = record.get("results") or []
    if not results:
        raise ApiError("上次测速没有任何节点结果，请重新测速。")
    best = results[0]
    name = best.get("name")
    if not name or best.get("score", 0) <= 0:
        raise ApiError("上次测速没有测出有效节点，请重新测速。")
    return name


def switch_to(name: str) -> int:
    secret = os.environ.get("MIHOMO_SECRET", "")
    try:
        base, needs_secret = detect_controller(secret, None)
    except ApiError as e:
        return fail(str(e))
    if needs_secret and not secret:
        return fail(f"{base} 需要访问密钥，请先设置环境变量 MIHOMO_SECRET 再重试。")

    api = MihomoAPI(base, secret=secret)
    try:
        data = api.get("/proxies")
        proxies = data.get("proxies", {}) if isinstance(data, dict) else {}
    except ApiError as e:
        return fail(f"读取节点列表失败: {e}")
    if not proxies:
        return fail("Mihomo 没有返回任何节点，请确认配置已加载。")

    graph = build_selectable_graph(proxies)
    group = pick_switch_group(proxies, graph, name, ROOT_GROUP)
    if not group:
        return fail(f"找不到包含节点 {name!r} 的 Selector 组（节点名错误或不在任何 Selector 中）。")

    try:
        if proxies.get(group, {}).get("now") == name:
            print(f"✅ {group} 已是 {name}，无需变更。")
            return 0
        api.select(group, name)
    except ApiError as e:
        return fail(f"切换失败: {e}")
    print(f"✅ 已切换 {group} → {name}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="把 Clash 主 Selector 组切换到指定/冠军节点"
    )
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--best", action="store_true", help="切换到上次测速的冠军节点")
    which.add_argument("--name", help="切换到指定节点名")
    args = parser.parse_args()

    if args.best:
        history = Path(os.environ.get("SPEEDBENCH_HISTORY", str(DEFAULT_HISTORY)))
        try:
            name = load_best_name(history)
        except ApiError as e:
            return fail(str(e))
    else:
        name = args.name.strip()
        if not name:
            return fail("节点名不能为空。")
    return switch_to(name)


if __name__ == "__main__":
    sys.exit(main())
