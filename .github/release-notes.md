## v0.9.0：订阅维度历史回顾

- **新增「🧭 订阅」视图**：按订阅（机场）聚合的稳定性看板——
  测速轮次、可用率、中位速度、中位延迟、平均分、最近测速时间一览；
  点进单个订阅可看可用率/速度/评分随时间的三条趋势线，
  以及该订阅各节点最近一轮表现，长期回顾订阅质量一目了然
- **订阅来源入库**：默认的两阶段并发测速模式现在也记录每个节点的
  订阅名（取自 Mihomo `/proxies` 的 provider-name），此前该字段恒为空；
  节点表格新增「订阅」列，搜索框同时支持按订阅名过滤
- **节点稳定身份**：新增 `node_key`（proto|server|port 哈希），
  机场给节点改名后历史趋势仍能续上；`/api/node?key=` 支持按身份查询
- **失败原因分类**：历史记录新增 `fail_reason`
  （timeout / no_data / http_error / connect_error / switch_failed），
  可用率低时能看出是超时还是连不上
- SQLite 历史库带幂等迁移自动升级，旧数据完整保留；
  无 provider 的旧数据归入「（未知订阅）」，照常可查
- 新增 20 个订阅维度单元测试，全套 366 个测试通过

## 安装

### macOS（12+）

下载 `Clash-SpeedBench-v0.9.0-macos.zip`，解压后把 `Clash SpeedBench.app`
拖进「应用程序」；首次打开需**右键 → 打开**（未做付费开发者签名，Gatekeeper 只拦一次），
需要系统里有 `python3`。

### Windows（10/11）

下载 `Clash-SpeedBench-v0.9.0-windows.zip`，解压后双击 `SpeedBench.bat`
（需要 Python 3.9+，未安装会自动引导到 Microsoft Store 安装，无需管理员权限）。
面板启动后除了浏览器页面，右下角托盘也会出现图标，随手点开。

校验下载完整性：对比各 zip 同名 `.sha256` 文件
（macOS/Linux 用 `shasum -a 256`，Windows 用 `Get-FileHash -Algorithm SHA256`）。

## 这是什么

给正在运行的 Clash Verge Rev / Mihomo 加「节点体检」：延迟（与 Verge 同口径）+
真实带宽（两阶段：并发粗筛 → Top N 串行精测）+ 出口 IP 画像 + 综合评分，
附带本地 Web 面板（排序表格 / 一键切换 / 历史趋势 / 订阅回顾 / 评分 Profile）。
零第三方依赖，纯 Python 标准库 + 系统 curl。
