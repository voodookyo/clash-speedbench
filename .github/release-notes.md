## v0.8.1：Windows 系统托盘

- **新增 Windows 系统托盘图标**：面板启动后右下角出现 SpeedBench 图标——
  **左键单击打开面板**，右键菜单可「打开面板 / 退出 SpeedBench」；
  图标随面板自动出现、自动消失，不留僵尸图标。纯 ctypes/Win32 实现，
  零第三方依赖，不多挂任何额外窗口或进程
- **修复**：非中文区域设置的 Windows（如 en-US）上启动面板时，
  控制台中文输出会因 cp1252 编码崩溃（CI windows-latest 实测揪出）
- 包含 v0.8.0 的全部 Windows 适配；macOS 端功能不变

## 安装

### macOS（12+）

下载 `Clash-SpeedBench-v0.8.1-macos.zip`，解压后把 `Clash SpeedBench.app`
拖进「应用程序」；首次打开需**右键 → 打开**（未做付费开发者签名，Gatekeeper 只拦一次），
需要系统里有 `python3`。

### Windows（10/11）

下载 `Clash-SpeedBench-v0.8.1-windows.zip`，解压后双击 `SpeedBench.bat`
（需要 Python 3.9+，未安装会自动引导到 Microsoft Store 安装，无需管理员权限）。
面板启动后除了浏览器页面，右下角托盘也会出现图标，随手点开。

校验下载完整性：对比各 zip 同名 `.sha256` 文件
（macOS/Linux 用 `shasum -a 256`，Windows 用 `Get-FileHash -Algorithm SHA256`）。

## 这是什么

给正在运行的 Clash Verge Rev / Mihomo 加「节点体检」：延迟（与 Verge 同口径）+
真实带宽（两阶段：并发粗筛 → Top N 串行精测）+ 出口 IP 画像 + 综合评分，
附带本地 Web 面板（排序表格 / 一键切换 / 历史趋势 / 评分 Profile）。
零第三方依赖，纯 Python 标准库 + 系统 curl。
