## 安装（macOS 12+）

1. 下载 `Clash-SpeedBench-*-macos.zip` 并解压
2. 把 `Clash SpeedBench.app` 拖进「应用程序」
3. **首次打开**：因为应用没有付费开发者签名，直接双击会被 Gatekeeper 拦截——
   在「应用程序」里**右键 → 打开**，再点一次「打开」即可（只需一次）
4. 需要系统里有 `python3`（装过 Xcode 命令行工具或 Homebrew Python 即可；
   没有的话 App 会弹窗提示 `xcode-select --install`）
5. 保持 Clash Verge 运行，双击 App 图标就会打开测速面板

校验下载完整性：`shasum -a 256` 对比 `.sha256` 文件。

## 这是什么

给正在运行的 Clash Verge Rev / Mihomo 加「节点体检」：延迟（与 Verge 同口径）+
真实带宽（两阶段：并发粗筛 → Top N 串行精测）+ 出口 IP 画像 + 综合评分，
附带本地 Web 面板（排序表格 / 一键切换 / 历史趋势 / 评分 Profile）。
零第三方依赖，单文件 Python + 系统 curl。
