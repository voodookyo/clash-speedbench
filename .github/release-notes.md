## v0.8.0：Windows 支持

- **Windows 一键启动**：新增 `SpeedBench.bat`，双击即用——自动检测 Python
  （未安装则打开 Microsoft Store 的 Python 3 页面引导安装）、初始化
  `%APPDATA%\ClashSpeedBench` 数据目录、启动面板并自动打开浏览器
- **优雅中断**：Windows 下「中断测速」通过 CTRL_BREAK_EVENT 实现，
  与 macOS 的 SIGINT 一样会自动恢复 Clash 的运行模式与节点选择
- **自动适配 Windows 环境**：自动探测 Windows 上 Clash Verge Rev 的配置路径
  与物理网卡，两阶段并发测速体验与 macOS 一致
- macOS 端功能与 v0.7.0 一致，无破坏性变更

## 安装

### macOS（12+）

下载 `Clash-SpeedBench-v0.8.0-macos.zip`，解压后把 `Clash SpeedBench.app`
拖进「应用程序」；首次打开需**右键 → 打开**（未做付费开发者签名，Gatekeeper 只拦一次），
需要系统里有 `python3`。

### Windows（10/11）

下载 `Clash-SpeedBench-v0.8.0-windows.zip`，解压后双击 `SpeedBench.bat`
（需要 Python 3.9+，未安装会自动引导到 Microsoft Store 安装，无需管理员权限）。

校验下载完整性：对比各 zip 同名 `.sha256` 文件
（macOS/Linux 用 `shasum -a 256`，Windows 用 `Get-FileHash -Algorithm SHA256`）。

## 这是什么

给正在运行的 Clash Verge Rev / Mihomo 加「节点体检」：延迟（与 Verge 同口径）+
真实带宽（两阶段：并发粗筛 → Top N 串行精测）+ 出口 IP 画像 + 综合评分，
附带本地 Web 面板（排序表格 / 一键切换 / 历史趋势 / 评分 Profile）。
零第三方依赖，纯 Python 标准库 + 系统 curl。
