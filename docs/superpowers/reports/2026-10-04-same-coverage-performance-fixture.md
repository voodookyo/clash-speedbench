# 相同覆盖调度 fixture 检查点

日期：2026-10-04。生产源码起点：`3f38f21`；本批只新增测试工具与专项测试，不修改运行时调度。B 的真实流量与跨平台验收、C/D 完整交付仍未完成。

## 证据边界与复现

`python3 -m tests.performance_fixture` 输出 JSON 对照；默认 30／100／300 节点，各跑 cold、hot、failure、ipv6_unavailable，每个场景分别运行 legacy 静态分片与 deep 动态队列，共 24 次任务。每次使用独立临时目录，任务结束后删除；不读取用户配置、历史、seed 或 provider Key，不启动 Mihomo。socket 与 subprocess 创建被禁用。

生产代码实际执行：10 路主实例 probe pool、失败节点的独立 worker probe、worker 节点独占、双地址族出口池、单 worker 逐节点下载、去重情报池、SQLite cache、事件编码/解析与 JobStore、结果 journal、CSV/JSONL 报告、raw 镜像导入、task metrics/五里程碑留存。连接/发现是 fixture API 方法，DNS、进程启动/停止、主实例 delay、出口和下载传输、IPQS 响应为合成边界。清理只证明 fixture Worker 对象及真实 enrichment Future 结束，**不证明 native 子进程回收**。

相同参数：全部连通节点精测、全部连通节点出口、每条 probe 路径 10 次、workers=3、intel_workers=2、固定 10MB 单轮、max_time=3s、settle=0、multi=false。未比较 legacy Top15 与缩小范围的 quick/standard。比较前校验节点、精测集合、两个地址族覆盖、主/worker 独立样本、失败数、传输调用数和注入字节一致。

慢节点按 index%3=0 分布，静态分片的一个 worker 承担全部慢节点；动态队列可以分担。IPv4/IPv6 传输分别注入 4/16ms 的慢调用，其他调用 2ms。failure 为 70% 节点主/worker 全失败，剩余节点中 index%10=7 下载失败并返回 1.25MB，另有 provider timeout。ipv6_unavailable 为 IPv6 返回失败、IPv4 独立成功。hot 在任务接收前预填相同去重出口的缓存；准备耗时不计入任务等待，cold 每次从空缓存开始。

所有时间来自真实 monotonic elapsed，包含实际 fixture sleep、生产 Python 调度、协议和 SQLite/报告成本。只有单次运行，无统计置信区间，不能将构造的慢分片优势外推到真实网络或其他节点分布。**下载字节是注入 curl 返回值，真实下载流量为零；不是请求预算，也不是网络实测。** 每阶段并行 span 是累计服务时间，不能相加当 wall time。

## 本机观测

Python 3.14.7 与 3.12.13，runtime platform 输出 `macOS-26.6.2-arm64`。表中单位为 ms，每格 `legacy_static / deep_dynamic`；从任务接收起计时。

| 节点/场景 | 首次结果 | 首次可用推荐 | 网络完成 | 完整情报 | 清理完成 |
|---|---:|---:|---:|---:|---:|
| 30 cold | 4 / 4 | 247 / 153 | 329 / 234 | 345 / 252 | 387 / 307 |
| 30 hot | 5 / 6 | 268 / 176 | 357 / 264 | 358 / 265 | 418 / 337 |
| 30 failure | 4 / 4 | 144 / 121 | 165 / 141 | 232 / 209 | 260 / 240 |
| 30 ipv6_unavailable | 4 / 4 | 320 / 205 | 413 / 296 | 414 / 296 | 453 / 346 |
| 100 cold | 4 / 4 | 863 / 476 | 1200 / 804 | 1201 / 806 | 1349 / 994 |
| 100 hot | 6 / 5 | 903 / 486 | 1218 / 797 | 1219 / 798 | 1346 / 983 |
| 100 failure | 4 / 4 | 458 / 343 | 543 / 426 | 562 / 445 | 630 / 525 |
| 100 ipv6_unavailable | 4 / 4 | 1022 / 651 | 1372 / 980 | 1373 / 981 | 1477 / 1103 |
| 300 cold | 4 / 4 | 2560 / 1419 | 3660 / 2536 | 3665 / 2539 | 4079 / 3038 |
| 300 hot | 6 / 5 | 2585 / 1449 | 3697 / 2522 | 3698 / 2526 | 4126 / 3031 |
| 300 failure | 4 / 4 | 1388 / 1011 | 1683 / 1286 | 1685 / 1288 | 1900 / 1513 |
| 300 ipv6_unavailable | 4 / 4 | 3045 / 1925 | 4144 / 3007 | 4148 / 3010 | 4398 / 3356 |

上述为 3.14 的一轮结果。3.12 的同覆盖 12 组也通过：300 cold 五项为 `4/4, 2560/1430, 3681/2446, 3686/2449, 4076/2952`；300 hot 为 `29/6, 2598/1455, 3672/2515, 3677/2519, 4059/3034`；300 failure 为 `4/4, 1437/1015, 1734/1290, 1737/1291, 1928/1512`；300 IPv6 不可用为 `4/4, 3053/1934, 4114/2888, 4120/2890, 4370/3197`。首次结果的变动也说明此单次对照不能承诺固定耗时。

覆盖和调用：cold/hot 每场精测 N 个、v4/v6=N/N，cold 查询 16 个去重出口，hot 为 16 cache hits、0 provider 传输调用；failure 精测 9/30/90 个、v4/v6 同数，分别注入 63.75/212.5/637.5MB，保留所有 N 条结果及主/worker 各 10 个失败样本；IPv6 不可用精测 N 个、v4/v6=N/0、provider 查询 8 个去重 IPv4。正常样本注入 N×10MB；这些 MB 都是合成返回值。

## 专项验收与 delegation

- `python3 -m unittest tests.test_performance_fixture -q`：7 项通过，0.812s。
- `/opt/homebrew/bin/python3.12 -m unittest tests.test_performance_fixture -q`：7 项通过，0.838s。
- 专项还检查全失败时首次可用推荐为 N/A、不同工作量拒绝比较、未加载/在途节点切换禁止、同时下载节点数≤1，main pool≤10、worker probe/各出口 family≤3、provider≤2，回收及持久化 metrics/里程碑/raw 一致。实际传输字节仍须真实验收。
- 本批使用既有本地 `repo-search --read-only` 路由（DeepSeek flash/OpenCode）完成 D 历史导入公开源码分析。仅采纳经控制器核实的线索；其中 whole-file digest/备份哈希建议不符合项目禁哈希规则，拒绝。其 proposed “requirement” 不替代已批准规格。旧历史按插入 id 排序的影响须在后续导入实现中处理。
- 本地 `review-k3 --read-only` 的两次路由内尝试均为 timeout/124（09:58:12–10:03:12、10:03:12–10:08:12，UTC+8），顶层进程退出 1，无审阅结论。没有再次调用失效路由或修改认证，改由只读 Codex 子代理独立审阅本批公开 fixture；没有委派写操作。
- 独立审阅发现两处实际漏检：丢弃 family 回调后 pending 状态仍可能通过；情报 apply 缺失或给 timeout 节点伪造 100/S 时仍可能通过。控制器采纳并补齐每个已测节点的最终 family/provider 状态、timeout 的 N/A 和未知标志、成功情报有效分数，以及协议 snapshot、持久化 task snapshot、原始 JSONL 三处结果一致性。再次独立验证：7 项通过，丢弃 family 回调、no-op enrichment、伪造 timeout 节点 clean score 三种注入均被两种策略拒绝，无剩余阻断意见。
- 最终断言下，3.14/3.12 各自再次跑完默认 12 对／24 次任务，均退出 0。该次两版本并行运行只作通过性验证，不用于耗时对照；上表保留此前分别串行运行的观测。新增最终值检查在 cleanup/terminal elapsed 固定之后执行，未改变上表对应的生产调度、传输参数或计时路径。`git diff --check` 通过。

## 尚未达到的门槛

固定节点真实流量对照仍待既已提出的授权；工具安装、Windows/macOS Intel/Linux 机器/CI 访问尚未获得答复。Python3.9、六格 OS/Python 矩阵、真实原生 worker/WebView/安装包生命周期未验。本机合成 fixture 与此前 portable 测试不能补齐这些门槛，因此 B/C/D 整项保持未完成。
