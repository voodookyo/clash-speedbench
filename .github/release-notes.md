## v0.8.2：修复 macOS 包打不开面板

- **修复**：v0.8.1 的 macOS 发布包漏打了 `speedbench_tray.py`，
  面板进程启动即崩溃（`ModuleNotFoundError`），双击 App 后浏览器提示
  webui 无法访问；Windows 包不受影响
- **加固**：`build_app.sh` 打包后校验全部程序文件完整性，
  漏拷即终止，避免再打出残包
- 功能与 v0.8.1 完全一致（Windows 托盘、评分、自动切换等均无变化）

## 安装

### macOS（12+）

下载 `Clash-SpeedBench-v0.8.2-macos.zip`，解压后把 `Clash SpeedBench.app`
拖进「应用程序」；首次打开需**右键 → 打开**（未做付费开发者签名，Gatekeeper 只拦一次），
需要系统里有 `python3`。

### Windows（10/11）

下载 `Clash-SpeedBench-v0.8.2-windows.zip`，解压后双击 `SpeedBench.bat`
（需要 Python 3.9+，未安装会自动引导到 Microsoft Store 安装，无需管理员权限）。
面板启动后除了浏览器页面，右下角托盘也会出现图标，随手点开。

校验下载完整性：对比各 zip 同名 `.sha256` 文件
（macOS/Linux 用 `shasum -a 256`，Windows 用 `Get-FileHash -Algorithm SHA256`）。

## 这是什么

给正在运行的 Clash Verge Rev / Mihomo 加「节点体检」：延迟（与 Verge 同口径）+
真实带宽（两阶段：并发粗筛 → Top N 串行精测）+ 出口 IP 画像 + 综合评分，
附带本地 Web 面板（排序表格 / 一键切换 / 历史趋势 / 评分 Profile）。
零第三方依赖，纯 Python 标准库 + 系统 curl。
