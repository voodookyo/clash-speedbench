# Clash SpeedBench

给**正在运行中的** Clash Verge Rev / Mihomo 加一个「节点体检」外挂：
**延迟 + 真实带宽 + 出口 IP 纯净度（住宅/机房/脏IP/风险）+ 综合星级评分**，一次跑完直接排名。

> English: A zero-dependency companion benchmark for a *running* Clash Verge Rev / Mihomo
> instance — real per-node download Mbps (not just ping), exit-IP purity
> (ASN / country / ISP / residential-or-datacenter / risk), a composite star rating,
> and optional auto-switch to the winner. Single Python file, no third-party packages.

## 为什么不用现成的？

现有工具（faceair/clash-speedtest 等）都是「读订阅文件 → 自己起核心 → 测速」，
协议支持永远落后于你的客户端。SpeedBench 走的是另一条路：

```
你的 Clash Verge（已配置好一切协议）
   ↑ External Controller API（支持新版默认的 Unix socket）
SpeedBench ── 逐节点切换 → 经 mixed-port 真实下载 → 查出口 IP 画像
   ↓
测完自动恢复你原来的模式与节点选择
```

- **挂接运行中的客户端**：不改订阅、不改配置、不需要把节点导入别的工具
- **支持 Unix socket controller**：新版 Clash Verge Rev 默认只开 socket（TCP 9097 其实没绑定）
- **真实带宽**：通过 mixed-port 用 curl 实际下载，不是拿 `/delay` 冒充速度
- **IP 纯净度**：每节点查出口 IP 的 ASN / 国家 / ISP / 住宅·机房·移动·代理 / 风险等级（ip-api.com，无需 key）
- **综合评分**：带宽 55% + 延迟 25% + IP 风险 20% → ★1~5
- **自动标签**：低延迟但龟速 / 高带宽 / 住宅IP / 脏IP / 高风险，一眼看清
- **安全恢复**：测完（含 Ctrl+C）自动恢复 Rule/Global 模式和原节点选择
- **可选自动切换**：`--auto-switch` 测完直接把主策略组切到冠军节点
- **零依赖**：单文件 Python 3.8+，只用标准库 + 系统自带 curl

## 快速开始

前提：Clash Verge Rev（或其他 Mihomo 客户端）正在运行，且开启外部控制（默认即可）。

```bash
git clone https://github.com/voodookyo/clash-speedbench.git
cd clash-speedbench

# 先小规模试跑 5 个节点
python3 clash_speedbench.py --limit 5

# 全量测速
python3 clash_speedbench.py --yes

# 只测香港/日本/新加坡，测完自动切到最强节点
python3 clash_speedbench.py --include '香港|HK|日本|JP|新加坡|SG' --auto-switch --yes
```

macOS 用户也可以直接**双击 `speedbench.command`**（可拖到桌面或 Dock 当按钮用）。

## 输出示例

```
┌ Clash SpeedBench ────────────────────────────────────────────────────────────┐
│ 节点                 │ 延迟  │ 带宽     │ 评分  │ IP画像         │ 标签          │
├──────────────────────┼───────┼──────────┼───────┼────────────────┼───────────────┤
│ 新加坡 02 | 高速推荐 │ 92ms  │ 21.0Mbps │ ★★★☆☆ │ SG·机房·风险中 │ 低延迟,机房IP │
│ 台湾 1               │ 179ms │ -        │ ☆☆☆☆☆ │ TW·住宅·风险低 │ 不通,住宅IP   │
│ 拉斯维加斯 01        │ 249ms │ 16.0Mbps │ ★★☆☆☆ │ US·代理·风险高 │ 脏IP,高风险   │
└──────────────────────┴───────┴──────────┴───────┴────────────────┴───────────────┘
  延迟测速 │ 带宽测速 │ IP质量 │ 综合评分 = 带宽55% + 延迟25% + IP风险20%

✅ 自动切换：节点选择 → 新加坡 02 | 高速推荐（21.0 Mbps / 92 ms / ★★★☆☆）
CSV 已保存: clash-speedtest-20260820-181130.csv
```

CSV 包含全部字段：延迟/中位带宽/峰值/各轮采样/评分/星级/标签/出口IP/国家/ASN/ISP/ORG/IP类型/风险/状态。

## 常用参数

| 参数 | 说明 | 默认 |
|---|---|---|
| `--include REGEX` | 只测匹配的节点 | 全部 |
| `--exclude REGEX` | 排除匹配的节点（自动过滤「剩余流量」等伪节点） | 内置 |
| `--limit N` | 最多测 N 个（先试跑） | 全部 |
| `--mb MB` | 单轮下载量 | 30 |
| `--rounds N` | 每节点轮数 | 1 |
| `--max-time S` | 单轮最长秒数 | 4 |
| `--no-ip` | 跳过 IP 画像 | 关 |
| `--auto-switch` | 测完自动切到冠军节点 | 关 |
| `--switch-group NAME` | 指定要切换的策略组 | 自动探测主 Selector |
| `--controller URL` | 指定 controller（支持 `unix://` 前缀） | 自动探测 |
| `--top N` | 表格只显示前 N 名 | 全部 |

设置了 External Controller Secret 时，用环境变量传入（避免写进 shell history）：

```bash
export MIHOMO_SECRET='你的secret'
```

## 注意事项

- **测速期间会临时切到 GLOBAL 模式**，全网流量跟着被测节点走；别在视频会议/游戏时跑。结束或 Ctrl+C 后自动恢复。
- **流量消耗**：全量 ≈ 节点数 × `--mb` × `--rounds`，默认 44 节点约 1.3 GiB。粗筛可用 `--mb 15`。
- 评分里 100 Mbps 为带宽满分；如果你的线路本身只有 ~30 Mbps，最强节点就是 ★★★☆☆ 左右，这是正常的。
- IP 画像来自 ip-api.com 免费端点（每节点经各自出口查询，45 次/分钟限制互不冲突）。

## License

[MIT](LICENSE)
