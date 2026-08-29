# Clash SpeedBench IP Intelligence 与环境泄漏检测升级设计

日期：2026-08-29

基线：`master` / `v0.9.1` / `40ad5eb`

目标版本：`v1.0.0`

## 1. 目标与非目标

本次升级把 Clash SpeedBench 从“节点延迟、带宽和基础 IP 画像”扩展为“节点网络性能测试、多源 IP Intelligence、IP 风险评估和客户端环境泄漏检测”。

必须保留现有架构和行为：

- Phase 1 并发初筛与 Phase 2 严格串行精测；
- 临时 Mihomo worker、Clash Verge Controller、节点自动恢复；
- 延迟、jitter、TCP/TLS connect、单流与多流下载；
- JSONL 历史、SQLite 查询镜像、Web UI；
- Windows、macOS、Linux 和 Python 3.9/3.12；
- Python 标准库加系统 curl，不新增 pip dependency。

本次不重写为 Python package，不替换测速端点，不改变 worker 隔离模型，也不把 DNS/WebRTC 结果错误归为离线测速节点的固有属性。

## 2. 现状与兼容约束

当前 `classify_ip()` 正确地把 `proxy=False`、`hosting=False`、`mobile=False` 归为中性的“ISP/非托管”，而不是住宅。本设计保持该原则。

当前 `ip_flag_score()` 对 IP 查询失败返回 100，必须删除这一语义。Unknown 不等于 Clean，数据不足时 IP Quality Score 和 Grade 均为 `N/A`。

`runs.raw` 是原始 JSONL 回放来源，必须保留。新字段只做向后兼容扩展；旧行缺少 Intelligence 字段时按 `N/A` 展示，不自动消耗付费 API 回填。

## 3. 模块边界

保留平面模块结构，新增两个文件：

```text
clash_speedbench.py          CLI、串行测速、结果模型和输出，增量接入
speedbench_workers.py        Phase 1/2、worker、出口双栈和探测统计
speedbench_db.py             兼容 schema、cache 和信誉时间线
speedbench_web.py            localhost API、安全设置和 leak API
web/                         主表、详情、设置和 #/leak

speedbench_ip_intel.py       provider、标准化、聚合、分类和 IP Grade
speedbench_leak.py           WebRTC candidate 判断与 leak audit 模型
```

`clash_speedbench.py` 不直接包含任何厂商字段解析。Provider 失败只生成独立状态，不得让测速失败。

## 4. 执行流程

```text
Phase 1 节点探测
  ├─ 延迟、jitter、应用层探测成功率
  ├─ IPv4 出口发现
  └─ IPv6 出口发现（失败为 unavailable）
          ↓
按 exit IP 去重
          ↓
SQLite cache 查询
          ↓
cache miss 启动最多 2～4 个 Intelligence worker
          ↓
Phase 2 原有单流/多流严格串行运行
          ↓
合并 Intelligence、分类和评分
          ↓
JSONL、SQLite、CLI 和 Web UI
```

Intelligence 查询在 Phase 1 排名完成后启动，并尽量与 Phase 2 重叠。30 个节点只有 8 个出口 IP 时，每个付费 provider 最多查询 8 次。

## 5. Provider 契约

```python
class IpIntelProvider:
    name: str
    ttl_seconds: int

    def query(self, ip: str) -> ProviderResult:
        ...
```

实现：

- `IpApiProvider`
- `IpInfoProvider`
- `IpqsProvider`
- `ScamalyticsProvider`

`ProviderResult.status` 限制为：

```text
ok
cache_hit
key_missing
configuration_incomplete
timeout
rate_limited
quota_unavailable
unsupported_tier
invalid_response
error
```

结果保存 provider、查询 IP、获取/过期时间、标准化字段、脱敏后的原始响应和不含 URL/Key 的错误代码。

### 5.1 ip-api

继续提供 country、ISP、organization、ASN、AS name、mobile、proxy 和 hosting。遵守免费 endpoint 的限额头；查询失败不影响其他来源。

### 5.2 IPinfo

使用当前 `api.ipinfo.io/lookup/{ip}` 响应。解析字段按账户套餐实际存在性处理：ASN、AS name/type、hosting、mobile、proxy、VPN、Tor、relay、residential proxy 和地理字段。字段缺失表示不可用，不等价于 `false`。

### 5.3 IPQS

使用官方 Proxy & VPN Detection API，解析 `fraud_score`、`proxy`、`vpn`、`tor`、`ISP`、`organization`、`ASN`、`connection_type`、`recent_abuse`、`abuse_velocity`、`bot_status` 和 `mobile`。

IPQS 没有官方 `residential_proxy` 字段时不得虚构。仅在 Residential connection type 与 proxy 信号同时存在时生成标明“推断”的住宅代理证据。

### 5.4 Scamalytics

使用 v3 官方接口：

```text
GET {region endpoint}/v3/{username}?key=...&ip=...
```

解析 `scamalytics_score`、`scamalytics_risk`、ISP score/risk、datacenter、VPN、外部 blacklist 和官方响应中实际存在的 proxy/server 标志。付费字段占位字符串不得转换为布尔真值。

配置：

```text
SPEEDBENCH_SCAMALYTICS_USERNAME
SPEEDBENCH_SCAMALYTICS_KEY
SPEEDBENCH_SCAMALYTICS_REGION=eu|us
```

Region 缺失时返回 `configuration_incomplete`，不猜测账户节点。

## 6. 聚合模型

```python
@dataclass
class IpClassification:
    category: str
    confidence: int
    evidence: List[str]
    conflicts: List[str]

@dataclass
class IpIntelligence:
    ip: str
    ip_version: int
    country: Optional[str]
    asn: Optional[str]
    as_name: Optional[str]
    isp: Optional[str]
    organization: Optional[str]
    hosting: Optional[bool]
    proxy: Optional[bool]
    vpn: Optional[bool]
    tor: Optional[bool]
    mobile: Optional[bool]
    residential_proxy: Optional[bool]
    connection_type: Optional[str]
    ipqs_fraud_score: Optional[int]
    ipqs_recent_abuse: Optional[bool]
    ipqs_abuse_velocity: Optional[str]
    scamalytics_score: Optional[int]
    scamalytics_risk: Optional[str]
    scamalytics_datacenter: Optional[bool]
    scamalytics_blacklisted: Optional[bool]
    classification: IpClassification
    ip_quality_score: Optional[float]
    ip_grade: Optional[str]
    provider_status: Dict[str, str]
```

具体字段以实现时的官方响应为准；模型允许字段缺失，不为满足示例而虚构数据。实现使用 `typing.Optional/List/Dict`，避免引入 Python 3.10 才支持的 `X | None` 语法。

## 7. Cache 与 SQLite

新增表：

```sql
CREATE TABLE ip_intel_cache (
    provider TEXT NOT NULL,
    ip TEXT NOT NULL,
    fetched_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    raw_json TEXT NOT NULL,
    normalized_json TEXT NOT NULL,
    PRIMARY KEY (provider, ip)
);

CREATE TABLE ip_intel_results (
    run_id INTEGER NOT NULL,
    exit_ip TEXT NOT NULL,
    ip_version INTEGER NOT NULL,
    classification TEXT NOT NULL,
    confidence INTEGER NOT NULL,
    ip_quality_score REAL,
    ip_grade TEXT,
    evidence_json TEXT NOT NULL,
    conflicts_json TEXT NOT NULL,
    provider_status_json TEXT NOT NULL,
    normalized_json TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (run_id, exit_ip)
);

CREATE TABLE leak_audits (
    id INTEGER PRIMARY KEY,
    created_at INTEGER NOT NULL,
    exit_ipv4 TEXT,
    exit_ipv6 TEXT,
    webrtc_status TEXT NOT NULL,
    candidates_json TEXT NOT NULL,
    dns_mode TEXT NOT NULL,
    dns_status TEXT,
    details_json TEXT NOT NULL
);
```

TTL：

- ip-api、ASN、ISP 基础数据：7 天；
- IPinfo Privacy、IPQS、Scamalytics 风险数据：24 小时。

TTL 为可覆盖常量。`(provider, ip)` 使用原子 upsert，同一进程增加 single-flight，保证相同 IP 的多个节点只调用一次。

`ip_intel_results` 每轮每出口 IP 一行；节点通过 exit IP 引用，不重复存 provider 原始结果。信誉变化从连续 run 动态计算，无需破坏现有 `ip_profiles`。

## 8. Evidence-based 分类器

内部分类固定为：

```text
residential
residential_proxy
corporate
mobile
datacenter
vpn_proxy
unknown
```

证据分为强肯定、弱支持和冲突：

- 明确 Residential connection type 是强住宅证据；
- `hosting=False`、`datacenter=False`、ISP ASN 只是弱支持；
- IPinfo residential proxy 是强住宅代理证据；
- IPQS Residential + proxy 是注明推断来源的住宅代理证据；
- Hosting、Data Center、Scamalytics datacenter 是强数据中心证据；
- 明确 VPN、Proxy、Tor 是强代理证据；
- ASN/Organization 文本匹配只能作为弱证据，不能单独分类。

优先顺序：

1. 住宅网络加住宅代理行为 → `residential_proxy`；
2. VPN/Proxy/Tor → `vpn_proxy`；
3. Mobile → `mobile`；
4. 多源 Hosting/Data Center → `datacenter`；
5. 多源住宅且无代理/托管冲突 → `residential`；
6. Corporate/Business 且非 hosting → `corporate`；
7. 数据不足或强冲突 → `unknown`。

住宅高置信度至少需要一个明确住宅信号和另一个独立来源的兼容证据。三个 ip-api 布尔标记全 false 永远不能直接推出住宅。

置信度：

```text
90～100  三个独立来源强一致且无冲突
75～89   两个独立来源强一致
55～74   一个强信号加兼容弱证据
0～54    数据不足或强冲突
```

IPQS Residential 与 IPinfo Hosting/Scamalytics Datacenter 冲突时分类为 `unknown`、低置信度，并同时保留 evidence 和 conflicts，不擅自选边。

## 9. IP Quality Score 与 Grade

IPQS 和 Scamalytics 的原始 fraud score 分别显示，绝不简单平均并称为真实风险。

只有至少一个具有实质风险字段的 provider 成功时才生成 SpeedBench IP Quality Score。只有 ip-api 或全部 provider 失败时：

```text
IP Quality Score = N/A
IP Grade = N/A
```

启发式评分采用“最坏官方风险档位 + 去重后的风险事实”：

- 最高 fraud score 风险档位；
- blacklist；
- recent abuse / abuse velocity；
- bot status；
- proxy、VPN、Tor；
- residential proxy；
- hosting/datacenter；
- 多源冲突。

同一 proxy 事实被多个 provider 报告时只扣一次。干净 Datacenter 只做轻度影响，不等同高风险。

Grade：

```text
90～100  S
75～89   A
60～74   B
40～59   C
0～39    D
```

Blacklist、极高风险或严重近期滥用最高只能为 D；高风险 fraud score 或近期滥用最高只能为 C。README 必须说明 Grade 是 SpeedBench 启发式推荐，不是厂商官方评分或诈骗概率。

## 10. Network 与 Overall Score

Network Score 仅在存在有效带宽结果时生成。权重：

```text
单流下载          35
多流下载          15
延迟              20
jitter            10
TCP/TLS connect    10
应用层探测成功率  10
```

未启用多流时，在其他有效网络维度间归一化。保持现有单流 100 Mbps 和延迟曲线；多流满分标尺为 4 × 100 Mbps。jitter、connect 使用独立、可测试的分段函数。

```text
IP Quality 可用：Overall = Network × 80% + IP Quality × 20%
IP Quality 不可用：Overall = Network
```

未知 IP 不获得 100 分；UI 明示 N/A。IP 清洁和住宅 Profile 将未知排在已验证清洁结果之后。

## 11. 稳定性探测

默认保持 3 次，`--stability` 在未显式指定时改为 10 次，`--probe-count N` 可覆盖。单次失败继续下一次，不再 break。

保存：

```text
probe_attempts
probe_successes
probe_failures
probe_success_rate
probe_loss_pct
```

延迟为成功样本中位数，jitter 只基于成功样本。全部失败时延迟/jitter 不可用。CLI、README 和 UI 必须称其为 HTTP/HTTPS application-level probe failure rate，而不是 ICMP 或物理链路 packet loss。

## 12. IPv4 与 IPv6

使用无 Key 的 `api.ipify.org` 和 IPv6-only `api6.ipify.org` 发现出口地址，再按明确 IP 查询 provider。ipify 失败时保留现有 ip-api self lookup 作为 IPv4 回退。

每个节点保存：

```text
exit_ipv4
exit_ipv6
intel_ipv4
intel_ipv6
```

IPv6 不可用是正常结果。IPv4/IPv6 的国家、ASN 或分类明显不一致时标记“节点双栈出口不一致”，但不称为客户端泄漏。客户端绕过仅由独立 Leak Audit 判断。

## 13. 环境泄漏检测

新增 `#/leak` 页面，固定声明检测对象是当前浏览器、当前 Clash/TUN 和当前活动节点，不是离线节点属性。

### 13.1 WebRTC

浏览器使用 `RTCPeerConnection` 和 ICE/STUN，收集 host、srflx、prflx、relay。公网候选提交 localhost backend，用标准库 `ipaddress` 判断并通过基础画像识别国家/ISP。

- 私网、loopback、link-local 不算公网泄漏；
- `.local` mDNS 不解析为真实 LAN IP；
- 公网 candidate 与浏览器出口不一致时标红；
- 中国公网、China Unicom 或未经代理的 IPv6 标红；
- STUN 失败、策略阻止、只有 mDNS 或缺少可比出口时显示“无法确认”；
- 完整采集且无异常时只显示“未发现明显泄漏”，不显示绝对“无泄漏”。

### 13.2 DNS

本版采用 Guided DNS Leak Audit，打开 BrowserLeaks DNS 与 DNSLeakTest，并提供 resolver 地域/运营商判读指南。不得抓取网页、读取系统 DNS 后声称安全。

预留 `DnsLeakProvider.start_audit()` 和 `poll_result()`，未来接自建 authoritative DNS observer。用户观察结果可保存到本地 `leak_audits`。

## 14. Web UI 与 Profile

主表限制为：

```text
节点 | 延迟 | 带宽 | Network | IP Grade | IP 类型 | 风险 | 标签
```

详情展示完整网络指标、IPv4/IPv6、每个 provider 原始标准化字段、Classification、Confidence、Evidence、Conflicts 和 provider/cache 状态。

增加仅内存的 Intelligence 设置。GET 只能返回 configured 布尔和状态，Key、token、用户名不得回传。浏览器不得把 Key 写入 localStorage、cookie 或 URL。

保留综合、日常、下载和 IP Profile，增加住宅优先。下载 Profile 不受 IP 风险排序影响。

## 15. 密钥与请求安全

环境变量：

```text
SPEEDBENCH_IPINFO_TOKEN
SPEEDBENCH_IPQS_KEY
SPEEDBENCH_SCAMALYTICS_USERNAME
SPEEDBENCH_SCAMALYTICS_KEY
SPEEDBENCH_SCAMALYTICS_REGION
```

Web Key 只 POST 到 token/Host/Origin 校验后的 localhost backend，默认仅驻留内存，通过子进程临时环境传递。Key 不得进入 CLI 参数、日志、JSONL、SQLite、HTTP response、前端状态或异常字符串。

Provider 不记录完整请求 URL，不跟随跨 host 重定向。所有外部请求使用官方 HTTPS 和系统证书验证。写出数据前执行递归敏感字段和值脱敏。

## 16. 历史信誉

通过 `node_key + exit_ip + ip_intel_results` 展示出口 IP、ASN、Classification、Grade、IPQS/Scamalytics 风险的跨轮变化。同一 IP 从 Residential 变为 Residential Proxy 或 fraud score 明显上升时显示信誉恶化警告。

## 17. 测试与 CI

新增：

```text
test_ipinfo_provider.py
test_ipqs_provider.py
test_scamalytics_provider.py
test_ip_classifier.py
test_ip_cache.py
test_ip_grade.py
test_probe_loss.py
test_leak_api.py
```

覆盖 provider 超时/429/quota/套餐缺字段、八个指定分类与安全场景、single-flight、双栈不一致、private/mDNS、STUN unknown、旧 JSONL 回放和 Key sentinel 全存储扫描。

CI 不调用真实 API，全部使用 mock fixture。每阶段运行新增测试、完整 unittest 和 compileall。GitHub Actions 保持 Windows/macOS/Linux × Python 3.9/3.12，release workflow 纳入新增模块。

## 18. 分阶段提交与 Agent 所有权

提交拆分：provider abstraction、各 provider/cache、classifier/grade、score 修复、probe/IPv6、DB history、Web UI、leak audit、docs/tests/release。

编码和测试使用 `gpt-5.6-luna`、`max`：

1. `intel_core`：`speedbench_ip_intel.py` 和 provider/classifier/grade/cache 测试；
2. `network_db`：`clash_speedbench.py`、`speedbench_workers.py`、`speedbench_db.py` 和网络/历史测试；
3. `web_leak`：`speedbench_web.py`、`speedbench_leak.py`、`web/` 和 Web/leak/security 测试；
4. `acceptance_test`：只读回归、安全负向测试、旧历史回放和跨平台静态审计。

后续 Agent 不得回退他人修改。主 Agent 在每阶段检查 diff、接口、测试和兼容性，最终独立验收。

## 19. 迁移与限制

v0.8.2/v0.9.1 可直接升级。启动时仅创建新表或增加兼容列，不删除或重写 `runs.raw`。建议用户先备份 JSONL 和 `.db`。

无 Key 时完整测速继续工作并退化为 ip-api 基础画像；IP Quality 为 N/A。

已知边界：

- provider 字段取决于账户套餐，缺失不能解释为 false；
- Intelligence 有 TTL，短期信誉变化可能在缓存期内不可见；
- 分类与 Grade 是启发式结果；
- WebRTC 受浏览器隐私策略和 STUN 可用性影响；
- DNS 本版为 Guided Audit，不自动声称无泄漏；
- 节点 IPv6 支持与客户端 IPv6 绕过是两个独立指标；
- 在不 push 的前提下只能本地实测 Windows，macOS/Linux 的真实远端 Actions 结果需后续用户推送分支后产生。
