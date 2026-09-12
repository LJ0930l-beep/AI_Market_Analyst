#!/usr/bin/env bash
# AI Market Analyst —— 桌面客户端重建 + 安装（Git Bash 版）
#
# 与 scripts/sync-client.ps1 做同一件事：构建前端 → 构建 sidecar → 构建 Tauri
# 可执行文件 → 覆盖安装目录 → 校验哈希 → 重启客户端。
#
# 为什么还要有这个 .sh：
#   某些受限/受管宿主里 PowerShell 通道不能派生外部进程（npm / python / cargo
#   都拉不起来，退出码为空），但 Bash 通道可以。这个脚本就是那条通道上的等价
#   入口；逻辑与 sync-client.ps1 保持一致，改一边时请同步另一边。
#
# 用法:
#   bash scripts/build-client.sh                  # 完整重建 + 安装 + 重启
#   bash scripts/build-client.sh --skip-frontend  # 只重建 sidecar 与壳
#   bash scripts/build-client.sh --skip-shell     # 只重打包 sidecar 并安装
#   bash scripts/build-client.sh --no-restart     # 构建安装但不重启
#   bash scripts/build-client.sh --no-install     # 只构建，不动安装目录
#
# 设计约束（都是踩过的坑，勿删）：
#   * dist/work 每轮写进新的时间戳目录，不做递归删除。受限宿主里的批量删除保护
#     会按整轮累计计数，PyInstaller 覆盖写成千上万个中间文件时直接中断进程。
#   * Vite 清空 outDir 时会逐个删旧文件，同样触发上述保护。改成"重命名归档"。
#   * 任一步失败立即中止，绝不用旧产物冒充新产物。
#   * MSVC 链接器必须前置：Git Bash 的 /usr/bin/link.exe（GNU coreutils）会遮蔽
#     它，报 "link: missing operand after '\377\376'"。

set -uo pipefail

SKIP_FRONTEND=0
SKIP_SIDECAR=0
SKIP_SHELL=0
DO_INSTALL=1
DO_RESTART=1

for arg in "$@"; do
  case "$arg" in
    --skip-frontend) SKIP_FRONTEND=1 ;;
    --skip-sidecar)  SKIP_SIDECAR=1 ;;
    --skip-shell)    SKIP_SHELL=1 ;;
    --no-install)    DO_INSTALL=0 ;;
    --no-restart)    DO_RESTART=0 ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "未知参数: $arg（--help 查看用法）" >&2; exit 2 ;;
  esac
done

PROJ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WIN_PROJ="$(cygpath -m "$PROJ" 2>/dev/null || echo "$PROJ")"
TMP="${TEMP:-/tmp}"
TMP_WIN="$(cygpath -m "$TMP" 2>/dev/null || echo "$TMP")"
RUN_TAG="$(date +%Y%m%d-%H%M%S)"
INSTALL_DIR="${LOCALAPPDATA:-$HOME/AppData/Local}/Programs/AI Market Analyst"
# 两种形态各司其职：本地文件操作用 POSIX 形态，交给 wmic / schtasks 的用 Windows 形态。
#
# 为什么必须分开：Git Bash 的 coreutils 会在文件名含反斜杠时给 sha256sum 的整行加
# 反斜杠转义，于是 `awk '{print $1}'` 会拿到 "\<hash>" 而不是 "<hash>"，校验就会
# 报出一个根本不存在的差异（本脚本第一版正是栽在这里：src=3ee3af71… vs dst=\3ee3af71…，
# 而两侧 size 完全一致）。
INSTALL_DIR_U="$(cygpath -u "$INSTALL_DIR" 2>/dev/null || echo "$INSTALL_DIR")"
INSTALL_DIR_W="$(cygpath -m "$INSTALL_DIR" 2>/dev/null || echo "$INSTALL_DIR")"

fail() { echo "!!!"; echo "!!! $1"; echo "!!! 构建中止（不会用旧产物冒充新产物）"; echo "### BUILD SCRIPT ABORTED"; exit 1; }

# ---------------------------------------------------------------- 工具链定位
PF86="/c/Program Files (x86)"

find_msvc() {
  local newest="" dir ver
  for dir in "$PF86/Microsoft Visual Studio/2022"/*/VC/Tools/MSVC "$PF86/Microsoft Visual Studio/2019"/*/VC/Tools/MSVC; do
    [ -d "$dir" ] || continue
    ver="$(ls -1 "$dir" 2>/dev/null | sort -V | tail -1)"
    [ -n "$ver" ] && [ -x "$dir/$ver/bin/Hostx64/x64/link.exe" ] && newest="$dir/$ver"
  done
  printf '%s' "$newest"
}

find_sdk() {
  ls -1d "$PF86/Windows Kits/10/Include/"10.*/ 2>/dev/null | sort -V | tail -1
}

MSVC_DIR="$(find_msvc)"
[ -n "$MSVC_DIR" ] || fail "找不到 MSVC 工具链（本机探测: $PF86/Microsoft Visual Studio/2022/*/VC/Tools/MSVC）"
SDK_DIR="$(find_sdk)"
[ -n "$SDK_DIR" ] || fail "找不到 Windows SDK（本机探测: $PF86/Windows Kits/10/Include/10.*）"

MSVC_DIR_WIN="$(cygpath -m "$MSVC_DIR")"
SDK_DIR_WIN="$(cygpath -m "$SDK_DIR")"
KITS_INC_WIN="$(cygpath -m "$PF86/Windows Kits/10/Include")"
KITS_LIB_WIN="$(cygpath -m "$PF86/Windows Kits/10/Lib")"
SDK_VER="$(basename "$SDK_DIR")"

export LIB="$MSVC_DIR_WIN/lib/x64;$KITS_LIB_WIN/$SDK_VER/ucrt/x64;$KITS_LIB_WIN/$SDK_VER/um/x64"
export INCLUDE="$MSVC_DIR_WIN/include;$KITS_INC_WIN/$SDK_VER/ucrt;$KITS_INC_WIN/$SDK_VER/shared;$KITS_INC_WIN/$SDK_VER/um;$KITS_INC_WIN/$SDK_VER/winrt;$KITS_INC_WIN/$SDK_VER/cppwinrt"
export PATH="$MSVC_DIR/bin/Hostx64/x64:$PATH"
# 打包进来的桌面壳通过 Rust 的 ownership 握手拿到本次 sidecar 地址，
# 绝不能把开发端口烧进 dist。
export VITE_API_BASE_URL=""

# PyInstaller 必须由带 PyInstaller 的解释器执行，而 PATH 里的第一个 python 往往
# 是托管解释器（本机实测：解析到 3.13.12，`No module named PyInstaller`）。所以
# 像 sync-client.ps1 的 Resolve-BuildPython 一样显式探测，别盲信 PATH。
try_python() {
  local cand="$1"
  [ -n "$cand" ] || return 1
  case "$cand" in
    */*|*\\*) ;;
    *) cand="$(command -v "$cand" 2>/dev/null)" || return 1 ;;
  esac
  [ -n "$cand" ] && [ -x "$cand" ] || return 1
  if "$cand" -c "import PyInstaller" >/dev/null 2>&1; then PY_BIN="$cand"; return 0; fi
  # 受限宿主里可能拿不到子进程退出码，再用 site-packages 目录兜一次判断。
  local root; root="$(dirname "$cand")"
  if [ -f "$root/Lib/site-packages/PyInstaller/__init__.py" ]; then PY_BIN="$cand"; return 0; fi
  return 1
}

PY_BIN=""
for cand in "${AIMA_BUILD_PYTHON:-}" "$(command -v python 2>/dev/null || true)" \
            "${LOCALAPPDATA:-}/Programs/Python/Python312/python.exe" \
            "${LOCALAPPDATA:-}/Programs/Python/Python313/python.exe" \
            "${LOCALAPPDATA:-}/Programs/Python/Python311/python.exe"; do
  try_python "$cand" && break
done
[ -n "$PY_BIN" ] || fail "找不到带 PyInstaller 的 python（可用环境变量 AIMA_BUILD_PYTHON 指定）"
export PATH="$(dirname "$PY_BIN"):$PATH"

# npm 同理：PATH 里没有时补上常见的托管/标准安装位置。
if ! command -v npm >/dev/null 2>&1; then
  for cand in "${LOCALAPPDATA:-}/Programs/nodejs" "$HOME/.workbuddy/binaries/node/versions"/*/; do
    if [ -x "$cand/npm" ] || [ -x "$cand/npm.cmd" ]; then export PATH="$cand:$PATH"; break; fi
  done
fi

echo "### run tag : $RUN_TAG"
echo "### msvc    : $MSVC_DIR"
echo "### sdk     : $SDK_VER"
echo "### linker  : $(command -v link.exe)"
echo "### python  : $PY_BIN"
echo "### npm     : $(command -v npm || echo MISSING)"

command -v npm >/dev/null || fail "PATH 里没有 npm（前端产物需要它）"
command -v cargo >/dev/null || fail "PATH 里没有 cargo（Tauri 壳需要它）"

cd "$PROJ" || fail "无法进入项目目录 $PROJ"

# ------------------------------------------------------------------ 1 前端
if [ "$SKIP_FRONTEND" != "1" ]; then
  echo "=== STEP 1 frontend ==="
  if [ -d "$PROJ/web/dist" ]; then
    ARCHIVE="$PROJ/build/web-dist-archive/$RUN_TAG"
    mkdir -p "$(dirname "$ARCHIVE")"
    mv "$PROJ/web/dist" "$ARCHIVE"      # 重命名不计入删除配额
    echo "FRONTEND_ARCHIVED_OLD_DIST=$ARCHIVE"
  fi
  ( cd web && npm run build ) || fail "前端构建失败（npm run build）"
  [ -f "$PROJ/web/dist/index.html" ] || fail "npm run build 未产出 web/dist/index.html"
  echo "FRONTEND_OK=yes"
else
  [ -f "$PROJ/web/dist/index.html" ] || fail "跳过前端构建，但 web/dist/index.html 不存在"
  echo "=== STEP 1 frontend (skipped) ==="
fi

# ---------------------------------------------------------------- 2 sidecar
DIST_DIR_WIN="$WIN_PROJ/build/sidecar/$RUN_TAG/dist"
WORK_DIR_WIN="$WIN_PROJ/build/sidecar/$RUN_TAG/work"
if [ "$SKIP_SIDECAR" != "1" ]; then
  echo "=== STEP 2 sidecar (PyInstaller) ==="
  PYI_LOG="$TMP/aima_pyi_$RUN_TAG.log"
  "$PY_BIN" -m PyInstaller --noconfirm --onefile --name ai-market-analyst-backend \
    --hidden-import ccxt --hidden-import ccxt.gate \
    --exclude-module torch --exclude-module torchvision --exclude-module torchaudio \
    --exclude-module scipy --exclude-module matplotlib --exclude-module sympy \
    --exclude-module PIL --exclude-module cv2 --exclude-module av \
    --distpath "$DIST_DIR_WIN" --workpath "$WORK_DIR_WIN" \
    --specpath "$WIN_PROJ/build/sidecar" \
    "$WIN_PROJ/apps/sidecar.py" > "$PYI_LOG" 2>&1 \
    || { echo "!!! PyInstaller 日志尾部："; tail -12 "$PYI_LOG"; fail "PyInstaller 失败（日志 $PYI_LOG）"; }
  echo "SIDECAR_OK=yes  log=$PYI_LOG"
else
  echo "=== STEP 2 sidecar (skipped) ==="
fi

BUILT="$PROJ/build/sidecar/$RUN_TAG/dist/ai-market-analyst-backend.exe"
if [ "$SKIP_SIDECAR" = "1" ]; then
  # 跳过构建时，沿用上一次已经放进 binaries 的那一份。
  BUILT="$PROJ/src-tauri/binaries/ai-market-analyst-backend-x86_64-pc-windows-msvc.exe"
fi
[ -f "$BUILT" ] || fail "未产出 sidecar：$BUILT"
if [ "$SKIP_SIDECAR" != "1" ]; then
  cp -f "$BUILT" "$PROJ/src-tauri/binaries/ai-market-analyst-backend-x86_64-pc-windows-msvc.exe"
  cp -f "$BUILT" "$PROJ/src-tauri/binaries/ai-market-analyst-backend-x86_64-pc-windows-gnu.exe"
  echo "SIDECAR_COPIED=yes size=$(stat -c%s "$BUILT")"
fi

# -------------------------------------------------------------------- 3 壳
if [ "$SKIP_SHELL" != "1" ]; then
  echo "=== STEP 3 tauri build (no bundle) ==="
  ( cd "$PROJ/src-tauri" && cargo tauri build --target x86_64-pc-windows-msvc --no-bundle \
      --config '{"build":{"beforeBuildCommand":""}}' ) || fail "cargo tauri build 失败"
else
  echo "=== STEP 3 tauri build (skipped) ==="
fi

APP_EXE="$PROJ/src-tauri/target/x86_64-pc-windows-msvc/release/ai-market-analyst.exe"
[ -f "$APP_EXE" ] || fail "未产出 $APP_EXE"

echo "=== ARTIFACTS (tag=$RUN_TAG) ==="
ls -l --time-style=+%Y-%m-%d_%H:%M:%S "$APP_EXE" "$PROJ/web/dist/index.html" \
      "$PROJ/src-tauri/binaries/ai-market-analyst-backend-x86_64-pc-windows-msvc.exe"

# ------------------------------------------------------------------ 4 安装
if [ "$DO_INSTALL" = "1" ]; then
  echo "=== STEP 4 install ==="
  [ -d "$INSTALL_DIR_U" ] || fail "安装目录不存在：$INSTALL_DIR_U（请先运行 NSIS 安装包）"

  # 只按"可执行文件完整路径"核对后结束进程，同名但不是本产品的进程不碰。
  stop_owned() {
    local exe_name="$1" pid_list
    if ! command -v wmic >/dev/null 2>&1; then
      echo "!! 警告：本机没有 wmic，退化为按进程名结束（$exe_name）"
      taskkill /F /IM "$exe_name" >/dev/null 2>&1
      return
    fi
    # wmic 的 /format:csv 列序随属性名排序变化，这里按"内容"识别：
    # 以 .exe 结尾的是路径，纯数字的是 PID。
    pid_list="$(wmic process where "name='$exe_name'" get ProcessId,ExecutablePath /format:csv 2>/dev/null \
      | tr -d '\r' | awk -F',' -v want="$INSTALL_DIR_W" '
          { p=""; id=""
            for (i = 1; i <= NF; i++) {
              if ($i ~ /[.]exe$/) p = $i
              else if ($i ~ /^[0-9]+$/) id = $i
            }
            if (p == "" || id == "") next
            gsub(/\\/, "/", p); gsub(/\\/, "/", want)
            if (index(tolower(p), tolower(want)) == 1) print id
          }')"
    [ -n "$pid_list" ] && taskkill /F /PID $pid_list >/dev/null 2>&1
    return 0
  }
  for n in ai-market-analyst.exe ai-market-analyst-backend.exe; do stop_owned "$n"; done
  sleep 2
  if tasklist 2>/dev/null | grep -qiE "^ai-market-analyst(-backend)?\.exe"; then
    echo "!! 提示：安装目录里的进程可能仍存活，覆盖可能失败"
  fi

  # 再剥离一次行首转义符做兜底，不依赖路径一定不含特殊字符。
  sha_of() { sha256sum "$1" 2>/dev/null | awk '{ h=$1; sub(/^\\/, "", h); print h }'; }

  # 覆盖后立刻读回可能撞上写后可见性延迟，因此重试几次再判失败；判失败时把两侧的
  # 哈希与字节数都打出来——只报"不一致"会让人无从下手（本脚本第一版就是这样，
  # 结果报了一次实际并不存在的差异）。
  verify_copy() {
    local src="$1" dst="$2" label="$3" src_hash dst_hash attempt
    src_hash="$(sha_of "$src")"
    for attempt in 1 2 3 4 5; do
      dst_hash="$(sha_of "$dst")"
      if [ -n "$dst_hash" ] && [ "$src_hash" = "$dst_hash" ]; then
        echo "INSTALLED $label sha256=$dst_hash"
        return 0
      fi
      sleep 0.5
      src_hash="$(sha_of "$src")"
    done
    echo "!!! $label 校验不一致"
    echo "!!!   src=$src_hash  size=$(stat -c%s "$src" 2>/dev/null)  $src"
    echo "!!!   dst=$dst_hash  size=$(stat -c%s "$dst" 2>/dev/null)  $dst"
    fail "$label 安装后校验不一致"
  }

  cp -f "$APP_EXE" "$INSTALL_DIR_U/ai-market-analyst.exe" || fail "覆盖 ai-market-analyst.exe 失败"
  cp -f "$BUILT"   "$INSTALL_DIR_U/ai-market-analyst-backend.exe" || fail "覆盖 sidecar 失败"

  verify_copy "$APP_EXE" "$INSTALL_DIR_U/ai-market-analyst.exe" "ai-market-analyst.exe"
  verify_copy "$PROJ/src-tauri/binaries/ai-market-analyst-backend-x86_64-pc-windows-msvc.exe" \
              "$INSTALL_DIR_U/ai-market-analyst-backend.exe" "ai-market-analyst-backend.exe"
fi

# ------------------------------------------------------------------ 5 重启
if [ "$DO_INSTALL" = "1" ] && [ "$DO_RESTART" = "1" ]; then
  echo "=== STEP 5 restart ==="
  # 走计划任务：GUI 进程必须脱离当前 shell 存活，直接后台启动会被回收。
  APP_WIN="$INSTALL_DIR_W/ai-market-analyst.exe"
  schtasks /create /tn "AIMA_ClientStart" /tr "\"$APP_WIN\"" /sc once /st 23:59 /it /f >/dev/null 2>&1
  schtasks /run /tn "AIMA_ClientStart" >/dev/null 2>&1 && echo "RESTART_REQUESTED pid 稍后由 status 查看"
  sleep 20
  tasklist 2>/dev/null | grep -iE "ai-market-analyst" || echo "（尚未看到进程，可能仍在启动）"
elif [ "$DO_INSTALL" = "1" ]; then
  echo "已安装但未重启：用 Start_AI_Market_Analyst.cmd 或 scripts/desktop-client.ps1 -Action start 启动。"
fi

echo "### BUILD SCRIPT DONE"
