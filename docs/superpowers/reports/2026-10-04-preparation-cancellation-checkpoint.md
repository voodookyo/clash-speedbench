# 准备阶段取消传递检查点

日期：2026-10-04。承接目标推荐提交 `6a06382`，仅补 B 准备阶段已有
取消机制的缺口，不改控制器发现顺序、恢复写或工具/原生验收边界。

`_execute_benchmark` 的 connection 与 discovery span 进入同一个任务
取消作用域。原先此处的 /version、/configs、/proxies 与目录 provider
查询未传递 callback，可能等完请求超时才退出。现在复用既有请求的
绝对预算及 scoped DNS/connect/TLS/pipe/read 行为；scope 在准备结束
退出，不影响后续恢复路径。磁盘/内核清理仍不声明硬实时总截止。

新 `tests/test_prepare_cancel.py` 通过实际 localhost HTTP stalled body
分别进入这两个生产准备 span。取消后1秒内捕获 KeyboardInterrupt，
没有继续下一个请求，保留失败请求 metrics；fixture 在 finally 中释放
并回收。旧代码两项均 TimeoutError，加入作用域后通过。
整个测试只使用 owned fixture 与内存参数，不读取 Verge 或真实历史。

- Python3.14 的 prepare/transport/partial-history/progress 专项：46 tests，OK。
- Python3.12 的 prepare 专项：2 tests，OK。
- Python3.14.7 全量：973 tests / 32.232s，OK (skipped=6)。
- Python3.12.13 全量：973 tests / 32.276s，OK (skipped=10)。
- `git diff --check` 通过。跳过项仍是 Windows 原生与可选 PyYAML 边界，
  不能用本机全绿代替这些验收。

完整 B/C/D 尚未验收。接下去仍需同覆盖性能 fixture/限定实测、汇总与
其余取消边界审计、完整 C 页面/错误/可访问性、D 导入与电源/原生矩阵。
