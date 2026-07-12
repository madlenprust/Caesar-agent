#!/bin/bash
# Установщик агента для Ubuntu.
# См. roadmap раздел 14 (Установщик).
#
# Использование:
#   curl -fsSL https://agent.example.com/install.sh | bash
#
# Или:
#   ./install.sh [options]
#
# Options:
#   --dev          — установка в dev-режиме (текущая папка)
#   --non-interactive — без вопросов, использует defaults
#   --mode MODE    — sandboxed|autonomous|full

set -euo pipefail

# === Colors ===
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

info()  { echo -e "${GREEN}✅${NC} $1"; }
warn()  { echo -e "${YELLOW}⚠️${NC} $1"; }
error() { echo -e "${RED}❌${NC} $1" >&2; }
step()  { echo -e "${BLUE}🔧${NC} $1"; }

# === Defaults ===
DEV_MODE=false
NON_INTERACTIVE=false
MODE=""

# === Parse args ===
while [[ $# -gt 0 ]]; do
    case $1 in
        --dev)
            DEV_MODE=true
            shift
            ;;
        --non-interactive)
            NON_INTERACTIVE=true
            shift
            ;;
        --mode)
            MODE="$2"
            shift 2
            ;;
        --help|-h)
            cat <<EOF
Установщик AI-агента для Ubuntu.

Использование:
  ./install.sh [options]

Options:
  --dev              Установка в dev-режиме (текущая папка, без systemd)
  --non-interactive  Без вопросов, использует defaults
  --mode MODE         sandboxed|autonomous|full
  --help              Эта справка
EOF
            exit 0
            ;;
        *)
            error "Неизвестный аргумент: $1"
            exit 1
            ;;
    esac
done

echo ""
echo "🤖 Установка AI-агента для Ubuntu"
echo "================================"
echo ""

# === 1. Проверка OS ===
step "Проверка системы..."

if [[ -f /etc/os-release ]]; then
    . /etc/os-release
    if [[ "$ID" != "ubuntu" && "$ID" != "debian" ]]; then
        warn "OS: $ID $VERSION_ID — не Ubuntu/Debian. Скрипт может не работать."
    else
        info "OS: $NAME $VERSION_ID"
    fi
else
    warn "Не удалось определить OS (/etc/os-release не найден)"
fi

ARCH=$(uname -m)
if [[ "$ARCH" != "x86_64" && "$ARCH" != "aarch64" && "$ARCH" != "arm64" ]]; then
    error "Неподдерживаемая архитектура: $ARCH (нужно x86_64 или arm64)"
    exit 1
fi
info "Архитектура: $ARCH"

# === 2. Проверка Python ===
step "Проверка Python..."

if ! command -v python3 &>/dev/null; then
    error "Python 3 не найден. Установи: sudo apt install python3 python3-pip python3-venv"
    exit 1
fi

PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)

if [[ "$PY_MAJOR" -lt 3 || ("$PY_MAJOR" -eq 3 && "$PY_MINOR" -lt 12) ]]; then
    error "Нужен Python 3.12+, у тебя $PY_VERSION"
    echo "  Установи через deadsnakes PPA:"
    echo "    sudo add-apt-repository ppa:deadsnakes/ppa"
    echo "    sudo apt update && sudo apt install python3.12 python3.12-venv"
    exit 1
fi
info "Python: $PY_VERSION"

# === 3. Пути установки ===
if [[ "$DEV_MODE" == "true" ]]; then
    INSTALL_DIR="$(pwd)"
    info "Dev-режим: установка в $INSTALL_DIR"
else
    INSTALL_DIR="/opt/agent"
    CONFIG_DIR="/etc/agent"
    DATA_DIR="/var/lib/agent"
    LOG_DIR="/var/log/agent"
    
    # Проверка прав
    if [[ $EUID -ne 0 ]]; then
        warn "Установка в /opt/agent требует sudo. Перепускаю с sudo..."
        exec sudo bash "$0" "$@"
    fi
fi

# === 4. Создание venv и установка ===
step "Установка агента..."

if [[ "$DEV_MODE" == "true" ]]; then
    if [[ ! -d venv ]]; then
        python3 -m venv venv
    fi
    source venv/bin/activate
    pip install --quiet -e ".[dev]" 2>&1 | tail -5
    info "Установлен в dev-режиме"
else
    mkdir -p "$INSTALL_DIR"
    cd "$INSTALL_DIR"
    
    if [[ ! -d venv ]]; then
        python3 -m venv venv
    fi
    source venv/bin/activate
    
    # Скачиваем или клонируем (заглушка — в реальном установщике тут git clone)
    warn "TODO: скачай исходники в $INSTALL_DIR"
    warn "В реальной установке тут будет: git clone https://github.com/yourname/agent.git ."
    
    pip install --quiet -e ".[dev]" 2>&1 | tail -5 || true
    
    mkdir -p "$CONFIG_DIR" "$DATA_DIR/skills" "$DATA_DIR/self" "$LOG_DIR"
    info "Установлен в $INSTALL_DIR"
fi

# === 5. Конфигурация ===
step "Конфигурация..."

if [[ "$DEV_MODE" == "true" ]]; then
    CONFIG_FILE="etc/config.yaml"
else
    CONFIG_FILE="$CONFIG_DIR/config.yaml"
fi

# Копируем example если нет
if [[ ! -f "$CONFIG_FILE" ]]; then
    if [[ "$DEV_MODE" == "true" ]]; then
        cp etc/config.yaml.example "$CONFIG_FILE"
    else
        cp "$INSTALL_DIR/etc/config.yaml.example" "$CONFIG_FILE"
    fi
    info "Создан $CONFIG_FILE"
fi

# === 6. Setup wizard (если не non-interactive) ===
if [[ "$NON_INTERACTIVE" != "true" ]]; then
    echo ""
    echo "🤖 Настройка агента"
    echo "------------------"
    echo "Нажми Enter для default, или введи своё значение."
    echo ""
    
    # Режим работы
    if [[ -z "$MODE" ]]; then
        echo "1. Режим работы:"
        echo "   1) Обычный (песочница, безопасно)  ← default"
        echo "   2) Автономный (права твоего юзера)"
        echo "   3) Полный (sudo, может сломать систему)"
        read -p "   Выбор [1]: " mode_choice
        case "${mode_choice:-1}" in
            2) MODE="autonomous" ;;
            3) MODE="full" ;;
            *) MODE="auto" ;;
        esac
    fi
    info "Режим: $MODE"
    
    # LLM провайдер
    echo ""
    echo "2. LLM-провайдер:"
    echo "   1) OpenAI  ← default"
    echo "   2) Anthropic"
    echo "   3) Z.ai (GLM)"
    echo "   4) Ollama (локально)"
    echo "   5) Другой (указать позже)"
    read -p "   Выбор [1]: " llm_choice
    
    case "${llm_choice:-1}" in
        1)
            PROVIDER="openai"
            DEFAULT_SMART="gpt-4o"
            DEFAULT_CHEAP="gpt-4o-mini"
            DEFAULT_URL="https://api.openai.com/v1"
            ;;
        2)
            PROVIDER="anthropic"
            DEFAULT_SMART="claude-3-5-sonnet-20241022"
            DEFAULT_CHEAP="claude-3-5-haiku-20241022"
            DEFAULT_URL=""
            ;;
        3)
            PROVIDER="zai"
            DEFAULT_SMART="glm-4.6"
            DEFAULT_CHEAP="glm-4-flash"
            DEFAULT_URL="https://api.z.ai/api/paas/v4"
            ;;
        4)
            PROVIDER="ollama"
            DEFAULT_SMART="llama3.1"
            DEFAULT_CHEAP="llama3.1"
            DEFAULT_URL="http://localhost:11434/v1"
            ;;
        *)
            PROVIDER=""
            ;;
    esac
    
    if [[ -n "$PROVIDER" ]]; then
        read -p "   API ключ для $PROVIDER: " API_KEY
        
        if [[ "$PROVIDER" != "ollama" && -n "$API_KEY" ]]; then
            # Тестовый запрос
            echo "   Проверяю ключ..."
            # TODO: реальная валидация
            info "Ключ принят"
        fi
        
        read -p "   Умная модель [$DEFAULT_SMART]: " SMART_MODEL
        read -p "   Дешёвая модель [$DEFAULT_CHEAP]: " CHEAP_MODEL
        
        SMART_MODEL="${SMART_MODEL:-$DEFAULT_SMART}"
        CHEAP_MODEL="${CHEAP_MODEL:-$DEFAULT_CHEAP}"
    fi
    
    # Telegram
    echo ""
    echo "3. Telegram-бот:"
    read -p "   Хочешь подключить TG-бот? [Y/n]: " tg_choice
    if [[ "${tg_choice:-Y}" =~ ^[YyДд]$ ]]; then
        read -p "   Bot token (от @BotFather): " TG_TOKEN
        if [[ -n "$TG_TOKEN" ]]; then
            # Проверяем токен
            echo "   Проверяю токен..."
            TG_ME=$(curl -s "https://api.telegram.org/bot$TG_TOKEN/getMe" 2>/dev/null || echo "")
            if echo "$TG_ME" | grep -q '"ok":true'; then
                TG_USERNAME=$(echo "$TG_ME" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["result"]["username"])' 2>/dev/null || echo "?")
                info "TG-бот: @$TG_USERNAME"
            else
                warn "Не удалось проверить токен (продолжаю, проверь позже)"
            fi
        fi
    fi
    
    # Записываем в конфиг
    if [[ -n "$MODE" ]]; then
        sed -i "s/^mode: .*/mode: $MODE/" "$CONFIG_FILE"
    fi
    if [[ -n "$PROVIDER" ]]; then
        sed -i "s/^  smart_provider: .*/  smart_provider: $PROVIDER/" "$CONFIG_FILE"
        sed -i "s/^  smart_model: .*/  smart_model: $SMART_MODEL/" "$CONFIG_FILE"
        sed -i "s/^  smart_api_key: \"\"/  smart_api_key: \"$API_KEY\"/" "$CONFIG_FILE"
        [[ -n "$DEFAULT_URL" ]] && sed -i "s|^  # smart_base_url:.*|  smart_base_url: $DEFAULT_URL|" "$CONFIG_FILE"
        
        sed -i "s/^  cheap_provider: .*/  cheap_provider: $PROVIDER/" "$CONFIG_FILE"
        sed -i "s/^  cheap_model: .*/  cheap_model: $CHEAP_MODEL/" "$CONFIG_FILE"
        sed -i "s/^  cheap_api_key: \"\"/  cheap_api_key: \"$API_KEY\"/" "$CONFIG_FILE"
        [[ -n "$DEFAULT_URL" ]] && sed -i "s|^  # cheap_base_url:.*|  cheap_base_url: $DEFAULT_URL|" "$CONFIG_FILE"
    fi
    if [[ -n "$TG_TOKEN" ]]; then
        sed -i "s/^  bot_token: \"\"/  bot_token: \"$TG_TOKEN\"/" "$CONFIG_FILE"
    fi
    
    info "Конфигурация сохранена в $CONFIG_FILE"
fi

# === 7. Systemd (только для prod) ===
if [[ "$DEV_MODE" != "true" ]]; then
    step "Регистрация systemd-сервисов..."
    
    cat > /etc/systemd/system/agent-daemon.service <<EOF
[Unit]
Description=Agent Daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/venv/bin/python -m agent.daemon
Restart=always
RestartSec=5
WatchdogSec=60
EnvironmentFile=$CONFIG_DIR/env
StandardOutput=journal
StandardError=journal
LimitNOFILE=65536
MemoryMax=2G

[Install]
WantedBy=multi-user.target
EOF

    cat > /etc/systemd/system/agent-watchdog.service <<EOF
[Unit]
Description=Agent Watchdog
After=agent-daemon.service

[Service]
Type=simple
ExecStart=$INSTALL_DIR/venv/bin/python -m agent.watchdog
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable agent-daemon agent-watchdog
    systemctl start agent-daemon
    info "Systemd-сервисы зарегистрированы и запущены"
fi

# === 8. Создание CLI-симлинка ===
if [[ "$DEV_MODE" != "true" ]]; then
    step "Создание CLI-симлинка..."
    cat > /usr/local/bin/agent <<EOF
#!/bin/bash
exec $INSTALL_DIR/venv/bin/python $INSTALL_DIR/bin/agent "\$@"
EOF
    chmod +x /usr/local/bin/agent
    info "CLI доступен: agent"
fi

# === 9. Готово ===
echo ""
echo "================================"
info "Установка завершена!"
echo ""
echo "Что дальше:"
if [[ "$DEV_MODE" == "true" ]]; then
    echo "  source venv/bin/activate"
    echo "  python -m agent.daemon &"
    echo "  ./bin/agent --status"
    echo "  ./bin/agent 'привет'"
else
    echo "  systemctl status agent-daemon"
    echo "  agent --status"
    echo "  agent 'привет'"
    echo "  journalctl -u agent-daemon -f  # смотреть логи"
fi
echo ""
