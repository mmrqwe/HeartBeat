#!/bin/bash
# macOS 构建脚本：把 HeartBeat 打包成 .app
# 用法：./build_mac.sh   （在项目根目录执行）
#
# 重要：构建产物与虚拟环境放在 OneDrive 同步目录之外（~/HeartBeat-mac），
# 因为 OneDrive 会破坏 Qt framework 的 symlink 结构导致 app 启动即退。
# 构建完成后自动做 Qt 库完整性校验 + offscreen 冒烟测试。
set -euo pipefail

cd "$(dirname "$0")"
ROOT="$(pwd)"
OUT_ROOT="${OUT_ROOT:-$HOME/HeartBeat-mac}"
VENV="$OUT_ROOT/.venv"
PY="$VENV/bin/python"
DIST_DIR="$OUT_ROOT/dist"

mkdir -p "$OUT_ROOT"

if [ ! -x "$PY" ]; then
  echo "[build] 创建独立 venv: $VENV"
  uv venv "$VENV" --python 3.12
fi

echo "[1/4] 安装/确认依赖..."
if "$PY" -m pip --version >/dev/null 2>&1; then
  "$PY" -m pip install --quiet -r requirements.txt pyinstaller pillow onnxruntime
else
  uv pip install --index-url https://pypi.tuna.tsinghua.edu.cn/simple \
    --python "$PY" -r requirements.txt pyinstaller pillow onnxruntime
fi

echo "[2/4] 生成 HeartBeat.icns ..."
"$PY" "$ROOT/.CodePapr/tmp/make_icns.py"

echo "[3/4] PyInstaller 打包（输出到 $DIST_DIR）..."
rm -rf "$DIST_DIR" "$OUT_ROOT/build"
"$PY" -m PyInstaller \
  --noconfirm --clean --windowed \
  --name HeartBeat \
  --distpath "$DIST_DIR" \
  --workpath "$OUT_ROOT/build" \
  --icon "$ROOT/HeartBeat.icns" \
  --add-data "$ROOT/plugins:plugins" \
  --add-data "$ROOT/brain:brain" \
  --collect-all fastembed \
  --collect-all onnxruntime \
  --collect-all sqlite_vec \
  "$ROOT/main.py"

echo "[4/4] 校验产物..."
APP="$DIST_DIR/HeartBeat.app"
if [ ! -d "$APP" ]; then
  echo "[build] 失败：未生成 $APP"
  exit 1
fi

# 菜单栏应用：Info.plist 加 LSUIElement=true（隐藏 Dock 图标），修改后重签
PLIST="$APP/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Add :LSUIElement bool true" "$PLIST" 2>/dev/null \
  || /usr/libexec/PlistBuddy -c "Set :LSUIElement true" "$PLIST"
codesign --force --sign - "$APP"
echo "[build] Info.plist: LSUIElement=true（状态栏应用，不占 Dock），已重签"

# 关键校验：Qt framework 实体必须存在（OneDrive 破坏的典型症状是只剩空壳）
QTCORE="$APP/Contents/Frameworks/PySide6/Qt/lib/QtCore.framework/Versions/A/QtCore"
if [ ! -f "$QTCORE" ]; then
  echo "[build] 失败：QtCore.framework 实体缺失（$QTCORE）"
  exit 1
fi
QTSIZE=$(stat -f%z "$QTCORE")
echo "[build] QtCore.framework 实体 OK（${QTSIZE} bytes）"

# 冒烟测试：offscreen 模式运行 8 秒，SIGALRM(142) 即正常；崩溃/导入错误则失败
# HB_NO_MAC_TRAY=1：无 GUI 会话环境 AppKit 不可用（PyObjC 托盘会 abort），冒烟绕过它
set +e
cd "$OUT_ROOT"
SMOKE_HOME=$(mktemp -d)
# HOME 隔离：冒烟不污染真实用户数据；同时验证 app 在用户数据目录生成 config.json
HOME="$SMOKE_HOME" HB_NO_MAC_TRAY=1 QT_QPA_PLATFORM=offscreen perl -e 'alarm 8; exec @ARGV' "$APP/Contents/MacOS/HeartBeat" \
  > "$OUT_ROOT/smoke.log" 2>&1
SMOKE_EXIT=$?
set -e
if [ "$SMOKE_EXIT" -ne 142 ]; then
  echo "[build] 冒烟测试失败（exit=$SMOKE_EXIT），日志："
  head -30 "$OUT_ROOT/smoke.log"
  exit 1
fi
if grep -qi "Traceback\|ImportError\|Error loading" "$OUT_ROOT/smoke.log"; then
  echo "[build] 冒烟测试日志含错误："
  head -30 "$OUT_ROOT/smoke.log"
  exit 1
fi

# 用户数据目录校验：config.json 必须生成在 HOME 下的 Application Support
if [ ! -f "$SMOKE_HOME/Library/Application Support/HeartBeat/config.json" ]; then
  echo "[build] 冒烟失败：未在用户数据目录生成 config.json"
  exit 1
fi
rm -rf "$SMOKE_HOME"

echo "[build] 完成: $APP"
echo "[build] 架构: $(file "$APP/Contents/MacOS/HeartBeat" | sed 's/.*: //')"
echo "[build] 启动: open \"$APP\""
