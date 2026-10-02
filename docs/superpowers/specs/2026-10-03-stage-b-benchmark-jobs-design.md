# 阶段 B：任务模式、调度与实时结果

日期：2026-10-03。状态：待书面设计确认。依赖：阶段 A 身份/来源目录。

## 交付原则

在现有 Phase 1/2 上增加任务配置与事件协议，改善实际耗时和首次可用结果时间。跨节点带宽串行不变，降低覆盖率的模式不能宣称同精度提速。

## 模式与默认参数

| 模式 | probe | 带宽范围 | IP 范围 | 默认样本 |
|---|---|---|---|---|
| quick | 3 | Top 5 候选 | 已选精测候选 | 固定 10MB，单轮，3s 上限 |
| standard | 3 | Top 10 候选 | 已选精测候选，可选全部 | 保留自适应 warmup + 10~95MB，单轮 |
| deep | 10 | 全部连通节点 | 全部连通节点 | 自适应；默认单轮；多轮/多流由用户显式启用 |
| ip | 3 | 不测 | 全部连通节点 | 无下载样本 |

quick 的 jitter/失败率基于有限样本，UI 要显示 probe 数。IP 模式未测带宽 N/A，不能成为下载冠军。deep 仍需展示最大流量估计，用户确定后开始。

现有 CLI 不指定新模式时保持 legacy 行为与 Top 15 默认，避免脚本静默改变。新 CLI `--mode` 与新 UI 采用上述默认；显式高级参数覆盖模式默认，经同一校验器处理。参数允许范围由后端定义并返回，不依赖 HTML min/max。

Web 原 `/api/run` 继续兼容旧参数；版本化新任务接口同时接受 mode、目标 Profile、source/node 范围和高级选项。自动切换默认 false。

## 候选选择

legacy 保留延迟升序行为。新模式选择 deterministic 候选：先保证可用订阅/地区代表，再纳入近期开销与带宽表现；剩余名额按当前延迟填充。历史陈旧或配置身份变化时不得复用为当前成绩。

目标 Profile 影响候选策略和最终推荐，但不是更改原始指标；下载目标不被住宅偏好改变。quick/standard 不保证找到所有节点的绝对最快者，显示“已测范围内推荐”。

先用纯函数测试定义覆盖与 tie-break，避免引入复杂不可解释的模型。用户手动选节点/只测某订阅时优先遵守其范围，不扩到其他订阅。

## 计时与性能证据

用户等待计时从任务接收开始，使用 monotonic 时钟记录 duration，wall-clock 仅供时间戳。分别记录连接/发现、DNS、worker 启动、delay、exit-v4/v6、基础画像、provider/cache、warmup、下载、汇总、恢复/清理。

记录尝试数、成功数、覆盖范围、cache hit、实际下载字节及 worker 数，不记录完整 URL 和配置。新增 task_runs/task_metrics 表，不重写旧 runs.raw。

性能对比同时报告：首次结果、首次可用推荐、网络任务完成、完整情报完成、最终清理完成。保留原模式相同参数基线；30/100/300 节点 fixture 的可控调度测试不替代真实测速数据。

真实验收使用固定节点范围与流量上限，记录代理/TUN、网络、回退模式、冷/热缓存；高比例失败与 IPv6 不可用需单独列样本。未取得实测前不写“100 节点十几秒”等无条件承诺。

## 增量优化

1. 纯延迟阶段不必为所有节点提前解析所有 server 域名；需要启动 worker 时才做对应依赖闭包 DoH，保留 fake-IP 绕过机制。
2. 动态队列替代 round-robin 静态分片。每个 worker 只同时测一个节点；支持整个候选集合的安全配置/依赖加载，不能动态切到其未加载节点。
3. probe 和出口是小流量有限并发；不同子任务不能形成失控的乘法线程池。
4. 出口每个地址族独立 timeout/status；IPv4 可先发布，IPv6 pending 后补。worker 在所属出口请求结束或已取消后才能换节点，避免在途请求串到下一节点。
5. quick/standard 只对精测候选取得出口；all-IP 用户显式开启。IPv6 timeout 是此次探测不可用，不证明节点永不支持 IPv6或环境无泄漏。
6. 失败兜底次数按模式预算，独立保存主实例/worker 探测统计；不能将失败样本抹掉来改善失败率。
7. paid Intelligence 已有缓存/single-flight，继续复用。按出口完成后可启动去重任务，与带宽并行但有限并发。
8. 网络阶段可先发布终结结果，IP 情报仍 enrichment_pending；最终任务只有情报已完成/timeout 与资源已清理才终态。
9. Provider timeout/quota 均是正常降级，IP score N/A。取消需要实际停止在途 curl/worker，不仅取消 Future。

## 任务状态与事件协议

任务状态：queued → preparing → probing → measuring/enriching → finalizing → completed；任意活跃态可进入 cancelling → cancelled，异常进入 failed。IP 专项无 measuring，终态互斥且只产生一次。

事件使用 version、job_id、seq、timestamp、type、phase、node_id、payload。类型至少包含 job_started、phase_started、node_probe、node_exit、node_measurement、node_intelligence、phase_finished、job_finished、job_cancelled、job_failed。

日志继续供 CLI 人读，但前端不解析日志获得业务状态。只发布白名单结果字段，避免临时 worker 配置/异常凭据泄漏。

后端先实现有界事件缓冲和基于 since_seq 的增量读取；实时 SSE 在同一状态源上实现，支持 Last-Event-ID。缓冲过期则返回 snapshot/resync，不无限保存每行日志。不额外新增 websocket Python 依赖。

重载页面通过 job_id/snapshot 续接；服务重启视为已中断，不能用遗留 lock 文件误认仍运行。老 status 接口由事件状态适配，保留旧 CLI/Web 扩展兼容。

## 历史与评分

新增 task_runs(job_id, mode, target_profile, status, started_at, finished_at, config_json, run_id) 与 task_metrics(job_id, phase, duration_ms, attempts, successes, bytes, counters_json)。config_json 严格白名单，不能保存命令行环境快照或文件路径内容。

每条新结果包含 measurement_scope、probe/bandwidth/intel 独立状态、更新时间、已测指标数。Network/Overall 仍按有效维度计算，但 UI 比较与推荐优先相同覆盖范围，不能让“只测延迟”的重归一化分数压过完成带宽精测者。

取消保留已完成记录，标记 partial；失败/未测/timeout 独立于网络不可达。重复事件或恢复任务不会重复插入同一节点结果。

## 验收

- 同一输入下 CLI/Web 的模式默认、覆盖和限制一致；旧 CLI 无 mode 的默认不变。
- 不支持参数、NaN/Infinity、超大 probe/worker/rounds/body 被拒绝。
- 慢任务 fixture 验证动态队列、独立预算及没有在途串节点。
- 模拟 API 延迟/限额，网络结果已发布且最终 N/A 合理；同 IP provider 仍只查一次。
- 同时下载的不同节点数始终 ≤1；多流只属于同一节点。
- seq 单调、重连/resync、乱序/重复、终态唯一、刷新恢复、服务重启处理。
- probe 失败统计完整；IP 未检测与查询失败不同；候选策略可解释。
- 取消发生于 DNS/启动/probe/出口/下载/情报/汇总均能回收本任务资源，不伤其他进程。
- 实际字节与预算估计不混淆，phase metrics 覆盖最初准备和最后清理。
- 专项和全量测试通过；完成相同覆盖基线比较才报告真实提速。
