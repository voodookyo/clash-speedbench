## v1.0.0：多源 IP Intelligence、风险评级与环境泄漏检测

Clash SpeedBench v1.0.0 在保留原有两阶段测速、Mihomo worker、自动恢复、SQLite 历史和本地 Web 面板的基础上，增加了网络稳定性、多源出口 IP 情报和客户端环境审计。

### 主要变化

- **网络性能与 IP 质量分离**：新增 Network Score、IP Quality Score、IP Grade 和 Overall；IP 查询失败/数据不足显示 `N/A`，不会再把 Unknown 当成干净 IP 或 100 分。
- **多源 IP Intelligence**：保留无需 Key 的 ip-api 基础画像，可选接入 IPinfo、IPQualityScore 和 Scamalytics 官方 API。第三方 provider 独立超时、限速、配额和套餐状态，一个来源不可用不影响测速。
- **证据式 IP 分类**：支持 residential、residential_proxy、corporate、mobile、datacenter、vpn_proxy、unknown，并展示置信度、证据和冲突。`hosting=false` 或“ISP/非托管”不等于住宅；Residential 与 Residential Proxy 也不会混为一类。
- **风险明细**：分别保留 IPQS Fraud Score、Recent Abuse/Abuse Velocity 与 Scamalytics Fraud Score/Risk/Blacklist 等实际返回字段。SpeedBench Grade 是启发式推荐等级，不是厂商官方评分，也不是实际诈骗概率。
- **缓存与去重**：按 `provider + exit_ip` 使用 SQLite cache；ip-api/基础 ASN/ISP 默认 7 天，Privacy/风险信誉默认 24 小时。同一轮相同出口 IP 的多个节点只查询一次。
- **稳定性探测**：默认 3 次独立 HTTP/HTTPS application-level probe；`--stability` 默认 10 次，`--probe-count N` 可自定义。记录 attempts/successes/failures/success rate/loss，明确不冒充 ICMP packet loss。
- **双栈出口**：分别发现和展示出口 IPv4、IPv6；节点 IPv6 不可用是正常状态，不与客户端 IPv6 绕过混淆。
- **环境泄漏检测**：新增 `#/leak` 页面。WebRTC 使用浏览器标准 ICE/STUN，mDNS、隐私策略或 STUN 失败显示“无法确认”。DNS 采用 Guided Audit，打开 BrowserLeaks DNS / DNSLeakTest，由用户人工判读，不读取系统 DNS、不抓取网页 HTML。
- **密钥安全**：支持 `SPEEDBENCH_IPINFO_TOKEN`、`SPEEDBENCH_IPQS_KEY`、`SPEEDBENCH_SCAMALYTICS_USERNAME`、`SPEEDBENCH_SCAMALYTICS_KEY`、`SPEEDBENCH_SCAMALYTICS_REGION`；面板输入只驻留 localhost backend 内存，不写 localStorage、日志、JSONL、SQLite、CSV 或 HTTP response。Scamalytics v3 必须提供 Username + Key + Region（`eu`/`us`）。
- **面板加固**：所有 GET 路由同样强制校验唯一本机 Host（防 DNS rebinding）；拒绝请求时先读尽请求体再响应，修复 Windows 上偶发连接被 RST（WinError 10053）的问题。
- **兼容升级**：v0.8.2/v0.9.1 的 JSONL 和 SQLite 可直接回放。启动时只增量创建 `ip_intel_cache`、`ip_intel_results`、`leak_audits` 及兼容列，不删除或重写 `runs.raw`。

### 安装

#### macOS 12+

下载 `Clash-SpeedBench-v1.0.0-macos.zip`，解压后把 `Clash SpeedBench.app` 拖进“应用程序”。首次打开需右键选择“打开”（应用未使用付费开发者签名），系统需要 `python3`。

#### Windows 10/11

下载 `Clash-SpeedBench-v1.0.0-windows.zip`，解压后双击 `SpeedBench.bat`。需要 Python 3.9+；未安装时启动器会引导到 Microsoft Store。面板使用 `pythonw`，托盘和取消哨兵机制保持可用。

两个包均只包含 Python 标准库运行时所需文件和静态 Web UI，不需要 `pip install`。macOS/Linux 可用 `shasum -a 256`，Windows 可用 `Get-FileHash -Algorithm SHA256` 校验同名 `.sha256` 文件。

### 升级提示

升级前建议备份现有 JSONL 与 `speedbench-history.db`。没有任何可选 API Key 时，v1.0.0 仍完整运行并退化为 ip-api 基础画像；IP Grade 和风险分显示 `N/A`。详细迁移步骤、Key 安全边界和已知限制见 [README](../README.md)。

## v0.9.1：Windows 不再驻留控制台窗口

- **修复**：Windows 端双击 `SpeedBench.bat` 后任务栏常驻一个最小化 Python 控制台窗口的问题。面板改用 `pythonw` 无窗口启动（缺失时回退最小化控制台），面板日志写入 `%APPDATA%\ClashSpeedBench\web.log`。
- **取消机制**：面板通过 `SPEEDBENCH_CANCEL_FILE` 哨兵文件优雅中断，Clash 策略组/运行模式照常恢复；macOS/Linux 行为不变。
