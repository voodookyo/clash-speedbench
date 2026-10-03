# Clash SpeedBench

给**正在运行中的** Clash Verge Rev / Mihomo 加一个「节点体检」外挂：
**延迟 + 真实带宽 + 多源出口 IP Intelligence + IP 风险评级 + 节点稳定性**，一次跑完直接排名。

当前版本：**v1.0.1**。网络质量、IP 质量和客户端环境泄漏是三个彼此独立的维度。

> 开发分支正在实施四阶段升级：可靠订阅来源/稳定节点 ID、quick/standard/deep/ip
> 任务模式、实时任务中心和共享界面、Tauri 桌面客户端。桌面 **1.1.0-alpha.1**
> 已进入本地 Windows 构建验证，尚未作为稳定 Release 发布。完整资源包自带固定
> Python；测速仍使用标准库 + 系统 curl/Mihomo。[桌面构建、数据迁移与限制](desktop/README.md)。
> macOS/Linux 原生运行、完整桌面交互及四阶段最终验收仍未完成，不能把构建配置视为已兼容。

开发版设置页提供显式“检查正式 Release”：点击才访问固定 GitHub 官方 API，不携带
密钥或历史，不检查 alpha 更新；失败显示无法确认，alpha 版本号领先正式版也不会自动降级。
升级仍由用户在官方 Release 手动选择平台包、核对 SHA-256、退出写入程序并备份后完成，
不自动下载／安装。校验和不能代替数字签名，当前桌面包为 unsigned。

> English: A zero-dependency companion benchmark for a *running* Clash Verge Rev / Mihomo
> instance — real per-node download Mbps, multi-source exit-IP intelligence and
> reputation, application-level stability probes, a composite network/IP view,
> and optional auto-switch to the winner. Standard library only at runtime.

## 为什么不用现成的？

现有工具（faceair/clash-speedtest 等）都是「读订阅文件 → 自己起核心 → 测速」，
协议支持永远落后于你的客户端。SpeedBench 走的是另一条路：

```
你的 Clash Verge（已配置好一切协议）
   ↑ External Controller API（支持新版默认的 Unix socket）
SpeedBench ── 逐节点切换 → 经 mixed-port 真实下载 → 查出口 IP Intelligence
   ↓
测完自动恢复你原来的模式与节点选择
```

- **挂接运行中的客户端**：不改订阅、不改配置、不需要把节点导入别的工具
- **支持 Unix socket / Windows 命名管道 controller**：新版 Clash Verge Rev 默认只开 socket（macOS/Linux）或 `\\.\pipe\verge-mihomo` 管道（Windows），TCP 9097 其实没绑定
- **真实带宽**：通过 mixed-port 用 curl 实际下载，不是拿 `/delay` 冒充速度
- **多源 IP Intelligence**：ip-api 基础画像可免费使用；可选接入 IPinfo、IPQualityScore（IPQS）和 Scamalytics，展示 ASN / 国家 / ISP / Hosting / Proxy / VPN / Tor / Mobile 等实际可用字段
- **证据式 IP 分类**：住宅 ISP、ISP 住宅代理、企业/商宽、移动、数据中心、代理/VPN、未知；多源冲突显示为低置信度未知，不把 `hosting=false` 粗暴当成住宅
- **分离评分**：Network Score 只衡量延迟、jitter、建连、带宽和 application-level probe；IP Quality Score/Grade 只在有足够 Intelligence 数据时生成；Overall 缺失维度重新归一化，不奖励查询失败
- **稳定性与双栈**：默认 3 次 application-level probe，可用 `--stability` 或 `--probe-count` 增加次数；分别记录出口 IPv4 / IPv6 与探测失败率
- **自动标签**：低延迟但龟速 / 高带宽 / ISP/非托管 / 机房托管 / 代理/VPN / 高风险，一眼看清
- **安全恢复**：测完（含 Ctrl+C）自动恢复 Rule/Global 模式和原节点选择
- **可选自动切换**：`--auto-switch` 测完直接把主策略组切到冠军节点
- **零依赖**：Python 3.9/3.12 均可，只用标准库 + 系统自带 curl；没有任何 API Key 也能正常测速

## 两阶段测速（默认）

开发版设置页新增“数据位置与迁移指引”及“显式迁移界面偏好”：从原浏览器
导出白名单 JSON，在桌面预览并确认导入；收藏合并，未匹配项保留待确认。
不转移历史、种子、路径、控制器信息、API Key 或写 token；不读取浏览器 profile。
旧版无导出按钮时，先备份，再在原浏览器和原 host/port 加载新版共享 Web 界面。
历史目录导入器仍未完成，不能把偏好导入称为完整历史迁移。

开发版新增任务模式（仅隔离 worker）：

| 模式 | Probe 次数 | 默认带宽范围 | 出口画像范围 |
|---|---|---|---|
| `--mode quick` | 3 | Top 5，10MB / 3 秒 / 1 轮 | 精测候选 |
| `--mode standard` | 3 | Top 10，自适应样本 / 1 轮 | 精测候选 |
| `--mode deep` | 10 | 全部连通节点，自适应样本 / 1 轮 | 全部 |
| `--mode ip` | 3 | 不下载，带宽 N/A | 全部 |

新模式优先覆盖不同已验证订阅，再按延迟补足候选；目前没有根据节点名称猜测地区，
也尚未接入历史速度候选提示。`--all-ip` 可扩大 quick/standard 的画像范围。
高级参数显式覆盖默认值；不指定 `--mode` 时旧 CLI 的 Top 15 / 自适应行为保持。
后端 `/api/run` 同样支持 `mode`，`/api/task-config` 返回共享默认值和允许范围；
共享界面可选择目标、模式和来源范围，并在任务中心续接进度。

快速/标准模式只提供已测范围内推荐，不保证全订阅绝对最快。结果含测量范围，
已完成带宽精测者优先于仅测延迟者，避免缺少指标被重新归一化后反获最高分。
新模式暂不静默回退到改变当前节点的串行测试；隔离 worker 不可用时明确退出。
需要旧串行测试时不指定 `--mode`，继续使用 `--workers 1`。
流量预算是请求上限（含自适应预热），不是实际下载字节；实际耗时与计数验收尚未完成。

新模式候选先考虑订阅/已知地区代表，再按当前延迟填充；下载目标可参考同一
强身份节点近 7 天的成功单流带宽记录。提示只从已有 SQLite 镜像只读取得，
每任务加载一次，总查询预算 250ms；无库、旧格式、占用或超时则退回当前探测。
历史国家也只是候选覆盖提示，不证明本次出口仍在该国。不按名字借用历史，
不把旧带宽复制为本次成绩；legacy 延迟 Top N、手选/来源范围、多节点带宽串行不变。

为了保证带宽数字可信，**同一时刻全网只有一路测速下载**——并发只用于小流量探测：

```
Phase 1 粗筛（--workers 6 并发）：延迟×3 + 抖动 + application-level probe + 双栈出口 IP
   ↓ 剔除不通，按延迟升序取 Top N（--top-n，默认 15；--all 全量精测）
Phase 2 精测（严格串行）：warmup 1MB → 自适应样本（10~95MB）→ 单流/可选多流真实带宽
按不同出口 IP 去重 → SQLite cache → 仅对 cache miss 查询可选 Intelligence provider
```

100 个节点的粗筛十几秒就能跑完，只有 Top N 会进入耗流量的带宽精测。
相同出口 IP 的多个节点共享 Intelligence 结果；缓存命中不会重复消耗第三方查询额度。
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

## IP Intelligence、分类与评分

自 v1.0.0 起，基础画像与第三方 Intelligence 分开。默认仍调用无需 Key 的
`ip-api.com`，可选 provider 只使用各厂商官方 API：

| 来源 | 主要内容 | 配置 | 默认缓存 |
|---|---|---|---|
| ip-api | 国家、ASN/AS 名、ISP/组织、Hosting、Proxy、Mobile | 无需 Key；`SPEEDBENCH_DISABLE_IP_API=1` 可禁用 | 7 天 |
| IPinfo | ASN/Company/ISP、Privacy、Hosting、VPN/Proxy/Tor、套餐支持的 Residential Proxy | `SPEEDBENCH_IPINFO_TOKEN` | 24 小时 |
| IPQualityScore (IPQS) | Fraud Score、Proxy/VPN/Tor、ISP/Organization/ASN、Connection Type、Recent Abuse、Abuse Velocity、Bot Status | `SPEEDBENCH_IPQS_KEY` | 24 小时 |
| Scamalytics | Fraud Score、Risk、Datacenter、Proxy/VPN/Server、Blacklist 等官方返回字段 | Username + Key + Region | 24 小时 |

Scamalytics v3 需要同时配置以下三个环境变量；`Region` 只接受账户对应的
`eu` 或 `us`，缺失时不会猜测 API 节点：

```bash
export SPEEDBENCH_IPINFO_TOKEN='…'
export SPEEDBENCH_IPQS_KEY='…'
export SPEEDBENCH_SCAMALYTICS_USERNAME='…'
export SPEEDBENCH_SCAMALYTICS_KEY='…'
export SPEEDBENCH_SCAMALYTICS_REGION='eu'   # 或 us
```

注意：ip-api 的官方免费 JSON 接口仅提供明文 HTTP（不是 HTTPS），因此保留它只是为了
兼容无 Key 的基础画像模式；SpeedBench 不向该接口发送任何 Key。官方免费服务限制为
每分钟 45 次请求且仅限非商业使用。ip-api 的字段不能单独
生成 IP Quality Score 或 IP Grade，也不会因查询成功而奖励节点。对传输保密有要求时可显式
禁用它：

```bash
export SPEEDBENCH_DISABLE_IP_API=1
# Windows PowerShell：
$env:SPEEDBENCH_DISABLE_IP_API='1'
```

禁用后 provider 仍会在状态列表中显示为 `disabled`，不会发起请求；IPinfo、IPQS 和
Scamalytics（若配置）仍独立运行。未设置该变量时 ip-api 默认启用，确保没有第三方 Key
时继续保留 v0.x 的退化能力。

也可以在 Web 面板的「⚙️ IP 设置」中输入。输入只提交给
`127.0.0.1` 上的 SpeedBench 后端并默认驻留当前进程内；不会写入
`localStorage`、Cookie、URL、命令行、日志、CSV、JSONL、SQLite 或 API response。
面板只显示每个 provider 的 `configured` 和运行状态（如 `cache_hit`、
`timeout`、`rate_limited`、`quota_unavailable`、`key_missing`），不会回显 Key。

未禁用且没有任何第三方 Key 时，测速、历史和 Web UI 仍完整工作，退化为 ip-api 基础画像。
IP Quality Score 与 IP Grade 此时显示 **N/A**，绝不会因为查询失败获得满分。
相同 provider 的相同出口 IP 共享 SQLite `ip_intel_cache`：基础 ASN/ISP 数据 TTL
为 7 天，Privacy/风险信誉 TTL 为 24 小时；同一轮多个节点使用相同出口 IP 时只查询一次。

### Residential 不是 Residential Proxy

分类器保留“证据 + 置信度 + 冲突”而非单一数据库断言，内部类别为：

`residential`、`residential_proxy`、`corporate`、`mobile`、`datacenter`、
`vpn_proxy`、`unknown`。

- `proxy=false`、`hosting=false`、`mobile=false` 只表示当前来源没有报出这些标记，
  等价于 **ISP/非托管**，不等于住宅。
- 住宅 ISP 需要明确 Residential 信号与独立来源的兼容证据；UI 使用“高置信度住宅 ISP”
  或“疑似住宅”，不使用“100% 真住宅”。
- Consumer ISP 同时出现住宅代理信号时显示“ISP住宅代理”，不会把它当作普通住宅。
- Hosting/Data Center、明确 VPN/Proxy/Tor、Mobile 和 Corporate/Business 会分别归类。
- `IPQS Residential` 与 `IPinfo Hosting=True` / `Scamalytics Datacenter=True` 等强信号冲突时，
  显示“未知/冲突”、低置信度，并展开列出双方证据；不会自行选边。

IPQS Fraud Score 与 Scamalytics Fraud Score 始终分别展示，**不把两者简单平均称为真实风险，
也不等于实际诈骗概率**。SpeedBench 额外生成 `IP Quality Grade`（S/A/B/C/D），综合考虑风险分、
Hosting/Datacenter、Proxy/VPN/Tor、Residential Proxy、Recent Abuse、Blacklist、Bot 状态与多源一致性。
这是 SpeedBench 的启发式推荐等级，不是 IPQS 或 Scamalytics 官方评级，也不是诈骗概率。

节点主表只显示 `Network`、`IP Grade`、`IP 类型` 和 `风险`；点击节点可展开完整 provider 字段、
置信度、Evidence、Conflicts、原始风险分和 provider/cache 状态。

## Windows

**依赖**：Windows 10/11、正在运行的 Clash Verge Rev、Python 3.9+
（推荐从 Microsoft Store 安装，免手动配置 PATH）、无需管理员权限。

**安装**：从 [Releases](https://github.com/voodookyo/clash-speedbench/releases) 下载
`Clash-SpeedBench-*-windows.zip`，解压后**双击 `SpeedBench.bat`** 即可。
若未检测到 Python，会自动打开 Microsoft Store 的 Python 3 页面，装好后重新双击即可；
数据（历史记录 / Web 令牌）存放在 `%APPDATA%\ClashSpeedBench\`，不污染源码目录。

**使用**：与 macOS 一致——浏览器自动打开 `http://127.0.0.1:8950`，在面板里一键测速、
排序、切换节点；「中断测速」在 Windows 下通过哨兵文件（`SPEEDBENCH_CANCEL_FILE`）
优雅中断，同样自动恢复 Clash 的运行模式与节点选择。面板经 `pythonw` 启动，
**不再有常驻控制台窗口**；托盘图标在右下角（左键开面板、右键退出），
面板里的「退出 SpeedBench」按钮同样可用。
同时右下角托盘会出现 SpeedBench 图标：**左键点一下打开面板**，右键菜单可「打开面板 /
退出 SpeedBench」；托盘随面板自动出现、自动消失，无需手动管理。

**与 macOS 的差异**：没有 .app 安装包与 SwiftBar 菜单栏小工具（macOS 上的可选附加），
Windows 的对应物是随 `SpeedBench.bat` 一起启动的托盘图标。

## 输出示例

```
┌ Clash SpeedBench ──────────────────────────────────────────────────────────────────┐
│ 节点                 │ 延迟  │ 带宽     │ Network │ IP Grade │ IP 类型       │ 风险 │
├──────────────────────┼───────┼──────────┼─────────┼──────────┼───────────────┼──────┤
│ 新加坡 02 | 高速推荐 │ 92ms  │ 21.0Mbps │ 83.4    │ A        │ 数据中心      │ 低   │
│ 台湾 1               │ 179ms │ -        │ N/A     │ N/A      │ 未知          │ -    │
│ 拉斯维加斯 01        │ 249ms │ 16.0Mbps │ 46.2    │ D        │ 代理/VPN      │ 高   │
└──────────────────────┴───────┴──────────┴─────────┴──────────┴───────────────┴──────┘
  Network = 带宽 + 延迟 + jitter + connect + application-level probe；IP Grade 为独立启发式评级

✅ 自动切换：节点选择 → 新加坡 02 | 高速推荐（21.0 Mbps / 92 ms / ★★★☆☆）
CSV 已保存: clash-speedtest-20260820-181130.csv
```

CSV 包含网络与兼容字段：延迟/中位带宽/峰值/各轮采样/Network/Overall/星级/标签、
probe attempts/successes/failures/loss、出口 IPv4/IPv6、国家/ASN/AS名/ISP/ORG、
IP 类型/Grade/状态。第三方 provider 的原始 response 与任何 Key 不写入 CSV；
详细的标准化 Intelligence 结果写入脱敏后的 JSONL/SQLite 结构供 Web 详情使用。

## 本地 Web 面板

```bash
python3 speedbench_web.py          # 打开 http://127.0.0.1:8950
# 或双击 speedbench-web.command
```

独立单页应用（`web/` 静态文件，零依赖无构建，左侧导航节点/历史/订阅/泄漏/设置/关于），只绑定 127.0.0.1：

- **节点视图**：一键开始测速（可设节点过滤/每轮 MB/轮数/自动切换），实时进度和日志，可随时「中断测速」（SIGINT 优雅中断，自动恢复 Clash 配置）；结果表格支持点表头排序（不通沉底）、当前节点高亮、行内一键切换、点击行展开详情（抖动/建连/单·多流/ASN/ISP/出口 IP）
- **历史视图**：历次测速轮次列表 + 该轮完整结果 + 任意节点 30 天带宽趋势图、出口 IP/ASN 变化时间线和 IP Grade/分类信誉变化（SQLite 历史库 `speedbench-history.db` 支撑）
- **来源选择（开发版）**：测速前可选择已加载的 Verge 订阅。通过本机 profile 与运行节点的有效连接定义验证来源，不将内核 provider 名直接视为订阅。相同定义属于多个订阅时标为多来源，脚本修改或无法验证时标为未知。未加载订阅不会被自动启用或下载。
- **订阅历史**：旧 provider 汇总与 `node_key` 入口保留；新来源历史按不透明 `subscription_id` 聚合，新 `node_id` 区分认证/传输/链式依赖。改名不断链，凭据变化产生新身份，不再将不同账号成绩误合并。旧 raw 不回填当前订阅，可用率和测量覆盖率分开计算。
- **评分 Profile 切换**：综合推荐 / ⚡日常（延迟+抖动优先）/ 🚀下载（单·多流带宽优先）/ 🧼IP（分类、风险、信誉优先）/ 🏠住宅优先；下载 Profile 不受 IP 风险排序干扰；搜索框实时过滤；地区分组榜单 + ⭐ 收藏节点（仅收藏偏好写入 localStorage）
- **环境泄漏检测 `#/leak`**：检测当前浏览器 + 当前 Clash/TUN + 当前活动节点，独立于离线节点结果；WebRTC 使用浏览器标准 RTCPeerConnection/ICE，识别 host/srflx/prflx/relay 和公网候选。mDNS、隐私策略或 STUN 失败时显示“无法确认”，不会误报“无泄漏”
- **DNS Guided Audit**：通过按钮打开 BrowserLeaks DNS / DNSLeakTest，由用户人工查看 resolver；不读取系统 DNS、不爬站点 HTML、不自动声称无泄漏。页面提示正常/危险判读并可保存人工结果
- **IP Intelligence 设置**：可输入 IPinfo、IPQS、Scamalytics（Username/Key/Region）凭据；只驻留 localhost backend 内存，页面只显示 provider 状态和 cache 状态，不返回 Key
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

开发版增加 `--subscription-id ID`、`--node-id ID`，可重复指定。ID 可从本机面板
`/api/catalog` 获取；来源仅限当前已加载范围，身份过期会拒绝操作，不隐式切订阅。
自定义配置可使用已有 `--config-file PATH` 指向本机 Verge 运行配置；该配置声明的
控制器必须与实际连接一致，否则来源降级为未知。UI 目录选择器仍在后续阶段实现。

新身份使用应用数据目录的私有 `identity-seed` 文件参与 HMAC。它不是 API Key，
不返回浏览器或进入导出/历史/发布包。POSIX 要求 0600；Windows 检查当前用户
所有权与受限 ACL。迁移时应连同原配置目录及身份种子备份；种子丢失、配置根目录
移动或凭据轮换可能产生新身份，旧历史仍保留，不自动合并。不要公开该文件。

| 参数 | 说明 | 默认 |
|---|---|---|
| `--include REGEX` | 只测匹配的节点 | 全部 |
| `--exclude REGEX` | 排除匹配的节点（自动过滤「剩余流量」等伪节点） | 内置 |
| `--limit N` | 最多测 N 个（先试跑） | 全部 |
| `--mb MB` | 单轮下载量；不指定时按 warmup 粗速度自适应（10~95MB） | 自适应 |
| `--rounds N` | 每节点轮数 | 1 |
| `--max-time S` | 单轮最长秒数（自适应模式最多放宽到 6s） | 4 |
| `--probe-count N` | Phase 1 独立 HTTP/HTTPS application-level probe 次数；失败继续后续探测 | 3 |
| `--stability` | 稳定性模式；未指定 `--probe-count` 时执行 10 次 probe | 关 |
| `--top-n N` | Phase 2 串行精测的节点数（Phase 1 延迟升序选取） | 15 |
| `--all` | Phase 2 对所有连通节点串行精测 | 关 |
| `--multi` | Phase 2 追加同节点 4 路并发流测峰值（多耗约 4 倍流量） | 关 |
| `--no-ip` | 跳过 IP 画像 | 关 |
| `--ip-timeout S` | 出口 IP / 基础画像查询超时 | 8 |
| `--intel-workers N` | 不同出口 IP 的 Intelligence 查询并发数（建议 2~4） | 3 |
| `--auto-switch` | 测完自动切到冠军节点 | 关 |
| `--switch-group NAME` | 指定要切换的策略组 | 自动探测主 Selector |
| `--controller URL` | 指定 controller（支持 `unix://`、`pipe://`（Windows）前缀） | 自动探测 |
| `--top N` | 表格只显示前 N 名 | 全部 |
| `--workers N` | 并发 worker 数；1=关闭并发（串行模式） | 6 |
| `--config-file PATH` | 并发模式用的配置文件（含节点凭据） | 自动探测 |
| `--history PATH` | 历史记录 JSONL 路径 | 脚本目录下 |
| `--no-history` | 不写历史记录 | 关 |

### Clash Verge 控制器自动连接

SpeedBench 的 CLI、Web 当前节点/切换和测速入口会自动读取本机 Clash Verge 的
`clash-verge.yaml`，获得控制器地址及 `secret`，包含新版 Windows 动态命名管道。
正常情况下直接双击启动即可，不需要每次设置环境变量，也不需要关闭控制器认证。
此密钥是 **Mihomo External Controller 访问密钥**，与可选的 IPinfo/IPQS/Scamalytics API Key 无关。

默认配置目录：

- Windows：`%APPDATA%\io.github.clash-verge-rev.clash-verge-rev\`
- macOS：`~/Library/Application Support/io.github.clash-verge-rev.clash-verge-rev/`
- Linux：`$XDG_CONFIG_HOME/io.github.clash-verge-rev.clash-verge-rev/`，未设置时使用 `~/.config/`。

运行配置不存在时才回退同目录的 `config.yaml`；不能读取/不支持的格式会明确提示，
仍保留旧版默认地址探测。便携版或自定义配置目录未被自动发现时，可以通过 CLI 的
`--controller` 指定控制器，并用环境变量提供密钥；Web 仍支持默认地址与 `MIHOMO_SECRET` 回退。
控制器字段仅支持 Verge 生成的顶层单行标量，不执行 YAML，不引入第三方 Python 依赖。

优先级为显式 `--secret` > `MIHOMO_SECRET` > 本机配置（显式空值也属于手动设置）。
错误或过期的手动密钥不会被自动覆盖；恢复自动发现时需移除旧环境变量。
`--controller` 限定连接目标；自动密钥只发给同一配置文件声明的本机 loopback/IPC 地址，
不发给不匹配的地址或远程控制器。手动连接远程实例仍需自行提供其密钥。
每次连接重读本机配置；自动认证失败最多再重读一次，不自动重放节点切换操作。

密钥只在后端内存中使用，不返回浏览器，不写测速历史、SQLite、日志或子进程命令行；
自动密钥也不写入全局环境，不传给测速 worker 或 IP Intelligence provider。
Web 测速在启动子进程前验证认证，失败立即报告，不再等待不可见的密码输入。
交互 CLI 保留隐藏密码输入；`--non-interactive` 或非终端输入时认证失败直接退出。

手动覆盖示例（这些命令仍会进入 shell history，请勿将含实际密钥的历史/截图公开）：

```bash
export MIHOMO_SECRET='你的secret'
```

Windows PowerShell：

```powershell
$env:MIHOMO_SECRET = '你的secret'
.\SpeedBench.bat
```

若此前设置过临时变量，移除后可测试自动发现：`Remove-Item Env:MIHOMO_SECRET -ErrorAction SilentlyContinue`。
若用户/系统级变量仍存在，还需在 Windows 环境变量设置中移除旧值，并从新进程启动。
更新程序后必须退出旧 SpeedBench 后台再重新启动；仅刷新或关闭浏览器不会加载新后端代码。

## 注意事项

- **仅串行模式**（`--workers 1` 或并发不可用时的回退）会临时切主实例到 GLOBAL 模式，全网流量跟着被测节点走；别在视频会议/游戏时跑。结束或 Ctrl+C 后自动恢复。两阶段模式全程不碰你正在用的 Clash。
- **流量消耗**：全量 ≈ Phase 2 节点数 × 样本大小 × `--rounds`（自适应 10~95MB），默认 Top 15 约 0.5~1.4 GiB；`--all` 精测全量会大很多。粗筛阶段只走小流量探测，可忽略。
- **探测失败率定义**：`probe_loss_pct` 是 HTTP/HTTPS application-level probe failure rate（应用层探测失败率），不是 ICMP 层真实 packet loss，也不等于物理链路丢包率。延迟/jitter 只按成功样本计算；全部失败时显示 N/A。
- **清理失败（开发版）**：中断时仅按本任务登记的 worker 句柄并发清理（最多 16 路），不按进程名杀 Mihomo。每个 worker 原有 terminate/kill 等待重叠，不再逐个累加；这不是操作系统／文件删除的硬总时限承诺。只有确认进程退出才显式删除临时配置，GC 不自动删除仍可能使用的配置。若清理仍失败，CLI 返回专用退出码 3，不回退串行；后端保留已接收的部分任务结果并标记 failed，而不是“取消成功”。同一后端阻止新任务和目录修改，请退出并先核对该任务残留资源，不要按进程名批量结束用户 Clash 或广泛删除临时目录。失败后直接 CLI 的自动部分历史报告、全阶段取消预算仍待完善。
- **评分**：100 Mbps 仍是单流带宽满分标尺；Network Score 由单流/多流、延迟、jitter、TCP/TLS connect 和 probe success 组成。IP Quality Score 可用时 Overall 按 Network 80% + IP Quality 20%，数据缺失时按剩余有效维度重新归一化；未知 IP 不会得到 100 分。
- **双栈边界**：节点支持 IPv6 与客户端真实 IPv6 是否绕过代理是两件事。每节点分别记录出口 IPv4/IPv6；双栈国家/ASN 不一致只是出口画像提示，只有「环境泄漏检测」页面才判断当前客户端是否绕过。
- **IP 画像与配额**：未禁用且无 Key 时使用 ip-api 免费基础画像；第三方 provider 可能受账户套餐、配额、限速和 TTL 影响，缺失字段保持未知，不把缺失解释为 false。

## 环境泄漏检测的边界

`#/leak` 页面检测的是**当前浏览器 + 当前 Clash/TUN + 当前活动节点**，不是某个离线测速 worker
或某个节点的固有属性。

- WebRTC 由浏览器通过标准 `RTCPeerConnection` / ICE / STUN 收集 candidates。
  只把公网 `host` / `srflx` / `prflx` 与当前浏览器出口比较；私网、loopback、link-local、
  `.local` mDNS 不当作公网泄漏，`relay` 只做展示。
- 公网候选与当前出口不一致、出现中国公网/中国联通地址或未经代理的 IPv6 时提示潜在泄漏。
  浏览器隐私策略、mDNS、STUN 不可用或缺少可比出口时显示“无法确认”，完整采集且没有异常才显示
  “未发现明显泄漏”，不使用绝对“无泄漏”。
- DNS 需要外部 authoritative observer 才能确定实际 resolver。本版是 Guided Audit：打开
  BrowserLeaks DNS 或 DNSLeakTest 并人工判读，不读取系统 DNS、不抓取网页 HTML；未来可接自建
  `DnsLeakProvider`。

## 升级与迁移（v0.8.2 / v0.9.1 / v1.0.0 → v1.0.1）

这是向后兼容升级，可直接替换代码或重新解压发布包：

1. 先备份现有 `speedbench-history.jsonl` 和 `speedbench-history.db`（Windows 默认在
   `%APPDATA%\ClashSpeedBench\`，macOS App 在 `~/Library/Application Support/ClashSpeedBench/`）。
2. 安装 v1.0.1 后首次打开 Web 面板会增量导入旧 JSONL，并只创建新的
   `ip_intel_cache`、`ip_intel_results`、`leak_audits` 表/兼容列；不会删除、重写 `runs.raw`，
   旧轮次缺失 Intelligence 时显示 N/A。
3. 旧的 ip-api `ip_profiles` / 出口 IP / ASN 时间线继续可回放；新的轮次按出口 IP 复用缓存，
   并可显示 IP Grade、风险和 Residential → Residential Proxy 等分类变化。
4. 想启用可选 provider 时再配置环境变量或在面板输入 Key；不配置任何 Key 不影响原有测速和历史功能。

`--no-ip` 仍可完全跳过出口画像；`--stability` / `--probe-count` 是新增可选项，默认普通模式只做
3 次 application-level probe，不改变 Phase 1/Phase 2 和单流测速逻辑。Windows、macOS、Linux
均保持 Python 3.9/3.12 与标准库运行，不需要 `pip install`；PyYAML 仍只是可选配置解析 fallback。

## 自定义 Verge 配置根目录（开发版）

默认自动发现保持不变。便携、自定义安装或多配置环境可在共享 Web／桌面设置中输入本机
**绝对目录**，先预览固定布局，再确认应用。要求目录内存在 `clash-verge.yaml`、
`profiles.yaml` 和 `profiles/`；不支持 UNC／Windows 映射网络盘、越界文件链接或超限文件。
预览只检查布局，不代表已经连接或订阅来源核验成功。没有通用文件读取／导出 API，
程序不会修改 Verge 配置，也没有新增原生文件选择器。

应用后控制器、订阅目录和测速 worker 使用同一根目录；任务接受时冻结此选择，运行、取消
及清理期间禁止更改。无效自定义目录**不会悄悄回退**到默认目录或另一个控制器，必须修正
目录或明确点击“恢复自动发现”。切换目录清除当前待测选择；历史、收藏和原始记录不删除。
稳定 ID 还依赖配置根目录命名空间与私有种子，不同根目录的旧 ID 不会强行匹配。

界面选择仅驻留本次后端内存；刷新页面保留，重启后重新读取启动设置。不进入 localStorage、
偏好导出、任务配置、JSONL 或 SQLite。需要固定启动目录时，由用户在启动环境设置
`SPEEDBENCH_VERGE_ROOT`（不要放入订阅配置内容或密钥），例如 PowerShell 当前会话：

```powershell
$env:SPEEDBENCH_VERGE_ROOT = 'C:\本机\Verge配置目录'
python speedbench_web.py
```

相同变量也适用于直接 CLI 和桌面后端；自定义根目录指定后，CLI 的 `--config-file` 必须
与该目录的 `clash-verge.yaml` 一致，否则测速前报错。恢复自动发现只影响当前后端，不修改
启动环境变量；下次启动仍读取该变量。`SPEEDBENCH_HOME` 是 SpeedBench **数据目录**，
不是 Verge 配置根目录，两者不要混淆。

### 同目录运行互斥（开发版）

桌面、独立 Web 和直接 CLI 在任何测速／身份／历史写入前取得数据目录内核锁。
CLI 默认历史目录遵循 `SPEEDBENCH_HOME`；显式 `--history` 位于另一目录时，两个目录均检查。
同目录已有后端时，请先正常退出后端再运行独立 CLI；不同目录不构成全局互斥，备份前仍须停止全部写入者。

后端启动的测速子任务经私有 stdin 管道核验实际父 PID、实例、目录和活跃内核锁，
没有环境变量或单一命令行参数跳过锁的通道。子任务持有独立 writer 锁直至报告和清理结束，
父管道 EOF 请求取消；即使父后端异常退出，新实例也不能抢占仍在清理的同目录子任务。
退出前取消未开始的 Intelligence 查询并等待在途缓存写入，之后才释放目录锁；不会在后台继续消耗队列中的查询额度。
私有帧不进 argv、日志或历史；锁文件不删除，旧 PID 元数据不会阻止已释放目录重启。
管道故障后若不能确认子任务已退出，保留原进程句柄并锁定新任务，不显示“清理成功”。
强制终止进程不能保证生成最终报告；直接 CLI 的异常部分历史与全阶段取消预算仍待完善。

## License

[MIT](LICENSE)
