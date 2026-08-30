# Windows 端适配交接文件（v1.0.1）

本文档用于 Windows 10/11 的安装、离线回归和真机验收。v1.0.1 在 v1.0.0 的多源
IP Intelligence、应用层稳定性探测、双栈出口和环境泄漏检测基础上，修复 IPinfo 官方
`as` 响应兼容，并增加 ip-api 明文接口的完整 opt-out。当前仓库验证不等于真实机场配置上的
端到端保证；发布前仍应在一台实际 Windows 机器上执行下方 checklist。

## 30 秒项目速览

- **入口**：根目录 `SpeedBench.bat` → `pythonw` 无窗口启动 `speedbench_web.py`；缺少
  `pythonw` 时回退最小化控制台。数据目录默认 `%APPDATA%\ClashSpeedBench`。
- **架构**：Phase 1 并发粗筛（主 Mihomo `/delay` + application-level probe + worker 出口 IP），
  Phase 2 串行 Top-N 真实带宽（curl / mixed-port）。`--workers 1` 或并发不可用时回退到串行 GLOBAL 模式，
  测完自动恢复原模式和节点。
- **运行时模块**：`clash_speedbench.py`、`speedbench_workers.py`、`speedbench_web.py`、
  `speedbench_db.py`、`speedbench_ip_intel.py`、`speedbench_leak.py`、`speedbench_tray.py`。
- **硬约束**：Python 3.9/3.12、Windows/macOS/Linux、标准库 + 系统 curl；无 pip runtime dependency。
  PyYAML 仅是可选配置解析 fallback。

## 安装与路径确认

1. 安装 Python 3.9+（推荐 Microsoft Store 版），确认 `python --version` 可用；如 PATH 中
   `pythonw` 被禁用，先修复 Python 安装或接受 bat 的控制台 fallback。
2. 确认 Clash Verge Rev / Mihomo 正在运行并开启 External Controller。Windows 默认候选包括
   `pipe://verge-mihomo`；TCP 9097 仅作为 fallback。若 Controller Secret 必填，设置
   `MIHOMO_SECRET`，不要把 Secret 写进命令历史。
3. 确认运行配置 `%APPDATA%\io.github.clash-verge-rev.clash-verge-rev\clash-verge.yaml`
   存在；找不到时把实际路径通过 `--config-file` 传给 CLI。`verge-mihomo.exe` 的候选路径在
   `speedbench_workers.py`，不同安装方式可能需要补路径和 mock 测试。
4. 从 Release 解压 `Clash-SpeedBench-v1.0.1-windows.zip`，保持 `web\` 目录与入口脚本同级，
   双击 `SpeedBench.bat`。面板应打开 `http://127.0.0.1:8950`，并出现托盘图标。

## 离线回归

在仓库根目录运行：

```powershell
python -m unittest discover -s tests -v
python -c "import ast; from pathlib import Path; files=['clash_speedbench.py','speedbench_workers.py','speedbench_web.py','speedbench_switch.py','speedbench_ip_intel.py','speedbench_leak.py']; [ast.parse(Path(f).read_text(encoding='utf-8'), filename=f) for f in files]; print('AST syntax OK')"
```

测试不调用真实 IPinfo、IPQS 或 Scamalytics API，所有 provider 使用 mock fixture。无 PyYAML 时，
可选 YAML 对拍用例 skip 属于预期；不应因此安装 runtime package。

## 真机验收 checklist

1. **启动与安全**：双击 bat 后无常驻控制台（或仅在 `pythonw` 缺失时出现 fallback）；浏览器只能
   访问 loopback；页面写操作缺少 token、Host 或 Origin 时返回 403。
2. **小量测速**：节点过滤填一个小组名，使用 10MB × 1 轮。确认实时日志、结果表格、节点切换和
   `speedbench-history.jsonl`/SQLite 增量历史正常。
3. **稳定性指标**：默认 Phase 1 显示 3 次 probe 的 attempts/successes/failures/loss；使用
   `python clash_speedbench.py --stability --limit 3 --yes` 验证 10 次。这里的 loss 是
   **HTTP/HTTPS application-level probe failure rate**，不是 ICMP/物理链路 packet loss。
4. **双栈出口**：详情中分别查看 IPv4 和 IPv6；IPv6 不可用应显示 unavailable/N/A，不应让节点失败。
   节点 IPv6 能力与客户端真实 IPv6 绕过是两个独立问题。
5. **取消与恢复**：测速中点“中断测速”，确认 `%APPDATA%\ClashSpeedBench\cancel-request`
   哨兵可写，Clash 运行模式/策略组选择恢复原样。若 5 秒仍不退出，面板有 terminate 兜底；此时
   手动检查 Clash 模式，不要继续并发启动第二轮。
6. **TUN 场景**：保持 TUN 开启跑小规模测速。虚拟网卡检测失败或 worker 不可用时应有明确日志并
   回退串行，不应输出明显错误的带宽；完成后确认模式恢复。
7. **IP Intelligence（可选）**：无 Key 先验证 ip-api 基础画像与所有测速功能仍正常，IP Grade 应为
   `N/A`。配置 Key 只能通过环境变量或 Web “IP 设置”页面；检查状态页只显示 configured/status，
   不回显凭据。
8. **泄漏页面**：打开 `#/leak`。WebRTC 采集完整且可比较时仅显示“未发现明显泄漏”；mDNS、隐私策略
   或 STUN 失败时必须显示“无法确认”。打开 BrowserLeaks DNS / DNSLeakTest 人工查看 resolver；
   本版不读系统 DNS、不抓 HTML，DNS 结论只能是 Guided Audit。
9. **历史迁移**：升级前备份 JSONL/DB。首次启动只创建 `ip_intel_cache`、`ip_intel_results`、
   `leak_audits` 和兼容列，不删除或重写 `runs.raw`；旧轮次 Intelligence 缺失时显示 N/A。
10. **全量稳定性**：最后再跑 100+ 节点，观察总耗时、Provider cache hit 和退出恢复；确认同一个
    出口 IP 的多个节点没有重复消耗第三方额度。

## IP Intelligence 配置边界

支持以下环境变量（均为可选）：

```text
SPEEDBENCH_IPINFO_TOKEN
SPEEDBENCH_IPQS_KEY
SPEEDBENCH_SCAMALYTICS_USERNAME
SPEEDBENCH_SCAMALYTICS_KEY
SPEEDBENCH_SCAMALYTICS_REGION=eu|us
```

Scamalytics v3 必须同时提供 Username、Key 和账户对应 Region；缺 Region 时状态为
`configuration_incomplete`，不猜测 endpoint。Key 不得放在命令行、CSV、JSONL、SQLite、日志、URL、
浏览器 localStorage/cookie 或 API response；面板输入默认仅保存在当前 localhost backend 进程内。

SQLite cache 按 `provider + exit_ip` 去重：ip-api/基础 ASN/ISP TTL 7 天，Privacy/风险 TTL 24 小时。
Provider 的 `key_missing`、`timeout`、`rate_limited`、`quota_unavailable` 等状态只影响对应来源，
不应阻断节点测速。

分类报告必须遵守：`ISP/非托管` 不等于住宅；`Residential` 与 `Residential Proxy` 分开；多源
Hosting/Data Center 与 Residential 冲突时输出低置信度 Unknown/Conflict。SpeedBench IP Grade 是
启发式推荐，不是 IPQS/Scamalytics 官方评级，也不是实际诈骗概率。

## 已知风险与排查

- **未检测到 mihomo**：检查 `speedbench_workers.py` 的 Windows 路径候选、PATH 和配置文件，必要时
  使用 `--config-file`；不要提交真实配置或节点凭据。
- **取消后模式未恢复**：检查子进程的 `SPEEDBENCH_CANCEL_FILE`、哨兵路径和 Clash Controller；
  进程被强制 terminate 时 Python finally 不会执行，需手动恢复 Clash 模式后再报问题。
- **虚拟网卡识别错误**：用 PowerShell 查看实际默认路由接口名，按现有前缀表增加脱敏 fixture，
  不要在真机提交网络信息。
- **IP provider 不可用**：先看面板 Provider 状态和 cache；超时、配额、套餐字段缺失属于可选来源
  降级，不应让 IP 画像失败变成“干净”。
- **WebRTC 显示无法确认**：可能是 mDNS、浏览器隐私策略或 STUN 不可用；这不是“无泄漏”，也不是
  节点失败。DNS Guided Audit 同理。

## 发布前边界

本文件只规定本地验收，不执行 tag、push、merge 或 GitHub Release。发布 workflow 会在用户明确
创建并推送 `v*` tag 后运行；在此之前应先让主 Agent 完成本地全量测试、审查打包清单和安全负向测试。
