# Архитектура агента

> Документ для самого агента — чтобы он понимал, как устроен, и мог безопасно
> расширять себя. Читай перед self-edit (PRINCIPLES #10).

## Деплой (user-space, без sudo для кода)
- Код: `~/caesar/` (git clone)
- venv: `~/.local/share/caesar/venv/`
- Данные (БД, скиллы, self-knowledge): `~/.local/share/caesar/data/`
- Конфиг (секреты — НЕ в git): `~/.config/caesar/`
- Логи: `~/.local/share/caesar/log/`
- Сокет: `~/.local/share/caesar/caesar.sock`
- Systemd user: `caesar-daemon.service`, `caesar-watchdog.service`

## Модули
### core/
- `daemon.py` — главный демон (systemd)
- `events.py` — шина событий (capability-based rendering)
- `queue.py` — очередь задач (interactive + background workers), персистится в БД
- `orchestrator.py` — оркестратор: ReAct + Skill-First + Tool-First + loop-detector + /stop
- `llm.py` — LLM-роутер: cheap анализирует, smart отвечает
- `cron.py` — cron через разговор + quiet hours (deferred, не skip)
- `briefing.py` — утренний дайджест
- `status.py` — расширенный /status
- `dream.py` — ночной цикл памяти (entity sweep, topic consolidation)
- `skill_executor.py` — выполнение рецептов скиллов

### memory/
- `storage.py` — SQLite (users, channels, tasks, l2_facts, l3_chunks, l4_skills, kg_entities, kg_relations, cron_tasks, permissions, token_usage)
- `l3.py` — векторная память (embeddings + cosine)
- `l4.py` — скиллы (YAML + SQLite, версионные, consecutive_failures)
- `knowledge_graph.py` — сущности и отношения

### tools/
- shell+files, интернет (web_search multi-engine, web_fetch, http_request),
  источники (RSS, HN, reddit, wikipedia, TG-каналы), документы (pdf/docx/xlsx/csv),
  память, self-knowledge

### channels/
- `cli_adapter.py` — CLI (через unix socket к daemon)
- `telegram_adapter.py` — Telegram Bot API (long polling, карточки эмодзи)

### watchdog/
- Надзор: зависшие задачи, анализ диалогов, re-delivery cron (читает tasks из БД)

## Поток данных
1. Сообщение → channel adapter → user_id + channel_id
2. Создаётся task в queue (пишется в БД — иначе watchdog слеп)
3. Worker берёт task → orchestrator.handle_task
4. Orchestrator: progress_start → cheap-LLM анализ → skill-first / smart-LLM ReAct
   с инструментами → answer_ready (каждый tool_call → progress_update)
5. Adapter рендерит events в формат канала

## Режимы доступа
- `sandboxed` — песочница
- `autonomous` — права пользователя, self_edit
- `full` — sudo

**exact_deny** (`rm -rf /`, `mkfs`, `dd of=/dev/`, `chmod -R 777 /`, fork-bomb)
в sandboxed НЕ отключается; в god_mode/full — отключается (владелец, бот привязан
→ god только у owner). **remote_exec** — SSH на соседние машины (god/full);
в sandboxed запрещён.

## Self-modification
- `PRINCIPLES.md` — заблокирован от авто-редактирования
- `ARCHITECTURE.md` / `CODE_MAP.md` — обновляются через self_edit (с git)
- Все изменения — через git, можно откатить
