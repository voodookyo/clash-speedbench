## v0.9.1：Windows 不再驻留控制台窗口

- **修复**：Windows 端双击 `SpeedBench.bat` 后任务栏常驻一个最小化
  Python 控制台窗口的问题。面板改用 `pythonw` 无窗口启动
  （`pythonw` 缺失时自动回退原来的最小化控制台兜底），
  面板日志写入 `%APPDATA%\ClashSpeedBench\web.log`，异常仍可排查
- **取消机制换代**：「中断测速」不再依赖控制台信号
  （CTRL_BREAK_EVENT 需要控制台存在，是旧设计的根因），
  改为哨兵文件（`SPEEDBENCH_CANCEL_FILE`）——测速核心在节点/轮次
  间隙检查，中断后走与 Ctrl+C 完全相同的优雅退出路径，
  Clash 策略组/运行模式照常自动恢复；macOS/Linux 行为不变
- 退出入口不变：托盘图标右键「退出 SpeedBench」或面板里的退出按钮
- 全套 369 个单元测试通过（win32 取消路径已重写为哨兵语义的 mock 覆盖）

## 安装

### macOS（12+）

下载 `Clash-SpeedBench-v0.9.1-macos.zip`，解压后把 `Clash SpeedBench.app`
拖进「应用程序」；首次打开需**右键 → 打开**（未做付费开发者签名，Gatekeeper 只拦一次），
需要系统里有 `python3`。

### Windows（10/11）

下载 `Clash-SpeedBench-v0.9.1-windows.zip`，解压后双击 `SpeedBench.bat`
（需要 Python 3.9+，未安装会自动引导到 Microsoft Store 安装，无需管理员权限）。
bat 窗口闪过即关，无常驻控制台；右下角托盘图标（左键开面板、右键退出）。

校验下载完整性：对比各 zip 同名 `.sha256` 文件
（macOS/Linux 用 `shasum -a 256`，Windows 用 `Get-FileHash -Algorithm SHA256`）。

## 这是什么

给正在运行的 Clash Verge Rev / Mihomo 加「节点体检」：延迟（与 Verge 同口径）+
真实带宽（两阶段：并发粗筛 → Top N 串行精测）+ 出口 IP 画像 + 综合评分，
附带本地 Web 面板（排序表格 / 一键切换 / 历史趋势 / 订阅回顾 / 评分 Profile）。
零第三方依赖，纯 Python 标准库 + 系统 curl。
