#!/usr/bin/env bash
# Caesar bootstrap installer
# ============================================================
# Установка одной командой:
#   curl -fsSL https://raw.githubusercontent.com/madlenprust/Caesar-agent/main/install.sh | bash
#
# Подход (как у Ari): user-space установка, без /opt и sudo для кода.
# - Клон в ~/caesar
# - venv в ~/.local/share/caesar/venv
# - данные в ~/.local/share/caesar/data
# - конфиги в ~/.config/caesar
# - systemd user services в ~/.config/systemd/user/
# - CLI shim в ~/.local/bin/caesar
# - enable-linger для always-on
# ============================================================

set -euo pipefail

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

info()  { echo -e "${GREEN}✅${NC} $1"; }
warn()  { echo -e "${YELLOW}⚠️${NC} $1"; }
error() { echo -e "${RED}❌${NC} $1" >&2; }
step()  { echo -e "${BLUE}🔧${NC} $1"; }

# Defaults — всё в домашней папке пользователя
REPO_URL="${CAESAR_REPO:-https://github.com/madlenprust/Caesar-agent.git}"
BRANCH="${CAESAR_BRANCH:-main}"
REPO_DIR="${CAESAR_REPO_DIR:-${HOME}/caesar}"
VENV_DIR="${REPO_DIR}/venv"
DATA_DIR="${REPO_DIR}/data"
CONFIG_DIR="${REPO_DIR}"
LOG_DIR="${REPO_DIR}/data/log"
SYSTEMD_USER_DIR="${HOME}/.config/systemd/user"
LOCAL_BIN_DIR="${HOME}/.local/bin"

NON_INTERACTIVE=false
SKIP_SETUP=false
START_SERVICES=true
ENABLE_LINGER=""
SKIP_TELEGRAM=false

# Banner
cat <<'BANNER'

 ██████╗ █████╗ ███████╗███████╗ █████╗ ██████╗
 ██╔════╝██╔══██╗██╔════╝██╔════╝██╔══██╗██╔══██╗
 ██║     ███████║█████╗  ███████╗███████║██████╔╝
 ██║     ██╔══██║██╔══╝  ╚════██║██╔══██║██╔══██╗
 ╚██████╗██║  ██║███████╗███████║██║  ██║██║  ██║
  ╚═════╝╚═╝  ╚═╝╚══════╝╚══════╝╚═╝  ╚═╝╚═╝  ╚═╝

        Caesar — автономный AI-агент для Ubuntu

BANNER

# Parse args
while [[ $# -gt 0 ]]; do
    case $1 in
        --non-interactive)  NON_INTERACTIVE=true; shift ;;
        --skip-setup)       SKIP_SETUP=true; shift ;;
        --no-start)         START_SERVICES=false; shift ;;
        --no-telegram)      SKIP_TELEGRAM=true; shift ;;
        --enable-linger)    ENABLE_LINGER="true"; shift ;;
        --disable-linger)   ENABLE_LINGER="false"; shift ;;
        --repo)             REPO_URL="$2"; shift 2 ;;
        --branch)           BRANCH="$2"; shift 2 ;;
        --dir)              REPO_DIR="$2"; shift 2 ;;
        --help|-h)
            cat <<EOF
Caesar installer (user-space, без sudo для кода).

Usage:
  curl -fsSL https://raw.githubusercontent.com/madlenprust/Caesar-agent/main/install.sh | bash
  ./install.sh [options]

Options:
  --non-interactive  Без вопросов, defaults
  --skip-setup       Пропустить setup wizard (настроить позже)
  --no-start         Не запускать сервисы
  --no-telegram      Пропустить TG-настройку
  --enable-linger    Always-on после выхода (systemd lingering)
  --disable-linger   Не включать lingering
  --repo URL         Git репозиторий
  --branch BRANCH    Git ветка
  --dir PATH         Куда клонировать (default: ~/caesar)
  --help             Эта справка

Layout:
  Repo:       $REPO_DIR
  Venv:       $VENV_DIR
  Data:       $DATA_DIR
  Config:     $CONFIG_DIR
  Services:   $SYSTEMD_USER_DIR
  CLI:        $LOCAL_BIN_DIR/caesar
EOF
            exit 0
            ;;
        *) error "Unknown arg: $1"; exit 1 ;;
    esac
done

run_privileged() {
    if [[ "$(id -u)" -eq 0 ]]; then
        "$@"
        return
    fi
    if command -v sudo >/dev/null 2>&1; then
        sudo "$@"
        return
    fi
    error "Missing sudo. Install sudo or run as root."
    exit 1
}

# === 1. Check OS ===
step "Checking system..."

if [[ -f /etc/os-release ]]; then
    . /etc/os-release
    if [[ "$ID" != "ubuntu" && "$ID" != "debian" ]]; then
        warn "OS: $ID — не Ubuntu/Debian, может не работать"
    else
        info "OS: $NAME $VERSION_ID"
    fi
fi

ARCH=$(uname -m)
case "$ARCH" in
    x86_64|amd64)   info "Arch: x86_64" ;;
    aarch64|arm64)  info "Arch: arm64" ;;
    *) error "Unsupported arch: $ARCH"; exit 1 ;;
esac

# === 2. Install system prerequisites (with sudo) ===
step "Installing system prerequisites..."

ensure_prereqs() {
    if ! command -v apt-get >/dev/null 2>&1; then
        return
    fi
    local packages=()
    
    if ! command -v python3 >/dev/null 2>&1; then
        packages+=("python3")
    fi
    if ! command -v git >/dev/null 2>&1; then
        packages+=("git")
    fi
    if ! command -v curl >/dev/null 2>&1; then
        packages+=("curl")
    fi
    if ! python3 -c "import venv" 2>/dev/null; then
        packages+=("python3-venv")
    fi
    if ! python3 -m pip --version >/dev/null 2>&1; then
        packages+=("python3-pip")
    fi
    if [[ ! -e /etc/ssl/certs/ca-certificates.crt ]]; then
        packages+=("ca-certificates")
    fi
    if ! command -v systemctl >/dev/null 2>&1; then
        packages+=("systemd")
    fi
    # Опциональные полезные пакеты для документов
    if ! command -v tesseract >/dev/null 2>&1; then
        packages+=("tesseract-ocr" "tesseract-ocr-rus")
    fi
    if ! command -v pdftoppm >/dev/null 2>&1; then
        packages+=("poppler-utils")
    fi
    if ! dpkg -s "dbus-user-session" >/dev/null 2>&1; then
        packages+=("dbus-user-session")
    fi
    
    if [[ ${#packages[@]} -gt 0 ]]; then
        echo "  Устанавливаю: ${packages[*]}"
        run_privileged apt-get update -qq
        run_privileged env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${packages[@]}"
    fi
    info "Prerequisites OK"
}

ensure_prereqs

# === 3. Check Python version ===
step "Checking Python 3.12+..."

PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || echo "0.0")
PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)

if [[ "$PY_MAJOR" -lt 3 || ("$PY_MAJOR" -eq 3 && "$PY_MINOR" -lt 12) ]]; then
    warn "Python $PY_VERSION, нужно 3.12+. Устанавливаю через deadsnakes PPA..."
    run_privileged add-apt-repository -y ppa:deadsnakes/ppa
    run_privileged apt-get update -qq
    run_privileged apt-get install -y -qq python3.12 python3.12-venv
    PY_BIN=python3.12
else
    PY_BIN=python3
    info "Python: $PY_VERSION"
fi

# === 4. Clone repo ===
step "Cloning Caesar into $REPO_DIR..."

mkdir -p "$(dirname "$REPO_DIR")"

if [[ -d "$REPO_DIR/.git" ]]; then
    info "Updating existing repo..."
    git -C "$REPO_DIR" fetch origin "$BRANCH" --quiet
    git -C "$REPO_DIR" checkout "$BRANCH" --quiet
    git -C "$REPO_DIR" pull --ff-only origin "$BRANCH" --quiet
else
    if [[ -e "$REPO_DIR" && ! -d "$REPO_DIR/.git" ]]; then
        error "Path exists but is not a git repo: $REPO_DIR"
        exit 1
    fi
    git clone --quiet --branch "$BRANCH" "$REPO_URL" "$REPO_DIR"
fi
info "Repo ready: $REPO_DIR"

cd "$REPO_DIR"

# === 5. Create venv and install ===
step "Creating venv..."

mkdir -p "${VENV_DIR%/*}" "$DATA_DIR" "$CONFIG_DIR" "$SYSTEMD_USER_DIR" "$LOCAL_BIN_DIR"

if [[ ! -d "$VENV_DIR" ]]; then
    "$PY_BIN" -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/pip" install --quiet --upgrade pip setuptools wheel
"$VENV_DIR/bin/pip" install --quiet -e "$REPO_DIR" 2>&1 | tail -3 || warn "Some deps may have failed"
info "Dependencies installed"

# === 6. CLI shim ===
step "Creating 'caesar' command..."

cat > "$LOCAL_BIN_DIR/caesar" <<EOF
#!/bin/bash
exec "$VENV_DIR/bin/python" -m caesar.cli_bridge "\$@"
EOF
chmod +x "$LOCAL_BIN_DIR/caesar"
info "CLI: $LOCAL_BIN_DIR/caesar"

# Add to PATH if not there
if [[ ":$PATH:" != *":$LOCAL_BIN_DIR:"* ]]; then
    warn "$LOCAL_BIN_DIR не в PATH. Добавь в ~/.bashrc:"
    echo '  export PATH="$HOME/.local/bin:$PATH"'
fi

# === 7. Config ===
if [[ ! -f "$CONFIG_DIR/config.yaml" ]]; then
    cp "$REPO_DIR/etc/config.yaml.example" "$CONFIG_DIR/config.yaml"
fi
chmod 700 "$CONFIG_DIR" 2>/dev/null || true
chmod 600 "$CONFIG_DIR/config.yaml" 2>/dev/null || true
info "Config: $CONFIG_DIR/config.yaml"

# === 7.5. Self-knowledge files ===
mkdir -p "$DATA_DIR/self"
if [[ -d "$REPO_DIR/caesar/self" ]]; then
    cp "$REPO_DIR/caesar/self/"*.md "$DATA_DIR/self/" 2>/dev/null || true
    info "Self-knowledge: $DATA_DIR/self/"
fi

# === 8. Systemd user services ===
step "Setting up systemd user services..."

cat > "$SYSTEMD_USER_DIR/caesar-daemon.service" <<EOF
[Unit]
Description=Caesar AI Agent Daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$REPO_DIR
ExecStart=$VENV_DIR/bin/python -m caesar.daemon
Restart=always
RestartSec=5
# Graceful shutdown: даём daemon 5 минут на завершение активных задач
# перед тем как systemd отправит SIGKILL. Daemon сам ждёт до 180s,
# остальное — запас на persist в БД.
TimeoutStopSec=300
KillSignal=SIGTERM
# Memory limit — чтобы daemon не мог сожрать всю RAM и уронить систему.
# 1.5GB достаточно для daemon + L3 cache (2000 векторов) + whisper/stt модели.
# При превышении systemd убьёт daemon (не систему) и перезапустит через Restart=always.
MemoryMax=1500M
MemoryHigh=1200M
Environment=PYTHONUNBUFFERED=1
Environment=CAESAR_CONFIG_DIR=$CONFIG_DIR
Environment=CAESAR_DATA_DIR=$DATA_DIR
Environment=CAESAR_LOG_DIR=$LOG_DIR
StandardOutput=journal
StandardError=journal
LimitNOFILE=65536

[Install]
WantedBy=default.target
EOF

cat > "$SYSTEMD_USER_DIR/caesar-watchdog.service" <<EOF
[Unit]
Description=Caesar Watchdog
After=caesar-daemon.service

[Service]
Type=simple
ExecStart=$VENV_DIR/bin/python -m caesar.watchdog
Restart=always
RestartSec=10
Environment=CAESAR_CONFIG_DIR=$CONFIG_DIR
Environment=CAESAR_DATA_DIR=$DATA_DIR
Environment=CAESAR_LOG_DIR=$LOG_DIR

[Install]
WantedBy=default.target
EOF

info "Systemd user services created"

# === 9. Setup wizard ===
if [[ "$SKIP_SETUP" != "true" && "$NON_INTERACTIVE" != "true" ]]; then
    echo ""
    echo -e "${BOLD}🤖 Setup wizard${NC}"
    echo "----------------"
    
    if [[ -t 0 ]]; then
        "$VENV_DIR/bin/python" -m caesar.setup || warn "Setup wizard failed"
    else
        warn "Не интерактивный shell, пропускаю setup. Запусти 'caesar setup' позже."
    fi
fi

# === 10. Linger ===
if [[ -z "$ENABLE_LINGER" && -t 0 ]]; then
    if [[ "$NON_INTERACTIVE" != "true" ]]; then
        read -r -p "Включить always-on режим (systemd lingering)? [Y/n]: " linger_ans
        linger_ans="${linger_ans:-y}"
        if [[ "$linger_ans" =~ ^[YyДд]$ ]]; then
            ENABLE_LINGER="true"
        else
            ENABLE_LINGER="false"
        fi
    fi
fi

if [[ "$ENABLE_LINGER" == "true" ]]; then
    run_privileged loginctl enable-linger "$(id -un)"
    info "Lingering enabled — сервисы будут работать после выхода"
fi

# === 11. Start services ===
if [[ "$START_SERVICES" == "true" ]]; then
    step "Starting Caesar services..."
    
    if systemctl --user show-environment >/dev/null 2>&1; then
        systemctl --user daemon-reload
        systemctl --user enable --now caesar-daemon caesar-watchdog
        sleep 2
        if systemctl --user is-active --quiet caesar-daemon; then
            info "Daemon is running"
        else
            warn "Daemon failed to start. Check: journalctl --user -u caesar-daemon -e"
        fi
    else
        warn "User systemd не готов. Перезайди в shell и запусти:"
        echo "  systemctl --user daemon-reload"
        echo "  systemctl --user enable --now caesar-daemon caesar-watchdog"
    fi
fi

# === 12. Done ===
echo ""
echo -e "${GREEN}${BOLD}✅ Caesar installed successfully!${NC}"
echo ""
echo "Layout:"
echo "  Repo:     $REPO_DIR"
echo "  Venv:     $VENV_DIR"
echo "  Data:     $DATA_DIR"
echo "  Config:   $CONFIG_DIR/config.yaml"
echo "  CLI:      $LOCAL_BIN_DIR/caesar"
echo ""
echo "What's next:"
echo ""
if [[ ":$PATH:" == *":$LOCAL_BIN_DIR:"* ]]; then
    LAUNCH="caesar"
else
    LAUNCH="$LOCAL_BIN_DIR/caesar"
fi

echo "  $LAUNCH --status           # статус daemon"
echo "  $LAUNCH 'привет'           # one-shot"
echo "  $LAUNCH                    # REPL"
echo "  $LAUNCH setup              # переконфигурировать"
echo "  $LAUNCH pair               # привязать бота к твоему Telegram (безопасность!)"
echo ""
echo "  # Логи:"
echo "  journalctl --user -u caesar-daemon -f"
echo ""
echo "  # Управление сервисами:"
echo "  systemctl --user status caesar-daemon"
echo "  systemctl --user restart caesar-daemon"
echo "  systemctl --user stop caesar-daemon caesar-watchdog"
echo ""
echo "Docs: https://github.com/madlenprust/Caesar-agent#readme"
echo ""
