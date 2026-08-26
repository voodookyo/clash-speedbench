#!/bin/bash
# 打包 Clash SpeedBench.app 到 dist/（自包含：代码 + 图标 + 启动器）
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
APP="$ROOT/dist/Clash SpeedBench.app"

echo "→ 清理旧包"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/app"

echo "→ 拷贝程序文件"
cp "$ROOT/clash_speedbench.py" \
   "$ROOT/speedbench_db.py" \
   "$ROOT/speedbench_web.py" \
   "$ROOT/speedbench_switch.py" \
   "$ROOT/speedbench_workers.py" \
   "$ROOT/speedbench_tray.py" \
   "$APP/Contents/Resources/app/"

echo "→ 校验程序文件完整性（防止漏拷打出残包）"
for f in clash_speedbench.py speedbench_db.py speedbench_web.py \
         speedbench_switch.py speedbench_workers.py speedbench_tray.py; do
  if [ ! -f "$APP/Contents/Resources/app/$f" ]; then
    echo "✗ 缺少 $f：程序文件不完整，终止打包（避免打出残包）" >&2
    exit 1
  fi
done

echo "→ 拷贝前端静态文件"
if [ ! -f "$ROOT/web/index.html" ]; then
  echo "✗ 缺少 web/index.html：前端文件不完整，终止打包（避免打出残包）" >&2
  exit 1
fi
cp -R "$ROOT/web" "$APP/Contents/Resources/app/"

echo "→ 生成图标"
python3 "$ROOT/scripts/make_icon.py" "$APP/Contents/Resources/AppIcon.icns"

echo "→ 写入 plist 与启动器"
cp "$ROOT/packaging/Info.plist" "$APP/Contents/"
cp "$ROOT/packaging/SpeedBench" "$APP/Contents/MacOS/SpeedBench"
chmod +x "$APP/Contents/MacOS/SpeedBench"

echo "→ ad-hoc 签名（本机免 Gatekeeper 拦截）"
codesign --force --deep --sign - "$APP" >/dev/null 2>&1 || true

echo "✅ 已生成: $APP"
echo "   安装到 /Applications:  cp -R \"$APP\" /Applications/"
