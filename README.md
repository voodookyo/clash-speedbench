# Clash SpeedBench

给**正在运行中的** Clash Verge Rev / Mihomo 加一个「节点体检」外挂：
**延迟 + 真实带宽 + 出口 IP 画像（国家/ASN/ISP/托管·移动·代理标记）+ 综合星级评分**，一次跑完直接排名。

> English: A zero-dependency companion benchmark for a *running* Clash Verge Rev / Mihomo
> instance — real per-node download Mbps (not just ping), exit-IP profile
> (ASN / country / ISP / hosting-mobile-proxy flags), a composite star rating,
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
- **IP 画像**：每节点查出口 IP 的 ASN / 国家 / ISP / 托管·移动·代理标记（ip-api.com，无需 key；只如实展示标记，不断言"住宅"、不打风险分）
- **综合评分**：带宽 55% + 延迟 25% + IP 标记 20%（启发式扣分，非风险评分）→ ★1~5
- **自动标签**：低延迟但龟速 / 高带宽 / ISP/非托管 / 机房托管 / 脏IP，一眼看清
- **安全恢复**：测完（含 Ctrl+C）自动恢复 Rule/Global 模式和原节点选择
- **可选自动切换**：`--auto-switch` 测完直接把主策略组切到冠军节点
- **零依赖**：单文件 Python 3.8+，只用标准库 + 系统自带 curl

## 两阶段测速（默认）

为了保证带宽数字可信，**同一时刻全网只有一路测速下载**——并发只用于小流量探测：

```
Phase 1 粗筛（--workers 6 并发）：延迟×3 + 抖动 + 连通性 + 出口 IP 画像
   ↓ 剔除不通，按延迟升序取 Top N（--top-n，默认 15；--all 全量精测）
Phase 2 精测（严格串行）：warmup 1MB → 自适应样本（10~95MB）→ 真实带宽
```

100 个节点的粗筛十几秒就能跑完，只有 Top N 会进入耗流量的带宽精测。
工作原理（针对 TUN 环境做了特殊处理）：

- 节点凭据来自 Verge 生成的 `clash-verge.yaml`（用 macOS 自带 ruby 转 JSON，无第三方依赖）
- 测试域名和节点服务器域名通过 DoH 预解析后写进 worker 的 `hosts`，避开主实例 TUN 的
  fake-ip DNS 劫持
- 每个节点拨号绑定物理网卡（`interface-name`），绕过主实例 TUN，不会产生「双重代理」

并发不可用（非 Verge 安装、找不到配置/二进制、DoH 全挂）时自动回退到串行模式
（临时切 GLOBAL，测完恢复，串行模式逐节点全量精测）。也可用 `--workers 1` 强制串行，
或 `--config-file` 指定配置。

## 快速开始

前提：Clash Verge Rev（或其他 Mihomo 客户端）正在运行，且开启外部控制（默认即可）。

### 方式一：下载 App（推荐，无需拉源码）

从 [Releases](https://github.com/voodookyo/clash-speedbench/releases) 下载
`Clash-SpeedBench-*-macos.zip`，解压后拖进「应用程序」。首次打开需**右键 → 打开**
（应用未做付费开发者签名，Gatekeeper 会拦一次）。需要系统自带 python3
（装过 Xcode 命令行工具或 Homebrew Python 即可，没有的话 App 会弹窗提示）。
保持 Clash Verge 运行，双击图标即打开 Web 面板。

### 方式二：源码运行

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

macOS 用户也可以直接**双击 `speedbench.command`**（可拖到桌面或 Dock 当按钮用），双击后出现菜单：终端全量测速 / 打开 Web 面板 / 测速并自动切换冠军。

## 输出示例

```
┌ Clash SpeedBench ──────────────────────────────────────────────────────────────────┐
│ 节点                 │ 延迟  │ 带宽     │ 评分  │ IP画像                   │ 标签            │
├──────────────────────┼───────┼──────────┼───────┼──────────────────────────┼─────────────────┤
│ 新加坡 02 | 高速推荐 │ 92ms  │ 21.0Mbps │ ★★★☆☆ │ SG·机房托管·CHANGWAY-AP  │ 低延迟,机房托管  │
│ 台湾 1               │ 179ms │ -        │ ☆☆☆☆☆ │ TW·ISP/非托管·HINET      │ 不通,ISP/非托管  │
│ 拉斯维加斯 01        │ 249ms │ 16.0Mbps │ ★★☆☆☆ │ US·代理/VPN·M247         │ 脏IP            │
└──────────────────────┴───────┴──────────┴───────┴──────────────────────────┴─────────────────┘
  延迟测速 │ 带宽测速 │ IP画像 │ 综合评分 = 带宽55% + 延迟25% + IP标记20%（启发式扣分）

✅ 自动切换：节点选择 → 新加坡 02 | 高速推荐（21.0 Mbps / 92 ms / ★★★☆☆）
CSV 已保存: clash-speedtest-20260820-181130.csv
```

CSV 包含全部字段：延迟/中位带宽/峰值/各轮采样/评分/星级/标签/出口IP/国家/ASN/AS名/ISP/ORG/IP类型/原始标记/状态。

## 本地 Web 面板

```bash
python3 speedbench_web.py          # 打开 http://127.0.0.1:8950
# 或双击 speedbench-web.command
```

独立单页应用（`web/` 静态文件，零依赖无构建，左侧导航三视图），只绑定 127.0.0.1：

- **节点视图**：一键开始测速（可设节点过滤/每轮 MB/轮数/自动切换），实时进度和日志，可随时「中断测速」（SIGINT 优雅中断，自动恢复 Clash 配置）；结果表格支持点表头排序（不通沉底）、当前节点高亮、行内一键切换、点击行展开详情（抖动/建连/单·多流/ASN/ISP/出口 IP）
- **历史视图**：历次测速轮次列表 + 该轮完整结果 + 任意节点 30 天带宽趋势图与出口 IP 变化时间线（SQLite 历史库 `speedbench-history.db` 支撑）
- **评分 Profile 切换**：综合推荐 / ⚡日常（延迟+抖动优先）/ 🚀下载（单·多流带宽优先）/ 🧼IP（托管·代理标记优先）；搜索框实时过滤；地区分组榜单 + ⭐ 收藏节点（localStorage 持久化）
- 安全：每次启动生成随机 token，所有写操作 API 校验 `X-SpeedBench-Token` + Host/Origin 白名单；静态文件白名单服务（防目录穿越）；页面无 inline 事件，节点名永不进入 JS 源码

## 菜单栏小工具（可选，需 SwiftBar）

```bash
brew install --cask swiftbar
# 软链（推荐，自动定位仓库路径）进 SwiftBar 插件目录：
ln -s "$PWD/swiftbar/speedbench.5m.sh" ~/SwiftBar/
```

菜单栏常驻显示 `⚡冠军节点 带宽`，下拉可看上次 Top 5、一键切换节点、发起全量测速、打开 Web 面板。

命令行也可以单独使用切换工具：

```bash
python3 speedbench_switch.py --best          # 切到上次测速的冠军节点
python3 speedbench_switch.py --name '日本 01'
```

## 打包成 macOS App（最省事）

```bash
bash build_app.sh          # 生成 dist/Clash SpeedBench.app（含图标，自包含）
cp -R "dist/Clash SpeedBench.app" /Applications/
```

之后从启动台/应用程序**双击 Clash SpeedBench 图标**即可：自动启动面板并打开浏览器；
在面板里完成测速、看榜、切节点；点右上角「停止面板」即退出。
数据（历史/CSV/日志）存放在 `~/Library/Application Support/ClashSpeedBench/`，不污染应用包。

## 常用参数

| 参数 | 说明 | 默认 |
|---|---|---|
| `--include REGEX` | 只测匹配的节点 | 全部 |
| `--exclude REGEX` | 排除匹配的节点（自动过滤「剩余流量」等伪节点） | 内置 |
| `--limit N` | 最多测 N 个（先试跑） | 全部 |
| `--mb MB` | 单轮下载量；不指定时按 warmup 粗速度自适应（10~95MB） | 自适应 |
| `--rounds N` | 每节点轮数 | 1 |
| `--max-time S` | 单轮最长秒数（自适应模式最多放宽到 6s） | 4 |
| `--top-n N` | Phase 2 串行精测的节点数（Phase 1 延迟升序选取） | 15 |
| `--all` | Phase 2 对所有连通节点串行精测 | 关 |
| `--multi` | Phase 2 追加同节点 4 路并发流测峰值（多耗约 4 倍流量） | 关 |
| `--no-ip` | 跳过 IP 画像 | 关 |
| `--auto-switch` | 测完自动切到冠军节点 | 关 |
| `--switch-group NAME` | 指定要切换的策略组 | 自动探测主 Selector |
| `--controller URL` | 指定 controller（支持 `unix://` 前缀） | 自动探测 |
| `--top N` | 表格只显示前 N 名 | 全部 |
| `--workers N` | 并发 worker 数；1=关闭并发（串行模式） | 6 |
| `--config-file PATH` | 并发模式用的配置文件（含节点凭据） | 自动探测 |
| `--history PATH` | 历史记录 JSONL 路径 | 脚本目录下 |
| `--no-history` | 不写历史记录 | 关 |

设置了 External Controller Secret 时，用环境变量传入（避免写进 shell history）：

```bash
export MIHOMO_SECRET='你的secret'
```

## 注意事项

- **仅串行模式**（`--workers 1` 或并发不可用时的回退）会临时切主实例到 GLOBAL 模式，全网流量跟着被测节点走；别在视频会议/游戏时跑。结束或 Ctrl+C 后自动恢复。两阶段模式全程不碰你正在用的 Clash。
- **流量消耗**：全量 ≈ Phase 2 节点数 × 样本大小 × `--rounds`（自适应 10~95MB），默认 Top 15 约 0.5~1.4 GiB；`--all` 精测全量会大很多。粗筛阶段只走小流量探测，可忽略。
- 评分里 100 Mbps 为带宽满分；如果你的线路本身只有 ~30 Mbps，最强节点就是 ★★★☆☆ 左右，这是正常的。
- IP 画像来自 ip-api.com 免费端点（每节点经各自出口查询，45 次/分钟限制互不冲突）。

## License

[MIT](LICENSE)
