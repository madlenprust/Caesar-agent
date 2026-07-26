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
- ✅ **0.10.0**: L3 numpy batch cosine + token-budget packing (4000 L3 / 2000 history) + KG-boost в ranking (+15%).
- ✅ **0.11.0**: provider pacing (0.5с inter-request) + meaning-based LLM error classification (`classify_http_error`) + pause/resume mid-command (`/pause` `/resume`).
- ✅ **0.11.1**: hardening policy — always-on снос локальной системы (roots-only rm), форматирование диска + remote разрешены.

## В работе / планы

### 🧠 Memory Transparency (вдохновлено obsidian-mind, адаптировано под caesar)
Цель: inspectability/trust — пользователь видит, что агент «знает», и правит —
не ломая DB-backed дизайн (L1-L4 + KG canonical). Крадём **слой прозрачности**,
НЕ markdown-vault-as-primary-storage.

**T1. Категоризация L2** (`decision` / `win` / `incident` / `fact` / `preference`). ✅ DONE (0.12.0)
- Миграция: колонка `category` в `l2_facts` (дефолт `fact` для существующих).
- `LLMRouter.extract_facts`: классифицировать каждый факт по категории.
- Dream Cycle + Morning Briefing: секции «решения за неделю / победы / инциденты».
- *Лучше, чем у obsidian-mind*: L2 temporal (`valid_from`/`valid_until`) → у решений
  таймлайн и supersession (новое решение перекрывает старое); у плоских md-заметок этого нет.

**T2. Markdown-зеркало `~/caesar/mind/` (projection + curated overlay).** ✅ DONE (0.12.1)
- `auto/` — read-only проекция L2+KG: `entities/<name>.md` (факты + relations как
  wikilinks), `decisions/`, `wins/`, `incidents/`. Регенерируется фазой Dream Cycle
  (после entity dedup) + по требованию `caesar mind export`.
- `manual/` — user-curated; агент читает как авторитетные high-priority факты
  (аналог AGENTS.md). Правки юзера = прямой редактор «что агент должен всегда знать».
- НЕ two-way sync проекции (кошмар с temporal/vector структурой). Только curated overlay.
- Browsable в Obsidian, но Obsidian НЕ обязателен.

**T3. Context-manifest + meter на ход.**
- После сборки контекста хода (L1 recent + L2 + L3 + KG) и до LLM-вызова — логировать
  компактный manifest: что подтянуто + ~токенов. Footer в CLI/TG
  («context: 3 L2, 5 L3, 2 KG, ~1.8K tok»). Transparency как у obsidian-mind meter.

**T4. `caesar mind` + TG-команда «что ты знаешь про X».** ✅ DONE (0.16.0)
- MindMirror.query(entity) → L2 active facts + KG relations (filtered by user_id).
- CLI: `caesar mind query <entity>` / `caesar mind forget <entity[.attribute]>`.
- TG: `/mind X` — inspect (instant, 0 tokens, no LLM). `/forget X` — supersede L2 facts.
- `caesar mind export` — full Markdown projection (T2).

**T5. Focus / North Star.**
- `manual/focus.md` — текущая цель юзера («на этой неделе делаю X»), авто-инжектится
  как high-priority контекст на каждом ходу. Лёгкий аналог North Star.

### Открытое (не из obsidian-mind)
- 🔻 Knowledge Graph → semantic triples: извлечение с regex на LLM
  (subject→relation→object). Интеграция в L3-ranking УЖЕ есть (KG-boost +15%);
  открыто само LLM-извлечение (сейчас `knowledge_graph.py` — regex).
- 🔻 Web-панель + webhook — не начато.

### Не берём из obsidian-mind (обосновано)
- ❌ Markdown-vault как primary storage — дублировал бы L2/L3/KG, ломал temporal/vector.
- ❌ Perf-graph, 1:1, peer-scan, brag-doc — people-management, чужая предметка.
- ❌ 9 сабагентов — у caesar subagent-тул падает (`InternalError`); Dream Cycle уже
  делает консолидацию, сабагенты не нужны.

## Hermes-parity (что подтянуть от Hermes Agent — честный замер по коду 2026-07-24)
- ✅ Self-improving (скиллы из опыта + авто-улучшение) — ЕСТЬ: `_maybe_auto_save_skill`
  (orchestrator:1405), L4 record_success/failure, anti_patterns. Полировать, не строить.
- ✅ Learning loop — ЕСТЬ: L2/L3/L4 + dream-cycle + anti_patterns.
- ✅ Advanced memory (модель юзера между сессиями) — ЕСТЬ/строится: L2+KG+Mind Mirror
  (T1-T5). НЕ gap.
- ❌ **Browser automation — GAP.** web_fetch/http_request без JS/взаимодействия.
- ❌ **Voice conversations — пол-GAP.** STT (faster-whisper) входящий ГОТОВ
  (telegram_adapter:396-428); TTS (ответ голосом) — GAP (нет tts.py / send_voice).
- ❌ More tools — gaps = browser + TTS.

**H1. Browser automation** (Playwright, опц. extra `[browser]`). ✅ DONE (0.13.0)
- `browser_fetch(url)` — рендер JS, visible text + title (drop-in upgrade web_fetch для SPA).
- `browser_action(url, steps)` — многошаговое взаимодействие (navigate/click/fill/text/screenshot)
  в одной сессии с сохранением состояния (для логинов/форм).
- import-guard: playwright не установлен → понятная ошибка с инструкцией. Тяжёлый binary (~150MB) — опц.
**H2. TTS + voice replies** (edge-tts, опц. extra `[voice]`). ✅ DONE (0.14.0)
- `caesar/tools/tts.py` (edge-tts → mp3 → ffmpeg → ogg/Opus; fallback mp3/sendAudio).
- telegram_adapter: юзер написал голосом → `session.voice_reply=True` → ответ TTS → `send_voice`.
  one-shot (сбрасывается после ответа). Fallback на текст если TTS недоступен.
- завершает voice-loop (вход STT есть, выход TTS — добавляем).
**H3. (polish) Self-improving:** auto-merge похожих скиллов, auto-prune low-success.

### Token Economy (вдохновлено статьёй Writer «AI harness», 2026-07-26)
Writer: orchestration-слой → -38% токенов, -41% cost, -44% time, success не упал.
**E1. Cheap-LLM предобработка результатов поиска.** ✅ DONE (0.15.0)
SEARCH_FETCH_TOOLS (web_search/web_fetch/http_request/browser_fetch/browser_action/
github_releases/hn_search/reddit_search/wikipedia_read/rss_read/tg_read_channel):
если результат >2000 chars → cheap LLM экстрактит релевантное (concise summary)
вместо brute-truncation (первые 500 символов могут пропустить релевантное).
Gate: только большие результаты (>2000 chars) → +1 cheap-LLM вызов (~$0.001)
vs -3000+ smart-LLM токенов. Fallback на brute-truncation если cheap-LLM недоступен.
Статья Writer валидировала: -38% токенов через subagent → concise summary.
**E2. Prompt-prefix кэширование.** ✅ DONE (0.14.1)
Подтверждено: DashScope поддерживает prefix-caching (implicit auto, 20% hit cost) для
qwen3.7-max + glm-5.2. Fix: _build_system_prompt → tuple (stable, variable). Stable
(rules+tools, ~8000 tok, 100% static) — первый system message → cacheable prefix.
Variable (memory+L3+manual+task) — второй. Caller: два system messages вместо одного.
DashScope implicit-cache находит stable-prefix → 80% скидка. + ReAct history prefix
(шаги 1-N) — auto-cached. Оценка: -64% токенов на 10-шаговом ReAct.
Pre-condition: ✅ dashscope implicit caching подтверждён для qwen3.7-max + glm-5.2.
TODO: explicit cache_control markers (10% hit cost vs 20% implicit) — follow-up.

## Принципы развития
1. Сначала простая рабочая версия, потом усложняем.
2. Каждый модуль тестируется отдельно перед интеграцией.
3. Roadmap обновляется после каждой закрытой темы.
4. Никаких «хотим сделать X» без конкретного сценария использования.

---

## 🧠 Brainstorm / Future (vision, НЕ committed)

### Федеральный «общий мозг» агентов (идея юзера, 2026-07-24)
Когда много установок Caesar — у каждой свой опыт. Если агент научился чему-то
(рецепт, анти-паттерн) — передать в общую базу, которую другие читают.

**Insight:** делиться ПРОЦЕДУРНЫМ (L4-скиллы + anti_patterns), НЕ фактами.
Факты (L2/L3) — приватны/чужие. L4 — универсален, и Caesar его уже учится
накапливать (dream-cycle, success_count). Валюта = рецепты + выстраданные «не делай X».

**Дизайн (caesar-native, переиспользует существующее):**
- shared layer = L4 skills (YAML recipes + anti_patterns/pitfalls).
- publish: `caesar skill publish <name>` — санирует (user-пути/секреты), тегирует
  контекстом (ОС/distro/caesar-ver), заливает в community skill-repo (куратор+signing).
- subscribe: `caesar skill search/install` — тянет рецепт; **первый запуск с approval**
  (shared = advisory до одобрения).
- reputation: reuse L4-телеметрии (success_count/consecutive_failures) → анонимная
  success/fail-статистика по shared-скиллам → рейтинг.
- trust: shared-скиллы через sandboxed skill_executor (is_dangerous always-on с 0.11.1,
  anti_patterns enforced) + first-run gate → малварный скилл не `rm -rf /`.

**Трудное:** trust/малварь (signing+куратор+approval), relevance (контекст-теги+рейтинг),
privacy (санация перед publish), abuse (модерация репы).

**MVP-путь (низкий риск):** `caesar skill publish/install` в публичный repo — шериинг
YAML-рецептов БЕЗ telemetry/signing; репутацию/подписи прикрутить позже.
