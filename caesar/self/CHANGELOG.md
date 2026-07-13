# Changelog агента

> Что изменилось в самом агенте.

## v0.6 (2026-07-13)

- Security: `allowed_chat_ids` (opt-in авторизация TG); `exact_deny` всегда
  срабатывает (god_mode/full больше не обходят чёрный список); харденинг
  `is_dangerous_command` — chaining (`&& ; |`), `$()`/backticks/eval, reorder
  флагов (`rm -fr /`), fork-bomb.
- Задачи пишутся в БД при работе → watchdog жив: зависшие задачи + re-delivery
  cron-результатов работают.
- L4: колонка `consecutive_failures` (3 подряд → broken, не lifetime);
  `record_success` больше не падает; `_sync_from_yaml` не inflate'ит version
  на рестарте.
- `/stop` реально останавливает задачу (`Task.cancelled` — worker не resurrect'ит,
  цикл выходит).
- cron quiet hours → DEFERRED (перенос firing на конец тихих часов, а не skip).

## v0.5

- Knowledge Graph (сущности + отношения), Dream Cycle (ночной цикл памяти),
  Morning Briefing, Quiet Hours.
- CLI: `caesar doctor / db / kg / config / skill / stats / cron`.
- Loop-detector + multi-engine web_search; расширенный `/status`.

## v0.3

- Скелет: daemon + CLI thin client (unix socket), шина событий, очередь задач,
  оркестратор (ReAct + Skill-First + Tool-First), LLM-роутер (multi-provider),
  память L2 (temporal) / L3 (vector) / L4 (skills), инструменты, TG-адаптер, cron.
