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
# uv 优先用 PATH 中的命令；可用 UV_CMD 环境变量覆盖（不再写死本机绝对路径）
UV_CMD="${UV_CMD:-uv}"

mkdir -p "$OUT_ROOT"

if [ ! -x "$PY" ]; then
  echo "[build] 创建独立 venv: $VENV"
  "$UV_CMD" venv "$VENV" --python 3.12
fi

echo "[1/4] 安装/确认依赖..."
if "$PY" -m pip --version >/dev/null 2>&1; then
  "$PY" -m pip install --quiet -r requirements.txt pyinstaller pillow onnxruntime
else
  "$UV_CMD" pip install --index-url https://pypi.tuna.tsinghua.edu.cn/simple \
    --python "$PY" -r requirements.txt pyinstaller pillow onnxruntime
fi

echo "[2/5] 准备嵌入模型（fastembed bge-small-zh-v1.5，约 91MB，打进包内离线可用）..."
FASTEMBED_CACHE="$OUT_ROOT/fastembed_models"
if [ ! -d "$FASTEMBED_CACHE/models--Qdrant--bge-small-zh-v1.5" ]; then
  echo "[build] 下载嵌入模型（首次约 91MB，缓存于 $FASTEMBED_CACHE）..."
  FASTEMBED_CACHE_PATH="$FASTEMBED_CACHE" "$PY" -c "from fastembed import TextEmbedding; TextEmbedding(model_name='BAAI/bge-small-zh-v1.5', cache_dir='$FASTEMBED_CACHE')"
fi
# HF hub 缓存是 snapshots→blobs 符号链接结构，PyInstaller 归档不支持 → 解引用为实体目录
FLAT_CACHE="$OUT_ROOT/fastembed_models_flat"
rm -rf "$FLAT_CACHE"
cp -RL "$FASTEMBED_CACHE" "$FLAT_CACHE"
echo "[build] 嵌入模型就绪（$(du -sh "$FLAT_CACHE" | cut -f1)，已解符号链接）"

echo "[3/5] 生成 HeartBeat.icns ..."
"$PY" "$ROOT/.CodePapr/tmp/make_icns.py"

echo "[4/5] PyInstaller 打包（输出到 $DIST_DIR）..."
rm -rf "$DIST_DIR" "$OUT_ROOT/build"
"$PY" -m PyInstaller \
  --noconfirm --clean --windowed \
  --name HeartBeat \
  --distpath "$DIST_DIR" \
  --workpath "$OUT_ROOT/build" \
  --icon "$ROOT/assets/HeartBeat.icns" \
  --add-data "$ROOT/plugins:plugins" \
  --add-data "$ROOT/brain:brain" \
  --add-data "$FLAT_CACHE:models/fastembed" \
  --collect-all fastembed \
  --collect-all onnxruntime \
  --collect-all sqlite_vec \
  "$ROOT/main.py"

echo "[5/5] 校验产物..."
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

# 关键校验：嵌入模型实体必须打进包（离线可用）
EMBED_ONNX=$(find "$APP/Contents/Resources/models/fastembed" -name "*.onnx" -size +50M 2>/dev/null | head -1)
if [ -z "$EMBED_ONNX" ]; then
  echo "[build] 失败：嵌入模型未打进包（Resources/models/fastembed 下无 >50MB onnx）"
  exit 1
fi
echo "[build] 嵌入模型实体 OK（$(du -h "$EMBED_ONNX" | cut -f1)）"

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

# 离线嵌入验证：--cli embed 从包内注入模型 → 用户目录 → 512 维向量
EMBED_OUT=$(HOME="$SMOKE_HOME" HB_NO_MAC_TRAY=1 perl -e 'alarm 60; exec @ARGV' "$APP/Contents/MacOS/HeartBeat" --cli embed 测试 2>&1)
if ! echo "$EMBED_OUT" | grep -q "512"; then
  echo "[build] 离线嵌入验证失败，输出："
  echo "$EMBED_OUT" | head -10
  exit 1
fi
echo "[build] 离线嵌入 OK（512 维向量）"
# 确认模型确实注入了用户目录（而非联网下载）
if [ ! -d "$SMOKE_HOME/Library/Application Support/HeartBeat/models/models--Qdrant--bge-small-zh-v1.5" ]; then
  echo "[build] 警告：模型未注入用户目录（$SMOKE_HOME/Library/Application Support/HeartBeat/models）"
fi

# 用户数据目录校验：config.json 必须生成在 HOME 下的 Application Support
if [ ! -f "$SMOKE_HOME/Library/Application Support/HeartBeat/config.json" ]; then
  echo "[build] 冒烟失败：未在用户数据目录生成 config.json"
  exit 1
fi
rm -rf "$SMOKE_HOME"
rm -rf "$FLAT_CACHE"  # 解符号链接的临时目录用完即删（模型实体已归档进包）

echo "[build] 完成: $APP"
echo "[build] 架构: $(file "$APP/Contents/MacOS/HeartBeat" | sed 's/.*: //')"
echo "[build] 启动: open \"$APP\""
