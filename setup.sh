#!/usr/bin/env bash
set -euo pipefail

# ─────────────────────────────────────────────────────────
#  ADPD4101 BLE Client — 跨平台環境設定腳本
#  用法: chmod +x setup.sh && ./setup.sh
#  支援: macOS, Linux, Windows(WSL / Git Bash / MSYS2)
# ─────────────────────────────────────────────────────────

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

ok()   { printf "${GREEN}✓${NC} %s\n" "$1"; }
warn() { printf "${YELLOW}⚠${NC} %s\n" "$1"; }
info() { printf "${BLUE}ℹ${NC} %s\n" "$1"; }
fail() { printf "${RED}✗${NC} %s\n" "$1"; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"

UNAME="$(uname -s)"

# ── 檢測作業系統 ────────────────────────────────────────
OS="unknown"
INSTALL_CMD=""
PKG_MANAGER=""

if [[ "$UNAME" == "Darwin" ]]; then
    OS="macos"
    INSTALL_CMD="brew install"
    PKG_MANAGER="brew"
elif [[ "$UNAME" == "Linux" ]]; then
    if [[ -f /etc/debian_version ]] || grep -q "debian\|ubuntu" /etc/os-release 2>/dev/null; then
        OS="linux-debian"
        INSTALL_CMD="sudo apt-get install -y"
        PKG_MANAGER="apt"
    elif [[ -f /etc/redhat-release ]] || grep -q "rhel\|fedora\|centos" /etc/os-release 2>/dev/null; then
        OS="linux-redhat"
        INSTALL_CMD="sudo dnf install -y"
        PKG_MANAGER="dnf"
    elif command -v pacman &>/dev/null; then
        OS="linux-arch"
        INSTALL_CMD="sudo pacman -S --noconfirm"
        PKG_MANAGER="pacman"
    else
        OS="linux-generic"
        warn "未檢測到特定 Linux 發行版，請手動安裝必要工具"
    fi
    # 檢查是否為 WSL
    if grep -qE "(Microsoft|WSL)" /proc/version 2>/dev/null; then
        OS="wsl"
        warn "偵測到 WSL 環境 - 藍牙功能可能受限，請確認已啟用 WSL2 + USB 藍牙支援"
    fi
elif [[ "$UNAME" == MINGW* ]] || [[ "$UNAME" == CYGWIN* ]] || [[ "$UNAME" == MSYS* ]]; then
    OS="windows"
    if command -v winget &>/dev/null; then
        PKG_MANAGER="winget"
    elif command -v choco &>/dev/null; then
        PKG_MANAGER="choco"
    elif command -v scoop &>/dev/null; then
        PKG_MANAGER="scoop"
    else
        PKG_MANAGER="windows"
    fi
    warn "偵測到 Windows (Git Bash/MSYS/Cygwin) — 將使用 Windows 版 venv 路徑與 .exe 產物"
fi

# ── 依 OS 設定 venv 路徑 ───────────────────────────────
VENV_BIN=""
VENV_PY=""
VENV_ACTIVATE=""
if [[ "$OS" == "windows" ]]; then
    VENV_BIN="$VENV_DIR/Scripts"
    VENV_PY="$VENV_BIN/python.exe"
    VENV_ACTIVATE="$VENV_BIN/activate"
else
    VENV_BIN="$VENV_DIR/bin"
    VENV_PY="$VENV_BIN/python"
    VENV_ACTIVATE="$VENV_BIN/activate"
fi

echo "======================================"
echo " ADPD4101 BLE Client — 環境設定"
echo " 作業系統: $OS"
echo "======================================"
echo ""

# ── 提示安裝指令 ────────────────────────────────────────
suggest_install() {
    local tool=$1
    case $PKG_MANAGER in
        winget)
            info "安裝方式: winget search $tool 之後 winget install <ID>"
            ;;
        choco)
            info "安裝指令: choco install $tool"
            ;;
        scoop)
            info "安裝指令: scoop install $tool"
            ;;
        brew)
            info "安裝指令: brew install $tool"
            ;;
        apt)
            case $tool in
                zig) info "安裝指令: sudo snap install zig --classic" ;;
                uv) info "安裝指令: curl -LsSf https://astral.sh/uv/install.sh | sh" ;;
                *) info "安裝指令: sudo apt-get install $tool" ;;
            esac
            ;;
        dnf)
            info "安裝指令: sudo dnf install $tool" ;;
        pacman)
            info "安裝指令: sudo pacman -S $tool" ;;
        *)
            info "請自行安裝: $tool"
            ;;
    esac
}

# ── 1. 編譯工具檢查 ───────────────────────────────────
echo "── 檢查必要編譯工具 ──"

MISSING_TOOLS=()

# 檢查 Zig
if command -v zig &>/dev/null; then
    ok "Zig 已安裝 ($(zig version))"
else
    warn "Zig 未安裝"
    MISSING_TOOLS+=("zig")
    suggest_install "zig"
fi

# 檢查 C/C++ 編譯器（macOS 需要 Objective-C 編譯；Windows 需要 C 編譯）
if command -v gcc &>/dev/null || command -v clang &>/dev/null || command -v cl &>/dev/null; then
    if command -v clang &>/dev/null; then
        ok "Clang 已安裝 ($(clang --version | head -1))"
    elif command -v cl &>/dev/null; then
        ok "MSVC (cl.exe) 已可用"
    else
        ok "GCC 已安裝 ($(gcc --version | head -1))"
    fi
else
    warn "未檢測到 C/C++ 編譯器"
    MISSING_TOOLS+=("gcc/clang")
    case $PKG_MANAGER in
        brew) info "安裝指令: xcode-select --install" ;;
        apt) info "安裝指令: sudo apt-get install build-essential" ;;
        dnf) info "安裝指令: sudo dnf groupinstall 'Development Tools'" ;;
        pacman) info "安裝指令: sudo pacman -S base-devel" ;;
    esac
fi

if [[ ${#MISSING_TOOLS[@]} -gt 0 ]]; then
    echo ""
    fail "缺少必要工具: ${MISSING_TOOLS[*]}\n請先安裝以上工具後再執行此腳本。"
fi

# ── 2. Python 3 檢查 ─────────────────────────────────
echo ""
echo "── 檢查 Python 3 ──"

PYTHON_CMD=""
if command -v python3 &>/dev/null; then
    PYTHON_CMD="python3"
elif command -v python &>/dev/null && python --version 2>&1 | grep -q "Python 3"; then
    PYTHON_CMD="python"
elif [[ "$OS" == "windows" ]] && command -v py &>/dev/null; then
    PYTHON_CMD="py -3"
fi

if [[ -n "$PYTHON_CMD" ]]; then
    PYTHON_VERSION=$($PYTHON_CMD --version 2>&1 | cut -d' ' -f2)
    ok "Python 3 已安裝 ($PYTHON_VERSION)"
    
    # 檢查 Python 版本 >= 3.8
    PYTHON_MAJOR=$($PYTHON_CMD -c 'import sys; print(sys.version_info.major)')
    PYTHON_MINOR=$($PYTHON_CMD -c 'import sys; print(sys.version_info.minor)')
    if [[ $PYTHON_MAJOR -lt 3 ]] || [[ $PYTHON_MAJOR -eq 3 && $PYTHON_MINOR -lt 8 ]]; then
        warn "Python 版本過低 ($PYTHON_VERSION)，建議升級至 3.8+"
    fi
else
    fail "未安裝 Python 3，請先安裝 Python 3.8 或更高版本"
fi

# ── 3. Python 環境設定 ─────────────────────────────────
echo ""
echo "── 設定 Python 虛擬環境 ──"

# 優先使用 uv，其次使用 venv
USE_UV=false
if command -v uv &>/dev/null; then
    ok "uv 已安裝 ($(uv --version))"
    USE_UV=true
else
    warn "uv 未安裝，使用標準 venv"
    info "如需更快安裝，可執行: curl -LsSf https://astral.sh/uv/install.sh | sh"
fi

# 建立虛擬環境
if [[ -d "$VENV_DIR" ]]; then
    ok "虛擬環境已存在"
else
    if $USE_UV; then
        uv venv "$VENV_DIR"
    else
        $PYTHON_CMD -m venv "$VENV_DIR"
    fi
    ok "虛擬環境建立完成 ($VENV_DIR)"
fi

# 若 Windows venv python.exe 不在預期路徑，嘗試 fallback
if [[ "$OS" == "windows" ]] && [[ ! -f "$VENV_PY" ]]; then
    if [[ -f "$VENV_BIN/python" ]]; then
        VENV_PY="$VENV_BIN/python"
    fi
fi

# 確保 pip 可用
if ! "$VENV_PY" -m pip --version &>/dev/null; then
    warn "虛擬環境缺少 pip，正在修復..."
    "$VENV_PY" -m ensurepip --upgrade
fi

# ── 4. 安裝 Python 套件 ──────────────────────────────
echo ""
echo "── 安裝 Python 套件 ──"

pip() { "$VENV_PY" -m pip "$@"; }

# 升級 pip
pip install --upgrade pip -q

# 安裝必要套件
REQUIRED_PACKAGES="numpy imufusion PySide6"
pip install $REQUIRED_PACKAGES -q

ok "Python 套件安裝完成: $REQUIRED_PACKAGES"

# ── 5. 編譯專案 ───────────────────────────────────────
echo ""
echo "── 編譯專案 ──"

cd "$SCRIPT_DIR"

# 編譯 Zig 專案
echo "  編譯 Zig BLE 接收器..."
zig build
ok "Zig 編譯完成"


# ── 6. 建立便利腳本 ───────────────────────────────────
echo ""
echo "── 建立便利啟動腳本 ──"

# 建立啟動腳本目錄
mkdir -p "$SCRIPT_DIR/bin"

# 創建通用啟動腳本
cat > "$SCRIPT_DIR/bin/imu-workout" << 'EOF'
#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -f "$SCRIPT_DIR/.venv/Scripts/activate" ]]; then
    # Windows venv (Git Bash/MSYS)
    source "$SCRIPT_DIR/.venv/Scripts/activate"
    BT_CLIENT="$SCRIPT_DIR/zig-out/bin/zig_bt_client.exe"
else
    source "$SCRIPT_DIR/.venv/bin/activate"
    BT_CLIENT="$SCRIPT_DIR/zig-out/bin/zig_bt_client"
fi
if [[ ! -f "$BT_CLIENT" ]] && [[ -f "${BT_CLIENT%.exe}" ]]; then
    BT_CLIENT="${BT_CLIENT%.exe}"
fi
exec "$BT_CLIENT" --stdout | python "$SCRIPT_DIR/tools/imu_workout_gui.py" "$@"
EOF
chmod +x "$SCRIPT_DIR/bin/imu-workout"

cat > "$SCRIPT_DIR/bin/imu-running" << 'EOF'
#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -f "$SCRIPT_DIR/.venv/Scripts/activate" ]]; then
    source "$SCRIPT_DIR/.venv/Scripts/activate"
    BT_CLIENT="$SCRIPT_DIR/zig-out/bin/zig_bt_client.exe"
else
    source "$SCRIPT_DIR/.venv/bin/activate"
    BT_CLIENT="$SCRIPT_DIR/zig-out/bin/zig_bt_client"
fi
if [[ ! -f "$BT_CLIENT" ]] && [[ -f "${BT_CLIENT%.exe}" ]]; then
    BT_CLIENT="${BT_CLIENT%.exe}"
fi
exec "$BT_CLIENT" --stdout | python "$SCRIPT_DIR/tools/imu_running_gui.py" "$@"
EOF
chmod +x "$SCRIPT_DIR/bin/imu-running"

cat > "$SCRIPT_DIR/bin/imu-record" << 'EOF'
#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BT_CLIENT="$SCRIPT_DIR/zig-out/bin/zig_bt_client"
if [[ -f "$SCRIPT_DIR/zig-out/bin/zig_bt_client.exe" ]]; then
    BT_CLIENT="$SCRIPT_DIR/zig-out/bin/zig_bt_client.exe"
fi
exec "$BT_CLIENT" "$@"
EOF
chmod +x "$SCRIPT_DIR/bin/imu-record"

# Windows CMD 快捷啟動
if [[ "$OS" == "windows" ]]; then
cat > "$SCRIPT_DIR/bin/imu-workout.cmd" << 'EOF'
@echo off
setlocal
set SCRIPT_DIR=%~dp0..
set PY=%SCRIPT_DIR%\.venv\Scripts\python.exe
set BT=%SCRIPT_DIR%\zig-out\bin\zig_bt_client.exe
"%BT%" --stdout | "%PY%" "%SCRIPT_DIR%\tools\imu_workout_gui.py" %*
endlocal
EOF

cat > "$SCRIPT_DIR/bin/imu-running.cmd" << 'EOF'
@echo off
setlocal
set SCRIPT_DIR=%~dp0..
set PY=%SCRIPT_DIR%\.venv\Scripts\python.exe
set BT=%SCRIPT_DIR%\zig-out\bin\zig_bt_client.exe
"%BT%" --stdout | "%PY%" "%SCRIPT_DIR%\tools\imu_running_gui.py" %*
endlocal
EOF

cat > "$SCRIPT_DIR/bin/imu-record.cmd" << 'EOF'
@echo off
setlocal
set SCRIPT_DIR=%~dp0..
set BT=%SCRIPT_DIR%\zig-out\bin\zig_bt_client.exe
"%BT%" %*
endlocal
EOF
fi

ok "啟動腳本建立完成 (bin/)"

# ── 7. 環境驗證 ────────────────────────────────────────
echo ""
echo "── 驗證安裝 ──"

# 驗證 Python 套件
if "$VENV_PY" -c "import numpy, imufusion, PySide6" 2>/dev/null; then
    ok "Python 套件驗證成功"
else
    warn "Python 套件驗證失敗"
fi

# 驗證編譯產物
if [[ -f "$SCRIPT_DIR/zig-out/bin/zig_bt_client" ]] || [[ -f "$SCRIPT_DIR/zig-out/bin/zig_bt_client.exe" ]]; then
    ok "Zig BLE 接收器已生成"
else
    warn "Zig BLE 接收器未找到"
fi


# ── 8. 完成與使用說明 ──────────────────────────────────
echo ""
echo "======================================"
printf "${GREEN} 環境設定完成！${NC}\n"
echo "======================================"
echo ""

if [[ $OS == "macos" ]]; then
    echo "💡 macOS 提示:"
    echo "   - 確保已配對藍牙裝置"
    echo "   - 第一次執行可能需要授權藍牙權限"
elif [[ $OS == "wsl" ]]; then
    echo "💡 WSL 提示:"
    echo "   - 藍牙需要額外設定，建議在 Windows 端執行或參考 WSL USBIP 教學"
fi

echo ""
echo "📋 可用指令:"
echo ""
echo "  zig build run          — 接收 BLE 資料並存檔"
echo "  zig build workout      — 運動標記 GUI"
echo "  zig build running      — 跑步實驗標記 GUI"
echo "  zig build ppg-check    — PPG 品質檢查"
echo ""
echo "🚀 快捷啟動 (使用虛擬環境):"
echo "  ./bin/imu-workout      — 直接啟動運動標記 GUI"
echo "  ./bin/imu-running      — 直接啟動跑步實驗 GUI"
echo "  ./bin/imu-record       — 直接啟動錄製"
echo ""
echo "🐍 Python 環境:"
echo "  虛擬環境路徑: $VENV_DIR"
if [[ "$OS" == "windows" ]]; then
    echo "  啟用指令 (Git Bash): source $VENV_DIR/Scripts/activate"
    echo "  啟用指令 (PowerShell): .\\.venv\\Scripts\\Activate.ps1"
else
    echo "  啟用指令: source $VENV_DIR/bin/activate"
fi
echo ""
echo "📖 詳細說明請參考 README.md"
echo ""
