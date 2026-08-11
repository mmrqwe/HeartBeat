#!/bin/bash
# ============================================================
#  HeartBeat Release 构建入口（macOS）
#  双击本文件即可编译，产物: ~/HeartBeat-mac/dist/HeartBeat.app
#  构建日志: ~/HeartBeat-mac/build-release.log
# ============================================================

cd "$(dirname "$0")" || exit 1

echo "=============================================="
echo " HeartBeat Release 构建开始"
echo "=============================================="
echo ""

# 日志记录（终端输出 + 落盘一份）
LOG="$HOME/HeartBeat-mac/build-release.log"
mkdir -p "$HOME/HeartBeat-mac"
: > "$LOG"
exec > >(tee "$LOG") 2>&1

if [ ! -f "./build_mac.sh" ]; then
  echo "[错误] 找不到 build_mac.sh，请确认本文件位于项目根目录（与 main.py 同级）。"
  echo ""
  read -r -p "按回车键关闭窗口..."
  exit 1
fi

chmod +x ./build_mac.sh
./build_mac.sh
CODE=$?

echo ""
if [ "$CODE" -eq 0 ]; then
  echo "=============================================="
  echo " 构建成功！"
  echo " 产物: $HOME/HeartBeat-mac/dist/HeartBeat.app"
  echo " 正在 Finder 中定位产物..."
  open -R "$HOME/HeartBeat-mac/dist/HeartBeat.app" 2>/dev/null || true
else
  echo "=============================================="
  echo " 构建失败（退出码 $CODE），请查看上方错误信息。"
  echo " 日志已保存: $LOG"
fi
echo "=============================================="
read -r -p "按回车键关闭窗口..."
exit "$CODE"
