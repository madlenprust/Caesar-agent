# Roadmap развития агента

> Куда развиваемся: что сделано, что в работе, планы.

## Готово (V0.3)

- ✅ Скелет проекта (daemon + CLI thin client через unix socket)
- ✅ Конфигурация (YAML + dataclasses)
- ✅ Логирование (journald + файлы с ротацией)
- ✅ Шина событий (EventBus, нейтральные events)
- ✅ Очередь задач (5 интерактивных + 10 фоновых workers)
- ✅ Оркестратор (ReAct + Skill-First + Tool-First Enforcement)
- ✅ LLM-роутер (cheap + smart, multi-provider: OpenAI, Anthropic, Z.ai, Ollama)
- ✅ Память L2 (SQLite temporal schema)
- ✅ Память L3 (векторная, BGE-M3)
- ✅ Память L4 (скиллы, YAML + SQLite)
- ✅ 27 инструментов (shell+files, интернет, источники, документы, память, self-knowledge)
- ✅ CLI-адаптер (one-shot + REPL)
- ✅ Telegram-адаптер (Bot API, карточки эмодзи, кнопки)
- ✅ Cron (APScheduler + SQLite jobstore)

## В работе (V0.7)

- 🔄 Наблюдатель (watchdog процесс)
- 🔄 Self-knowledge (этот документ + CODE_MAP.md)
- 🔄 Self-modification (self_edit с git+тесты)

## Запланировано (V1.0)

- ⬜ Установщик (`curl ... | bash`, setup wizard)
- ⬜ Systemd-юниты (agent-daemon.service, agent-watchdog.service)
- ⬜ `agent update`, `agent rollback`, `agent uninstall`
- ⬜ Базовые скиллы (add_llm_provider, setup_tg_channel, и т.д.)

## Запланировано (V1.1+)

- ⬜ Web-панель (для просмотра истории/логов)
- ⬜ Webhook (для внешних интеграций)
- ⬜ Голосовой ввод через Whisper
- ⬜ Саморасширение кода (агент дописывает новые инструменты)

## Принципы развития

1. Сначала простая рабочая версия, потом усложняем
2. Каждый модуль тестируется отдельно перед интеграцией
3. Roadmap обновляется после каждой закрытой темы
4. Никаких «хотим сделать X» без конкретного сценария использования
