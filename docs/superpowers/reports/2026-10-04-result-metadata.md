# B/C 结果指标元数据

规格依据：stage-b-benchmark-jobs-design 的新结果独立状态、更新时间、已测指标数，以及 stage-c-shared-ui-design 的 Network/IP Grade 分开更新、旧历史未知展示。本次补足这部分合约，不改变测量参数、评分、候选范围、预算、并发和数据库 schema。

## 实现

新结果序列化包含 measurement_scope、metric_updated_at 和 measured_metric_count。时间为仅用于展示的 epoch 毫秒；probe 在实际观测时记录，重建最终 Result 保留原观测时间；带宽和出口分别在相应调用边界记录。情报只在实际附着查询结果时记录，其完成或评分不推进 Network 时间；真实有效等级才记录 IP Grade 时间。禁用／缺 Key 不虚构完成时间，全失败 probe 不显示成功。

已测指标数最多九项，只计有效原始观测：延迟、抖动、建连、单流中位带宽、多流带宽、有完成样本的探测失败率、有效且地址族匹配的 IPv4/IPv6、附着情报的有效 IP quality。派生 Network/Overall/grade 不重复计数，旧格式成功 IP profile 的可核验 IPv4 仍计入。取消与中断的地址族状态保留。

ResultJournal 合并 partial 与最终行时保留独立状态、非倒退时间及真实探测计数，最终对象和事件/JSONL 序列化保持一致。任务投影与 slim_history 只返回白名单字段，拒绝布尔、浮点、负值、极大时间及未知字段。历史读取不补猜缺失时间或计数，不重写旧 raw。共享 UI 详情分别显示 Network/IP Grade 更新时间、探测／带宽／情报状态、已测指标数及 IPv4/IPv6 状态；旧字段缺失显示“未知”，动态文字转义。

## 委派与整合

使用用户指定的本地 normal-code --write，任务 bdc2a404-a756-4e70-83bb-2087a882cd1c 从 clean committed a164efa 启动 Git worktree。GLM、MiniMax 超时后，DeepSeek flash 第二次执行成功；源 HEAD/status unchanged、write_isolated=true、自动 commit/merge/push=false。结构化 tests 为 unknown，worker 文字测试声明未作为验收结果。

Codex 在 worker 结束后的隔离区审阅并修正真实观测时间、独立状态、旧历史未知、计数有效性及 partial→final 合并，再手动应用到已提交切换确认的源树。没有 worker 直接写源树，没有自动合并、提交或推送。未安装依赖或修改 ai-dev、凭据、网络配置。

## 实测与修正

- 隔离区最初 37 项中一项 fixture 错把无 provider 状态的情报当作已完成，改为真实返回状态；后续 75 项通过。一次控制器改动使用重复 dict keyword 造成 4 failures/18 errors，已改为字典合并，78 项随后通过。这些失败未作为通过记录。
- 新增 27 项元数据测试，覆盖真实 probe 时间、无请求／失败／双栈 partial、无 progress 的串行 CLI 历史、worker 带宽、事件投影、旧历史、非法时间、计数及 journal 合并。隔离区含 UI 的 68 项通过；整合后 Python3.12 的 140 项专项通过。
- 首次源树 Python3.14 全量 1185 项有一项 failure、一项 error：旧历史字段集合断言未包含新增合约，以及旧 JS 切换测试仍假定直接切换。更新为新增字段和 preview→确认协议后，相关 42 项通过；未放宽实际切换检查。
- 最终冻结功能 diff：Python3.14.7，`python3 -m unittest discover -s tests -q`，1185 tests / 68.396s，OK (skipped=6)。Python3.12.13，`/opt/homebrew/bin/python3.12 -m unittest discover -s tests -q`，1185 / 68.389s，OK (skipped=10)。跳过不构成 Windows pipe、原生或其他平台通过。
- node --check 与 git diff --check 通过。实际 loopback 合成浏览器快速任务完成并展开长名称详情；缺少新元数据的旧格式 fixture 正确显示“未知”，已有 IPv4/IPv6 独立状态仍展示。截图 `/tmp/clash-speedbench-result-metadata-final.jpg`；新字段值展示另由 JS 回归核对。未用合成状态证明真实出口或原生 WebView。

全部 controller、下载、provider 和历史使用临时或合成输入，不访问真实 Verge、不测速真实节点、不查询付费 API、不改用户历史。B/C/D 整项仍未验收：真实同覆盖性能、Windows pipe 真机、Python3.9/平台矩阵、原生 WebView／休眠／安装包生命周期依赖待答授权与对应环境。无独立外部只读审阅成功记录。
