# Roadmap агента

> Куда развиваемся: что сделано, что в работе, планы.

## Готово
- ✅ Скелет (daemon + CLI + unix socket), конфиг, логирование, шина событий,
  очередь задач (персистится в БД).
- ✅ Оркестратор (ReAct + Skill-First + Tool-First + loop-detector), LLM-роутер
  (multi-provider: OpenAI, Anthropic, Z.ai, Ollama, custom).
- ✅ Память L2 (temporal facts) / L3 (vector) / L4 (skills), Knowledge Graph.
- ✅ Инструменты: shell+files, web, источники (RSS/HN/reddit/wikipedia/TG),
  документы, self-knowledge.
- ✅ Telegram + CLI, Cron (+ quiet hours deferred), Watchdog, Dream Cycle,
  Morning Briefing.
- ✅ CLI: setup/update/rollback/doctor/db/kg/config/skill/stats/cron.
- ✅ Security: `exact_deny` всегда, `allowed_chat_ids`, `/stop`.

## В работе / планы
- 🔻 Реальный token-budget packing + numpy/sqlite-vec для L3 (сейчас char-budget
  + pure-Python cosine, full-scan).
- 🔻 Knowledge Graph как semantic triples (subject→relation→object вместо
  regex + co-occurrence); интеграция в L3-ranking.
- 🔻 Provider pacing (inter-request spacing) + meaning-based классификация
  ошибок LLM (сейчас по HTTP-коду).
- 🔻 Pause/resume mid-command (`interruption_check` в subprocess poll).
- 🔻 Web-панель, webhook.

## Принципы развития
1. Сначала простая рабочая версия, потом усложняем.
2. Каждый модуль тестируется отдельно перед интеграцией.
3. Roadmap обновляется после каждой закрытой темы.
4. Никаких «хотим сделать X» без конкретного сценария использования.
