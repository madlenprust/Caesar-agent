# Caesar 🏛️

> Автономный AI-агент для Ubuntu без GUI. Аналог OpenClaw и Hermes, но с настоящей автономностью, трёхуровневой памятью и экономией токенов как главным KPI.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![Ubuntu 22.04+](https://img.shields.io/badge/Ubuntu-22.04+-E95420)](https://ubuntu.com/)

## Что такое Caesar

Caesar — это **автономный** AI-агент: пользователь даёт задачу → агент молча уходит работать → возвращается с готовым результатом.

**Главный анти-паттерн** (что НЕ делаем):
> Агент заканчивает ответ вопросом «хочешь, продолжу?» → пользователь 15 раз отвечает «да». Запрещено.

### Ключевые особенности

- 🤖 **Полная автономия** — работает сам, спрашивает только когда реально нужно
- 💰 **Экономия токенов** — главный KPI архитектуры (lazy loading, две LLM, скиллы)
- 🧠 **Четырёхуровневая память** — L1 (RAM) / L2 (SQLite temporal) / L3 (vector) / L4 (skills)
- 🔧 **27 инструментов** — shell, files, web, RSS, TG, документы, и т.д.
- ⏰ **Cron через разговор** — «каждый день в 9:00 делай дайджест»
- 📡 **Telegram + CLI** — общайся как удобно
- 🛡 **Self-knowledge** — агент понимает свою архитектуру и может безопасно расширять себя
- 🚫 **Никакого «хочешь, продолжим?»** — Tool-First Enforcement на уровне кода

## Установка

### Одна команда (Ubuntu 22.04+, user-space)

```bash
curl -fsSL https://raw.githubusercontent.com/madlenprust/caesar/main/install.sh | bash
```

Установщик:
1. Поставит системные пакеты (Python 3.12+, git, curl, tesseract, poppler)
2. Склонирует репозиторий в `~/caesar`
3. Создаст venv в `~/.local/share/caesar/venv`
4. Поставит зависимости
5. Создаст systemd user-сервисы (`caesar-daemon`, `caesar-watchdog`)
6. Запустит setup wizard (LLM-ключ, TG-бот, режим)
7. Запустит daemon

### Layout (всё в домашней папке, без sudo для кода)

| Что | Где |
|---|---|
| Код | `~/caesar/` |
| venv | `~/.local/share/caesar/venv/` |
| Данные (БД, скиллы) | `~/.local/share/caesar/data/` |
| Конфиги (с API ключами) | `~/.config/caesar/` |
| Логи | `~/.local/share/caesar/log/` |
| Сокет | `~/.local/share/caesar/caesar.sock` |
| Systemd user-сервисы | `~/.config/systemd/user/` |
| CLI | `~/.local/bin/caesar` |

### LLM-провайдеры (5 вариантов в setup)

1. **OpenAI** — gpt-4o, gpt-4o-mini
2. **Anthropic** — claude-3-5-sonnet, claude-3-5-haiku
3. **Z.ai** — glm-4.6, glm-4-flash
4. **Ollama** — локально (llama3.1 и др.)
5. **Custom** — любой OpenAI-совместимый endpoint (DeepSeek, Groq, Together, OpenRouter, vLLM, LM Studio, и т.д.)

## Использование

### CLI

```bash
# One-shot
caesar "найди новости про Rust"

# REPL
caesar
> найди новости про Rust
> а погоду?
> /exit

# Статус
caesar --status

# Справка
caesar --help
```

### Management команды

```bash
caesar setup                    # переконфигурировать (LLM, TG, режим)
caesar update                   # обновить через git pull
caesar rollback                 # откатиться к предыдущей версии
caesar uninstall                # удалить Caesar
caesar stats                    # статистика по токенам
caesar permissions list         # список разрешений
caesar permissions reset        # сбросить все разрешения
```

### Управление daemon (systemd user)

```bash
systemctl --user start caesar-daemon
systemctl --user stop caesar-daemon
systemctl --user restart caesar-daemon
systemctl --user status caesar-daemon
journalctl --user -u caesar-daemon -f       # смотреть логи
```

### Telegram

1. Создай бота через [@BotFather](https://t.me/BotFather)
2. Запусти `caesar setup`, введи токен
3. Напиши боту в Telegram

Прогресс выполнения виден как карточка:
```
Caesar: 🧠 🔍 📄 🧠 💾
```
При финале карточка заменяется на ответ.

### Cron через разговор

```
Ты: "Каждый день в 9:00 делай дайджест новостей и публикуй в @news"

Caesar: 📅 Расписание задач
        Понял так:
        - Когда: каждый день в 09:00
        - Что: дайджест новостей, публикация в @news
        Всё верно? [Да / Нет]
```

Понимает: «каждый день в N», «по будням», «каждый понедельник», «каждые N минут/часов», «раз в неделю», «каждое утро/вечер».

## Архитектура

```
┌─────────────────────────────────────────────────┐
│  CAESAR DAEMON (один процесс, systemd user)      │
│                                                  │
│  ┌──────────┐  ┌──────────┐  ┌───────────────┐  │
│  │ Память   │  │ Tools    │  │ LLM Router    │  │
│  │ L1/L2/L3 │  │ 27 шт    │  │ smart/cheap   │  │
│  │ /L4      │  │          │  │               │  │
│  └──────────┘  └──────────┘  └───────────────┘  │
│         │                                      │
│         ▼                                       │
│  ┌──────────────────────────────────────────┐  │
│  │  Оркестратор                             │  │
│  │  ReAct + Skill-First + Tool-First        │  │
│  │  Адаптивная рефлексия (4 режима A/B)     │  │
│  └──────────────────────────────────────────┘  │
│         │                                       │
│         ▼                                       │
│  ┌──────────────────────────────────────────┐  │
│  │  Очередь задач                           │  │
│  │  5 интерактивных + 10 фоновых workers    │  │
│  └──────────────────────────────────────────┘  │
│         │                                       │
│         ▼                                       │
│  ┌──────────────────────────────────────────┐  │
│  │  Каналы (capability-based rendering)     │  │
│  │  Telegram | CLI | Web (TODO)             │  │
│  └──────────────────────────────────────────┘  │
└─────────────────────────────────────────────────┘
       ▲                          ▲
       │                          │
   пользователь                cron / webhook
```

### Память (4 уровня)

| Уровень | Где | Что хранит | Поиск |
|---|---|---|---|
| **L1** | RAM | Рабочий контекст текущей задачи | Прямо, мгновенно |
| **L2** | SQLite (temporal) | Факты по каналам (новые вперёд) | SQL от новых к старым |
| **L3** | sqlite-vec | Полные тексты, векторная база | BGE-M3 + reranker |
| **L4** | YAML + SQLite | Скиллы (точные рецепты) | По trigger-описанию |

### LLM-роутер

- **Дешёвая LLM** (gpt-4o-mini, claude-3-haiku, glm-4-flash) — анализирует запрос, извлекает факты, проверяет тривиальность
- **Умная LLM** (gpt-4o, claude-3-sonnet, glm-4.6) — планирует, отвечает, самопроверка
- **Скилл найден** → код выполняет recipe (0 LLM вызовов)
- **Скилл не найден** → умная LLM в ReAct цикле

Поддержка: OpenAI, Anthropic, Z.ai (GLM), Ollama, любой OpenAI-совместимый endpoint.

### Инструменты (27 шт)

| Категория | Инструменты |
|---|---|
| Shell + Files | shell_exec, read_file, write_file, edit_file, find_files, grep |
| Интернет | web_search (DDG), web_fetch, http_request |
| Источники | rss_read, tg_read_channel, hn_search, reddit_search, wikipedia_read |
| Документы | parse_pdf, parse_docx, parse_xlsx, parse_csv |
| Память | memory_search, memory_add_fact, skill_find, skill_save |
| Self-knowledge | self_read, self_edit, self_install_package, self_scan, self_test |

### Режимы доступа

| Режим | Права | Self-edit |
|---|---|---|
| **sandboxed** (default) | Песочница, отдельный юзер `caesar` | ❌ |
| **autonomous** | Права твоего юзера | ✅ (с git+тесты) |
| **full** | sudo | ✅ |

## Документация

- [Полный roadmap](download/PROJECT_ROADMAP.md) — спецификация архитектуры (2372 строки)
- [Принципы агента](var/lib/caesar/self/PRINCIPLES.md) — заблокированы от авто-редактирования
- [Архитектура для самого агента](var/lib/caesar/self/ARCHITECTURE.md)

## Разработка

```bash
git clone https://github.com/madlenprust/caesar.git
cd caesar
python3.12 -m venv venv
source venv/bin/activate
pip install -e ".[dev]"

# Тесты (TODO)
pytest

# Линтер
ruff check caesar/
```

## Лицензия

MIT — используй свободно.
