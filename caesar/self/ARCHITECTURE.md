# Архитектура агента

> Документ для самого агента — чтобы он понимал как устроен и мог безопасно расширять себя.
> См. roadmap раздел 8 (Self-Knowledge Layer).

## Модули

### core/ — ядро
- `daemon.py` — главный daemon, запускается через systemd
- `events.py` — шина событий (capability-based rendering)
- `queue.py` — очередь задач (5 интерактивных + 10 фоновых workers)
- `orchestrator.py` — оркестратор: ReAct + Skill-First + Tool-First
- `llm.py` — LLM-роутер: дешёвая анализирует, умная отвечает
- `cron.py` — APScheduler, cron-задачи через разговор с пользователем

### memory/ — память
- `storage.py` — SQLite: users, channels, tasks, l2_facts, l3_chunks, l4_skills, cron_tasks, permissions, token_usage
- `l3.py` — векторная память (BGE-M3 + cosine similarity)
- `l4.py` — скиллы (YAML + SQLite, версионные)

### tools/ — инструменты (27 шт в V0.3)
- `base.py` — базовый класс Tool, exact_deny
- `shell_files.py` — shell_exec, read_file, write_file, edit_file, find_files, grep
- `internet.py` — web_search (DDG), web_fetch, http_request
- `sources.py` — rss_read, tg_read_channel, hn_search, reddit_search, wikipedia_read
- `documents.py` — parse_pdf, parse_docx, parse_xlsx, parse_csv
- `memory_tools.py` — memory_search, memory_add_fact, skill_find, skill_save
- `self_knowledge.py` — self_read, self_edit, self_install_package, self_scan, self_test

### channels/ — каналы ввода/вывода
- `cli_adapter.py` — CLI (через unix socket к daemon)
- `telegram_adapter.py` — Telegram Bot API (long polling)

### watchdog/ — наблюдатель
- Отдельный процесс, раз в 2 мин проверяет что daemon жив

## Поток данных

1. Сообщение от пользователя → channel adapter (TG/CLI)
2. Adapter определяет user_id и channel_id
3. Создаёт task в queue
4. Worker (из 5 интерактивных или 10 фоновых) берёт task
5. Worker вызывает orchestrator.handle_task(task)
6. Orchestrator:
   a. Эмитит progress_start event
   b. Дешёвая LLM анализирует запрос
   c. Если тривиальный — отвечает сама
   d. Иначе умная LLM в ReAct цикле с инструментами
   e. Каждый tool_call → progress_update event
   f. Эмитит answer_ready event
7. Channel adapter рендерит events в формат канала
8. Пользователь видит карточку с эмодзи → финальный ответ

## Ключевые принципы (см. PRINCIPLES.md)

1. Экономия токенов — главный KPI
2. Lazy loading контекста
3. Stateless L1 (пересобирается каждый ход)
4. Temporal facts в L2
5. Tool-First Enforcement (физический запрет «хочешь, продолжим?»)

## Режимы доступа

- `sandboxed` — обычный, агент в песочнице
- `autonomous` — права пользователя, может self_edit
- `full` — sudo, может всё

## Деплой

- `/opt/agent/` — код (Python venv)
- `/etc/agent/` — конфиги
- `/var/lib/agent/` — данные (SQLite, скиллы, self-knowledge)
- `/var/log/agent/` — логи
- `/run/agent.sock` — unix socket для CLI

## Systemd

- `agent-daemon.service` — главный процесс
- `agent-watchdog.service` — наблюдатель
