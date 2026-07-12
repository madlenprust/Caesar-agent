# Changelog агента

> Что изменилось в самом агенте.

## V0.3 (2026-06-28)

- Added: LLM-роутер с поддержкой OpenAI, Anthropic, Z.ai, Ollama
- Added: Память L2 (SQLite temporal schema) — факты с valid_from/valid_until
- Added: Память L3 (векторная, BGE-M3, cosine similarity)
- Added: Память L4 (скиллы, YAML + SQLite, версионные)
- Added: 27 инструментов в 6 категориях
- Added: Оркестратор с ReAct циклом и Tool-First Enforcement
- Added: Адаптивная рефлексия (4 режима для A/B тестирования)
- Added: Счётчик токенов с логированием
- Added: Telegram-адаптер (Bot API, карточки эмодзи, кнопки)
- Added: Cron через APScheduler

## V0.2 (2026-06-28)

- Added: Шина событий (EventBus, нейтральные events)
- Added: Очередь задач (5+10 workers, без прерываний)
- Added: Логирование (journald + файлы)
- Added: Конфигурация (YAML + dataclasses)

## V0.1 (2026-06-28)

- Initial: Скелет проекта
- Initial: Daemon + CLI thin client через unix socket
- Initial: Эхо-режим (без LLM)
