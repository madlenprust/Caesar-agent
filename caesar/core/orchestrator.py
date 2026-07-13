"""Оркестратор — берёт задачи из очереди, выполняет.

См. roadmap раздел 10.

Базовый цикл: ReAct + Skill-First + Tool-First.
- Дешёвая LLM анализирует запрос
- Если скилл найден — код выполняет recipe (TODO V0.4)
- Иначе умная LLM в ReAct цикле с инструментами
- Tool-First Enforcement: агент не может ответить без tool или финала

Адаптивная рефлексия (раздел 10.4).
Жёсткие лимиты (раздел 10.3).
История диалога загружается из storage (conversation_messages).
"""

import asyncio
import json
from datetime import datetime
from caesar.config import Config
from caesar.core.events import (
    EventBus,
    progress_start,
    progress_update,
    answer_ready,
    error_occurred,
)
from caesar.core.llm import LLMRouter, LLMMessage
from caesar.core.queue import Task, TaskStatus, TaskComplexity
from caesar.logging_setup import get_logger
from caesar.memory.storage import Storage
from caesar.tools import ToolRegistry


# Минимальный интервал между вызовами LLM (секунды)
# 0 = без throttle (провайдеры обычно сами rate-limit)
LLM_THROTTLE_SECONDS = 0.0


# Иконки для каждого инструмента (раздел 4)
TOOL_ICONS = {
    "shell_exec": "💻",
    "read_file": "📄",
    "write_file": "✏️",
    "edit_file": "✏️",
    "find_files": "📁",
    "grep": "🔍",
    "web_search": "🔍",
    "web_fetch": "🌐",
    "http_request": "🌐",
    "github_releases": "🚀",  # релизы проекта — свежие новости
    "github_search": "🐙",    # GitHub repo search
    "rss_read": "📡",
    "tg_read_channel": "📡",
    "hn_search": "📡",
    "reddit_search": "📡",
    "wikipedia_read": "📡",
    "parse_pdf": "📄",
    "parse_docx": "📄",
    "parse_xlsx": "📄",
    "parse_csv": "📄",
    "memory_search": "🧶",  # L3 vector search — клубок памяти, семантический поиск по прошлому
    "memory_add_fact": "💾",
    "memory_delete": "🗑️",  # L3 delete — удаление из долгой памяти
    "skill_find": "📚",
    "skill_save": "💾",
    "self_read": "📖",
    "self_edit": "🔧",
    "self_install_package": "📦",
    "self_scan": "🔧",
    "self_test": "🧪",
    "transcribe_audio": "🎤",  # STT — распознавание голоса
    "cron_add": "⏰",          # постановка cron задачи
    "cron_list": "📋",         # список cron задач
    "cron_remove": "🗑️",      # удаление cron задачи
}

# Сколько последних сообщений загружать в контекст
CONVERSATION_HISTORY_LIMIT = 20


class Orchestrator:
    """Оркестратор задач.
    
    Полный flow:
    1. Загружаем историю диалога из storage
    2. Дешёвая LLM анализирует запрос (если есть ключ)
    3. Если тривиальный — отвечаем сразу
    4. Иначе умная LLM в ReAct цикле с инструментами
    5. После ответа — сохраняем в историю
    """
    
    def __init__(
        self,
        config: Config,
        event_bus: EventBus,
        storage: Storage | None = None,
        llm_router: LLMRouter | None = None,
        tool_registry: ToolRegistry | None = None,
    ):
        self.config = config
        self.event_bus = event_bus
        self.storage = storage
        self.llm = llm_router
        self.tools = tool_registry
        self.log = get_logger("orchestrator")
        self._running = False
        self._l3 = None  # L3 memory, устанавливается извне
        self._l4 = None  # L4 skills, устанавливается извне
        self._kg = None  # Knowledge Graph, устанавливается извне
        self._skill_executor = None  # SkillExecutor, устанавливается извне
    
    def set_storage(self, storage: Storage) -> None:
        self.storage = storage
    
    def set_llm(self, llm: LLMRouter) -> None:
        self.llm = llm
    
    def set_tools(self, tools: ToolRegistry) -> None:
        self.tools = tools
    
    async def start(self) -> None:
        self._running = True
        has_llm = self.llm is not None and (
            self.llm.smart.api_key or self.llm.cheap.api_key
        )
        if has_llm:
            self.log.info("Orchestrator started (LLM mode)")
        else:
            self.log.info("Orchestrator started (echo mode — no LLM keys)")
    
    async def stop(self) -> None:
        self._running = False
        self.log.info("Orchestrator stopped")
    
    async def handle_task(self, task: Task) -> None:
        """Обработать задачу. Вызывается queue worker-ом."""
        self.log.info(f"Processing task {task.id}: '{task.user_message[:50]}...'")
        
        channel_id = task.channel_id
        
        try:
            # Сохраняем сообщение пользователя в историю ДО обработки
            if self.storage:
                self.storage.save_message(
                    channel_id=channel_id,
                    role="user",
                    content=task.user_message,
                    task_id=task.id,
                )
            
            # Эмитим прогресс — ТОЛЬКО на source_chat_id (откуда спросили)
            emit_key = task.source_chat_id or ""
            
            # Для фоновых (complex) задач — НЕ показываем прогресс-иконки.
            # Пользователь уже получил сообщение '🔬 уводу в фон'.
            # Прогресс будет только когда ответ готов (answer_ready).
            is_background = task.complexity == TaskComplexity.COMPLEX
            
            if not is_background:
                await self.event_bus.emit(emit_key, progress_start(task.id))
            
            # Проверяем есть ли LLM
            has_llm = (
                self.llm is not None
                and self.tools is not None
                and (self.llm.smart.api_key or self.llm.cheap.api_key)
            )
            
            if not has_llm:
                # Эхо-режим — нет LLM ключа
                await self.event_bus.emit(emit_key, progress_update(task.id, "🔍"))
                await asyncio.sleep(0.05)
                response = self._echo(task.user_message)
                await self.event_bus.emit(emit_key, answer_ready(task.id, response))
                
                # Сохраняем ответ в историю
                if self.storage:
                    self.storage.save_message(
                        channel_id=channel_id,
                        role="assistant",
                        content=response,
                        task_id=task.id,
                    )
                
                task.status = TaskStatus.COMPLETED
                task.result = response
                return
            
            # Полный flow с LLM
            response = await self._run_with_llm(task)
            
            # Сохраняем ответ в историю
            if self.storage:
                self.storage.save_message(
                    channel_id=channel_id,
                    role="assistant",
                    content=response,
                    task_id=task.id,
                )
            
            # Эмитим ответ. Для cron задач source_chat_id может быть
            # пустым или содержать внутренний channel_id вместо Telegram chat_id.
            # Эмитим на ВСЕ возможные ключи чтобы точно дошло.
            await self.event_bus.emit(emit_key, answer_ready(task.id, response))
            
            # ВСЕГДА делаем TG fallback для cron задач.
            # emit_key может быть чем угодно: CLI session, channel_id, пустая строка.
            # TG adapter подписан на str(chat_id) (число) — только это работает.
            if task.source == "cron":
                tg_chat_id = ""
                try:
                    with self.storage._conn() as conn:
                        # Ищем ЛЮБОЙ telegram channel (не фильтруем по user_id —
                        # cron может быть создан cli-1000, а TG принадлежит user-tg-XXX)
                        row = conn.execute(
                            """SELECT source_chat_id FROM channels 
                               WHERE source = 'telegram' 
                               AND source_chat_id IS NOT NULL LIMIT 1""",
                        ).fetchone()
                        if row and row["source_chat_id"]:
                            tg_chat_id = str(row["source_chat_id"])
                except Exception:
                    pass
                
                if tg_chat_id and tg_chat_id != emit_key:
                    self.log.info(
                        f"Cron fallback: emitting answer to TG chat_id={tg_chat_id} "
                        f"(emit_key was '{emit_key}')"
                    )
                    await self.event_bus.emit(tg_chat_id, answer_ready(task.id, response))
            
            task.status = TaskStatus.COMPLETED
            task.result = response
        
        except Exception as e:
            self.log.exception(f"Task {task.id} failed: {e}")
            task.status = TaskStatus.FAILED
            task.error = str(e)
            await self.event_bus.emit(
                emit_key,
                error_occurred(task.id, str(e)),
            )
    
    async def _run_with_llm(self, task: Task) -> str:
        """Полный flow с LLM и инструментами."""
        assert self.llm is not None
        assert self.tools is not None
        
        channel_id = task.channel_id
        user_id = task.user_id
        
        # Проверяем god_mode для этого пользователя
        god_mode = False
        if self.storage and user_id:
            try:
                god_mode = self.storage.get_user_god_mode(user_id)
            except Exception:
                pass
        emit_key = task.source_chat_id or ""
        
        # Для фоновых задач — suppress progress icons.
        # Пользователь видит только финальный ответ.
        is_background = task.complexity == TaskComplexity.COMPLEX
        
        async def emit_progress(icon: str):
            """Эмитить прогресс только для интерактивных задач."""
            if not is_background:
                await self.event_bus.emit(emit_key, progress_update(task.id, icon))
        
        # Устанавливаем контекст для memory-инструментов
        channel_name = channel_id.rsplit(":", 1)[-1] if ":" in channel_id else channel_id
        self.tools.set_context(
            channel_id=channel_id,
            user_id=user_id,
            author_id=task.author_id or user_id,
            stt_model=getattr(self.config.stt, "model", "base") if hasattr(self.config, "stt") else "base",
            stt_language=getattr(self.config.stt, "language", None) if hasattr(self.config, "stt") else None,
        )
        
        # === ШАГ -1: Детектор прямых команд ===
        # Если пользователь явно просит выполнить shell команду — ВЫПОЛНЯЕМ
        # её напрямую через shell_exec, минуя LLM. Это предотвращает
        # галлюцинации когда LLM "рассуждает" о логах вместо того чтобы
        # их реально прочитать.
        #
        # ВАЖНО: здесь мы вызываем ShellExecTool.execute() НАПРЯМУЮ,
        # а не через ToolRegistry.execute(). Это обходит проверку
        # requires_permission. Это БЕЗОПАСНО потому что:
        # 1. Команда пришла от пользователя напрямую (не от LLM)
        # 2. Пользователь явно попросил "выполни команду X"
        # 3. LLM не может сюда попасть — это отдельный путь до LLM
        # 4. is_dangerous_command (rm -rf /, mkfs, dd) ВСЁ ЕЩЁ блокируется
        #    внутри ShellExecTool.execute()
        direct_cmd = self._detect_direct_command(task.user_message, task.user_id)
        if direct_cmd:
            # ОСОБЫЙ СЛУЧАЙ: рестарт caesar-daemon.
            # Если выполнить 'systemctl restart caesar-daemon' через shell_exec
            # напрямую — daemon убьёт сам себя. systemd увидит быстрый restart-loop
            # и поставит start-limit-hit → daemon больше не поднимется.
            # Поэтому используем ОТВЯЗАННЫЙ subprocess через setsid+bash:
            # - setsid запускает bash в новой session (переживёт смерть daemon)
            # - sleep 2 даёт время вернуть ответ пользователю
            # - systemctl restart убивает старый daemon
            # - systemd поднимает новый
            cmd_lower = direct_cmd.lower()
            is_daemon_restart = (
                "systemctl" in cmd_lower
                and ("restart" in cmd_lower or "stop" in cmd_lower)
                and "caesar-daemon" in cmd_lower
            )
            
            if is_daemon_restart:
                self.log.info(f"Daemon restart detected, using detached subprocess: {direct_cmd[:80]}")
                try:
                    import subprocess as _sp
                    import os as _os
                    import json as _json
                    
                    daemon_uid = _os.getuid()
                    xdg_runtime_dir = f"/run/user/{daemon_uid}"
                    
                    # Сохраняем chat_id в файл чтобы новый daemon при старте
                    # знал кому отправить 'готово'.
                    # Файл: /tmp/caesar-restart-chat-id
                    tg_chat_id = task.source_chat_id or ""
                    if tg_chat_id:
                        try:
                            with open("/tmp/caesar-restart-chat-id", "w") as f:
                                f.write(tg_chat_id)
                        except Exception:
                            pass
                    
                    # Bash скрипт: sleep 2 → restart
                    # Новый daemon при старте прочитает /tmp/caesar-restart-chat-id
                    # и отправит 'готово' сам — надёжнее, чем subprocess,
                    # который может не найти config.yaml.
                    # Whitelist: только systemctl [--user] (restart|stop) caesar-daemon
                    # Без shlex.quote — он ломает команду (превращает в один токен с пробелами)
                    import re as _re
                    if not _re.match(r"^systemctl(\s+--user)?\s+(restart|stop)\s+caesar-daemon\s*$", direct_cmd):
                        return f"⚠️ Команда не разрешена для restart-flow: {direct_cmd}"
                    
                    import shlex as _shlex
                    restart_script = (
                        f"sleep 2 && "
                        f"XDG_RUNTIME_DIR={_shlex.quote(xdg_runtime_dir)} "
                        f"DBUS_SESSION_BUS_ADDRESS=unix:path={_shlex.quote(xdg_runtime_dir)}/bus "
                        f"{direct_cmd}"
                    )
                    
                    _sp.Popen(
                        ["setsid", "bash", "-c", restart_script],
                        stdin=_sp.DEVNULL,
                        stdout=_sp.DEVNULL,
                        stderr=_sp.DEVNULL,
                        start_new_session=True,
                    )
                    
                    return (
                        "♻️ Перезапускаю daemon...\n"
                        "⏱️ ~10 сек. Когда закончу — напишу «готово»."
                    )
                except Exception as e:
                    self.log.exception(f"Daemon restart scheduling failed: {e}")
                    return f"⚠️ Не удалось запланировать рестарт: {e}\nВыполни вручную: {direct_cmd}"
            
            # Обычная команда — выполняем через shell_exec
            self.log.info(f"Direct command detected, executing: {direct_cmd[:80]}")
            try:
                # Вызываем shell_exec напрямую, минуя ToolRegistry (и requires_permission)
                shell_tool = self.tools.get("shell_exec")
                if shell_tool:
                    # Проверяем god_mode повторно (не полагаемся на локальную переменную)
                    is_god = False
                    if self.storage and task.user_id:
                        try:
                            is_god = self.storage.get_user_god_mode(task.user_id)
                        except Exception:
                            pass
                    shell_tool.god_mode = is_god
                    # requires_permission (мягкий gate: sudo/chmod-R/systemctl/...)
                    # — в sandboxed проверяем и для прямых команд (god/full обходит).
                    needs_perm = (
                        shell_tool.access_mode != "full"
                        and not is_god
                        and hasattr(shell_tool, "requires_permission")
                        and shell_tool.requires_permission(command=direct_cmd)
                    )
                    if needs_perm:
                        from caesar.tools.base import ToolResult
                        tool_result = ToolResult(
                            success=False,
                            error=f"BLOCKED (sandboxed): прямая команда требует "
                                  f"god/full. Команда: {direct_cmd[:80]}",
                        )
                    else:
                        tool_result = await shell_tool.execute(command=direct_cmd, timeout=30)
                else:
                    # Fallback на ToolRegistry (с проверкой permission)
                    tool_result = await self.tools.execute("shell_exec", command=direct_cmd, timeout=30)
                
                # Форматируем результат для пользователя
                if hasattr(tool_result, 'data') and tool_result.data:
                    stdout = tool_result.data.get("stdout", "")
                    stderr = tool_result.data.get("stderr", "")
                    exit_code = tool_result.data.get("exit_code", 0)
                    
                    if not stdout.strip() and not stderr.strip():
                        return f"Команда выполнена (exit code {exit_code}), но вывод пустой."
                    
                    parts = []
                    if stdout.strip():
                        # Обрезаем длинный вывод
                        stdout_display = stdout if len(stdout) <= 3000 else stdout[:3000] + f"\n\n... (обрезано, всего {len(stdout)} символов)"
                        parts.append(f"```\n{stdout_display}\n```")
                    if stderr.strip():
                        stderr_display = stderr if len(stderr) <= 1000 else stderr[:1000] + "\n... (обрезано)"
                        parts.append(f"stderr:\n```\n{stderr_display}\n```")
                    if exit_code != 0:
                        parts.append(f"Exit code: {exit_code}")
                    
                    return "\n\n".join(parts) if parts else f"Команда выполнена (exit {exit_code})."
                elif hasattr(tool_result, 'error') and tool_result.error:
                    return f"⚠️ Ошибка выполнения команды:\n{tool_result.error}"
                else:
                    return "Команда выполнена, но результат пустой."
            except Exception as e:
                self.log.exception(f"Direct command execution failed: {e}")
                return f"⚠️ Не удалось выполнить команду: {e}"
        
        # === ШАГ 0: Проверяем есть ли подходящий скилл (L4) ===
        # Если скилл найден с high confidence — выполняем recipe БЕЗ LLM.
        # Это экономит токены: 0 вызовов smart LLM, только script шаги.
        if self._skill_executor:
            try:
                skill_result = await self._skill_executor.try_apply_skill(
                    user_message=task.user_message,
                    user_id=user_id,
                    channel_id=channel_id,
                )
                if skill_result and skill_result.success:
                    self.log.info(
                        f"Skill '{skill_result.skill_name}' v{skill_result.skill_version} "
                        f"applied: {skill_result.steps_executed}/{skill_result.steps_total} steps, "
                        f"{skill_result.tokens_used} tokens (vs ~5000 for LLM)"
                    )
                    return skill_result.final_output
                elif skill_result and not skill_result.success:
                    self.log.info(
                        f"Skill '{skill_result.skill_name}' failed, falling back to LLM: "
                        f"{skill_result.error}"
                    )
                    # Падаем в обычный LLM flow ниже
            except Exception as e:
                self.log.warning(f"Skill execution error: {e}, falling back to LLM")
        
        # === ШАГ 1: Загружаем историю диалога (L1 — short-term context) ===
        # 💬 — recall из истории диалога
        history_messages: list[LLMMessage] = []
        has_history = False
        if self.storage:
            recent = self.storage.get_messages(channel_id, limit=CONVERSATION_HISTORY_LIMIT)
            # Исключаем последнее сообщение — это текущий запрос пользователя
            # (мы его только что сохранили)
            for msg in recent[:-1]:  # все кроме последнего
                role = msg["role"]
                content = msg["content"]
                if role in ("user", "assistant") and content.strip():
                    # Обрезаем каждое сообщение до 1000 символов — экономия на длинных ответах
                    truncated = content[:1000] + ("..." if len(content) > 1000 else "")
                    history_messages.append(LLMMessage(role=role, content=truncated))
            
            if history_messages:
                has_history = True
                self.log.info(f"Loaded {len(history_messages)} history messages for channel {channel_name}")
        
        # === ШАГ 2: Cheap LLM analyzer — анализируем запрос ДО умной LLM ===
        # Дешёвая модель решает:
        # - Тривиальный ли запрос? (привет, спасибо) → отвечает сама, без умной
        # - Нужно ли искать в памяти/инструментах? → умная LLM
        # - Какая сложность? (simple/medium/complex)
        # Это экономит токены: 70% запросов идут через дешёвую модель.
        analysis = await self._analyze_request(task, has_history)
        
        # === ШАГ 3: L2 факты — ТОЛЬКО как fallback к истории диалога ===
        # 📌 — long-term facts из L2 (SQLite)
        # Логика (как у человека):
        #   1. Сначала вспоминаем разговор (history_messages выше)
        #   2. Если факта НЕТ в истории — тащим из L2
        #   3. L2 — страховка для long-term, не параллельный источник
        # Это убирает дублирование и путаницу "шашлык виден и там и там".
        memory_context = ""
        has_l2_facts = False
        if self.storage:
            facts = self.storage.get_facts(user_id, channel_name, limit=20)
            if facts:
                # Дедупликация: выкидываем факты, value которых уже есть в истории.
                # Entity ("user") не проверяем — в истории будет "я/ты", не "user".
                history_text = " ".join(m.content.lower() for m in history_messages if m.content)
                fresh_facts = []
                for f in facts:
                    value_lower = str(f.get("value", "")).lower().strip()
                    # Только value — если значение уже упоминалось, факт дублирует контекст
                    if value_lower and value_lower in history_text:
                        continue
                    fresh_facts.append(f)
                
                if fresh_facts:
                    has_l2_facts = True
                    memory_context += "Долгосрочные факты (которых нет в недавней истории):\n"
                    for f in fresh_facts[:10]:  # не больше 10
                        memory_context += f"- {f['entity']}.{f['attribute']} = {f['value']}\n"
                    memory_context += "\n"
                    self.log.info(
                        f"L2: {len(facts)} total → {len(fresh_facts)} fresh "
                        f"(deduped {len(facts) - len(fresh_facts)} already in history) "
                        f"for channel {channel_name}"
                    )
                else:
                    self.log.info(
                        f"L2: {len(facts)} facts but all already in history "
                        f"for channel {channel_name}"
                    )
            else:
                self.log.info(f"No facts in L2 for channel {channel_name}")
        
        # Показываем индикаторы источников памяти (до начала LLM работы).
        # 💬 = есть что вспомнить из истории (L1)
        # 📌 = есть долгосрочные факты которых нет в истории (L2)
        # 🔎 = авто-поиск в L3 (векторная память) когда L1/L2 не дали результата
        # Пользователь сразу видит, ОТКУДА агент берёт данные.
        if has_history:
            await emit_progress("💬")
        if has_l2_facts:
            await emit_progress("📌")
        
        # === АВТО-ПОИСК В L3 ===
        # L3 — векторная память (загруженные документы + важные прошлые диалоги).
        # Если в L3 есть ЧТО-ТО для этого пользователя — ВСЕГДА ищем.
        # Это позволяет агенту отвечать на вопросы по загруженным документам
        # без явного "вспомни" — пользователь загрузил документ, ожидает
        # что агент его знает.
        l3_context = ""
        l3_has_chunks = self._l3 and len(self._l3._vectors_cache) > 0
        # Дополнительно проверяем что есть чанки именно этого пользователя
        l3_has_user_chunks = False
        if l3_has_chunks and self.storage:
            try:
                with self.storage._conn() as conn:
                    row = conn.execute(
                        "SELECT COUNT(*) as cnt FROM l3_chunks WHERE user_id = ?",
                        (user_id,),
                    ).fetchone()
                    l3_has_user_chunks = row["cnt"] > 0 if row else False
            except Exception:
                pass
        
        if l3_has_user_chunks:
            await emit_progress("🧶")
            try:
                # Адаптивный L3: simple=3 чанка, medium/complex=5
                l3_final_k = 3 if (analysis.get("complexity") or task.complexity) == "simple" else 5
                l3_results = await self._l3.search(
                    query=task.user_message[:1000],
                    user_id=user_id,
                    channel=channel_name,
                    final_k=l3_final_k,
                    boost_same_channel=1.0,
                    min_similarity=0.15,
                )
                if l3_results:
                    l3_context = "Тебе доступны следующие материалы (используй их как свои знания):\n"
                    for r in l3_results:
                        # Обрезаем чанки до 300 символов — экономия
                        l3_context += f"{r.content[:300]}\n\n"
                    l3_context += "\n"
                    
                    # Gap Analysis: проверяем что мозг НЕ знает
                    if self._kg:
                        try:
                            gap = self._gap_analysis(task.user_message, user_id, l3_results)
                            if gap:
                                l3_context += f"\n⚠️ Чего мозг НЕ знает:\n{gap}\n"
                        except Exception:
                            pass
                    
                    self.log.info(
                        f"L3 auto-search: found {len(l3_results)} chunks "
                        f"(top score: {l3_results[0].score:.3f})"
                    )
                else:
                    self.log.info("L3 auto-search: no results")
                    # Gap Analysis для пустого L3: сообщаем что в памяти
                    # есть данные, но по этому запросу ничего не нашлось.
                    if self._kg and self.storage:
                        try:
                            gap = self._gap_analysis_empty_l3(task.user_message, user_id)
                            if gap:
                                l3_context = f"\n⚠️ Чего мозг НЕ знает:\n{gap}\n"
                        except Exception:
                            pass
            except Exception as e:
                self.log.warning(f"L3 auto-search failed: {e}")
        
        # Тривиальный запрос — отвечаем сразу через cheap analyzer.
        # Не зовём умную LLM для "привет", "спасибо" — экономим токены.
        if analysis.get("is_trivial") and analysis.get("trivial_response"):
            response = analysis["trivial_response"]
            await emit_progress("🤔")
            self.log.info(f"Trivial response (cheap analyzer): {response[:50]}")
            return response
        
        # === ШАГ 4: Собираем сообщения для LLM ===
        # Ограничиваем длину сообщения (защита от огромных вводов)
        user_msg = task.user_message[:50000] if len(task.user_message) > 50000 else task.user_message
        
        system_prompt = self._build_system_prompt(task, memory_context, len(history_messages), l3_context)
        
        # Лимиты — используем complexity из cheap analyzer если есть
        effective_complexity = analysis.get("complexity") or task.complexity
        
        messages: list[LLMMessage] = [
            LLMMessage(role="system", content=system_prompt),
        ]
        
        # Добавляем историю диалога — адаптивный лимит
        # Simple: 10 сообщений (экономия ~2500 токенов)
        # Medium/Complex: 20 сообщений (нужен контекст)
        history_limit = 10 if effective_complexity == "simple" else CONVERSATION_HISTORY_LIMIT
        messages.extend(history_messages[-history_limit:])
        
        # Добавляем текущий запрос пользователя (с ограничением длины)
        messages.append(LLMMessage(role="user", content=user_msg))
        
        # Схемы инструментов — adaptive: только релевантные
        # Экономия: ~4000 → ~1200 токенов (34 → 8-12 инструментов)
        needs_tools = analysis.get("needs_tools", True)
        if needs_tools:
            tool_schemas = self.tools.get_schemas_smart(task.user_message)
            self.log.info(f"Tool schemas: {len(tool_schemas)} tools selected (adaptive)")
        else:
            tool_schemas = None
            self.log.info(f"Skipping tool schemas — analyzer says no tools needed (-4000 tokens)")
        
        max_steps = self._get_max_steps(effective_complexity)
        max_tokens = self._get_max_tokens(effective_complexity)
        
        total_tokens = 0
        last_tool_calls: list[dict] = []
        dedup_count = 0
        
        # Полная последовательность tool calls для auto-save skill extraction.
        # last_tool_calls capped at 5 — недостаточно для recipe extraction.
        # Здесь храним все (с аргументами и статусом success/error).
        full_tool_history: list[dict] = []
        
        # === ШАГ 5: ReAct цикл ===
        # Иконки:
        #   🤔 — первый шаг LLM (возможен recall без tool calls)
        #   🧠 — после первого tool call (реальная многошаговая работа)
        # Источники памяти (💬 L1, 📌 L2) уже показаны выше ДО цикла.
        has_used_tools = False
        # Tracker использованных инструментов — нужно для решения
        # сохранять ли ответ в L3 (ответ из интернета НЕ сохраняем)
        used_tool_names: set[str] = set()
        # Инструменты которые тянут данные из интернета — если они использовались,
        # ответ пришёл ИЗНЕ, не от пользователя. Не сохраняем в L3.
        WEB_SOURCE_TOOLS = {
            "web_search", "web_fetch", "http_request",
            "hn_search", "reddit_search", "rss_read", "tg_read_channel",
            "github_releases", "github_search", "wikipedia_read",
        }
        for step in range(1, max_steps + 1):
            task.current_step = step

            # /stop ставит task.cancelled — выходим сразу, не дожигая токены.
            if getattr(task, "cancelled", False):
                await emit_progress("⏹️")
                return "⏹️ Остановлено пользователем."

            # Лимит токенов — мягкое предупреждение, НЕ прерываем сразу.
            # Если LLM в середине работы (были tool calls), даём ей закончить
            # текущую цепочку и ответить. Жёстко прерываем только если токенов
            # реально ОЧЕНЬ много (2x от лимита) — защита от зацикливания.
            if total_tokens >= max_tokens * 2:
                return await self._force_finish(task, messages, "Достигнут лимит токенов (2x)")
            if total_tokens >= max_tokens:
                self.log.warning(
                    f"Task {task.id}: token usage {total_tokens} >= {max_tokens} "
                    f"(soft limit, letting LLM finish)"
                )
                # НЕ прерываем — даём LLM шанс ответить на текущем шаге
            
            # 🤔 на первом шаге (возможен recall из контекста), 🧠 после инструментов
            icon = "🧠" if has_used_tools else "🤔"
            await emit_progress(icon)
            
            # Throttle: пауза между последовательными вызовами LLM
            if step > 1:
                await asyncio.sleep(LLM_THROTTLE_SECONDS)
            
            try:
                resp = await self.llm.smart_chat(
                    messages,
                    tools=tool_schemas,
                    temperature=0.7,
                    max_tokens=4096,
                )
                
                # Если analyzer сказал "no tools" но LLM всё равно попыталась вызвать tool —
                # такого не должно быть (tools=None), но если API требует tools для tool_choice,
                # и LLM вернула content вместо tool_calls — это нормально.
                # Если LLM вернула ответ "мне нужен инструмент" — пересылаем со schemas на шаге 2.
                if not tool_schemas and step == 1 and not (resp.content or "").strip():
                    # LLM не смогла ответить без инструментов — включаем schemas
                    tool_schemas = self.tools.get_schemas_smart(task.user_message)
                    self.log.info("Re-enabling tool schemas — LLM needs them")
                    # Повторяем шаг с schemas
                    resp = await self.llm.smart_chat(
                        messages,
                        tools=tool_schemas,
                        temperature=0.7,
                        max_tokens=4096,
                    )
            except Exception as e:
                # Логируем с типом исключения — пустые сообщения не помогают
                err_msg = f"{type(e).__name__}: {e}" if e else f"{type(e).__name__} (no message)"
                self.log.error(f"LLM call failed: {err_msg}")
                self.log.exception(f"LLM call traceback:")
                return f"Ошибка LLM: {err_msg}"
            
            total_tokens += resp.total_tokens
            task.tokens_used = total_tokens
            
            # Логируем вызов
            if self.storage:
                self.storage.log_action(
                    task_id=task.id,
                    step_number=step,
                    action_type="llm_response",
                    llm_thinking=resp.content[:500] if resp.content else "",
                    tokens_used=resp.total_tokens,
                )
                self.storage.log_token_usage(
                    task_id=task.id,
                    step=step,
                    llm_role="smart",
                    llm_model=resp.model,
                    prompt_tokens=resp.prompt_tokens,
                    completion_tokens=resp.completion_tokens,
                    total_tokens=resp.total_tokens,
                    cost_rub=0,
                    reason="main_answer",
                )
            
            # Tool-First Enforcement: если нет tool_calls и нет контента
            if not resp.tool_calls and not (resp.content or "").strip():
                messages.append(LLMMessage(role="assistant", content=""))
                messages.append(LLMMessage(
                    role="user",
                    content="(продолжай — вызови инструмент или дай финальный ответ)",
                ))
                continue
            
            # Если есть tool_calls — выполняем
            if resp.tool_calls:
                has_used_tools = True  # переключаем индикатор: реальная работа, не recall
                messages.append(LLMMessage(role="assistant", content=resp.content))
                
                for tc in resp.tool_calls:
                    # Иконка прогресса
                    icon = TOOL_ICONS.get(tc.name, "🔧")
                    await emit_progress(icon)
                    
                    # Проверка на дубли
                    call_sig = {"name": tc.name, "args": tc.arguments}
                    if self._is_duplicate_call(call_sig, last_tool_calls):
                        dedup_count += 1
                        args_str = json.dumps(tc.arguments, ensure_ascii=False)[:300]
                        
                        # На ПЕРВОМ duplicate — внедряем предупреждение в tool result,
                        # чтобы LLM переключилась на другой инструмент/запрос
                        if dedup_count == 1:
                            self.log.warning(
                                f"Task {task.id} step {step}: duplicate {tc.name}({args_str}) "
                                f"— injecting warning, skipping execution"
                            )
                            warning_payload = {
                                "skipped": True,
                                "reason": (
                                    "duplicate call — уже вызывал с этими аргументами, "
                                    "результат был пустой или не помог. Не повторяй. "
                                    "Дай финальный ответ на основе того что уже знаешь."
                                ),
                            }
                            messages.append(LLMMessage(
                                role="tool",
                                content=json.dumps(warning_payload, ensure_ascii=False),
                                tool_call_id=tc.id,
                                name=tc.name,
                            ))
                            continue
                        
                        # На 2+ duplicate — СРАЗУ force-finish, не ждём threshold
                        # Пользователь не должен ждать 3 цикла — 2 уже достаточно
                        return await self._force_finish(
                            task, messages,
                            f"Обнаружен цикл: {dedup_count} одинаковых вызовов {tc.name} подряд"
                        )
                    else:
                        dedup_count = 0
                    last_tool_calls.append(call_sig)
                    if len(last_tool_calls) > 5:
                        last_tool_calls.pop(0)
                    
                    # Выполняем инструмент
                    self.log.info(f"Task {task.id} step {step}: {tc.name}({tc.arguments})")
                    # Устанавливаем god_mode на shell_exec если активен
                    # god_mode propagates на любой tool с этим атрибутом
                    # (shell_exec, remote_exec, ...) — чтобы god/full обходил
                    # их проверки (exact_deny / requires_permission).
                    cur_tool = self.tools.get(tc.name)
                    if cur_tool and hasattr(cur_tool, "god_mode"):
                        cur_tool.god_mode = god_mode
                    tool_result = await self.tools.execute(tc.name, **tc.arguments)
                    # Запоминаем какой инструмент использовали (для L3 save filter)
                    used_tool_names.add(tc.name)
                    
                    # Нормализуем результат — может быть ToolResult или dict
                    if hasattr(tool_result, 'data'):
                        result_data = tool_result.data
                        result_error = tool_result.error
                        result_success = tool_result.success
                    elif isinstance(tool_result, dict):
                        result_data = tool_result
                        result_error = tool_result.get('error')
                        result_success = tool_result.get('success', True)
                    else:
                        result_data = {"result": str(tool_result)}
                        result_error = None
                        result_success = True
                    
                    # Сохраняем в full_tool_history для auto-save skill extraction.
                    # Храним только recipe-worthy поля (без полного result — он
                    # может быть огромным). Для ошибок — сохраняем message.
                    history_entry = {
                        "step": step,
                        "tool": tc.name,
                        "args": tc.arguments,
                        "success": result_success,
                    }
                    if not result_success and result_error:
                        # Текст ошибки — для anti_patterns
                        history_entry["error"] = str(result_error)[:300]
                    full_tool_history.append(history_entry)
                    
                    # Логируем
                    if self.storage:
                        self.storage.log_action(
                            task_id=task.id,
                            step_number=step,
                            action_type="tool_call",
                            tool_name=tc.name,
                            tool_args=tc.arguments,
                            tool_result=result_data,
                            success=result_success,
                        )
                    
                    # Добавляем результат в сообщения
                    # Обрезаем значения ВНУТРИ result_data до 500 символов ДО json.dumps
                    # чтобы JSON оставался валидным
                    truncated_data = {}
                    for k, v in (result_data or {}).items():
                        if isinstance(v, str) and len(v) > 500:
                            truncated_data[k] = v[:500] + "... (truncated)"
                        elif isinstance(v, list) and len(str(v)) > 1000:
                            truncated_data[k] = str(v)[:1000] + "... (truncated)"
                        else:
                            truncated_data[k] = v
                    result_str = json.dumps(
                        truncated_data if truncated_data else {"error": result_error or "unknown error"},
                        ensure_ascii=False,
                        default=str,
                    )[:3000]
                    
                    messages.append(LLMMessage(
                        role="tool",
                        content=result_str,
                        tool_call_id=tc.id,
                        name=tc.name,
                    ))
                
                continue  # следующий шаг ReAct
            
            # Если нет tool_calls и есть контент — это финальный ответ
            if (resp.content or "").strip():
                # Сохраняем в L3 ТОЛЬКО если ответ важный (smart filter).
                # L3 — это записная книжка, не лог всего.
                # "шашлык вкусный" → нет, "как настроить nginx" + код → да.
                # НО: если ответ пришёл из интернета (web_search и т.д.) — НЕ сохраняем,
                # интернет уже это знает, не дублируем.
                if self.storage and self._l3:
                    used_web_tools = used_tool_names & WEB_SOURCE_TOOLS
                    triggers = self._l3_save_triggers(
                        task.user_message, resp.content, step,
                        used_tool_names=used_tool_names,
                        used_web_tools=used_web_tools,
                    )
                    if triggers:  # есть триггеры И не было web tools (или web tools не блокируют)
                        try:
                            await self._l3.add(
                                user_id=user_id,
                                channel=channel_name,
                                content=f"User: {task.user_message}\n\nAssistant: {resp.content}",
                                task_id=task.id,
                                author_id=task.author_id or user_id,
                                metadata={
                                    "auto_indexed": True,
                                    "tool_calls_count": step,
                                    "triggers": triggers,
                                    "used_tools": list(used_tool_names),
                                },
                            )
                            self.log.info(f"L3: saved (triggers: {triggers}, tools: {list(used_tool_names)})")
                            
                            # Knowledge Graph: извлекаем entities из сохранённого чанка
                            if self._kg:
                                try:
                                    kg_result = self._kg.process_text(
                                        text=f"User: {task.user_message}\n\nAssistant: {resp.content}",
                                        user_id=user_id,
                                        source_chunk_id=None,  # TODO: передать chunk_id
                                    )
                                    if kg_result["entities_new"] > 0 or kg_result["relations_new"] > 0:
                                        self.log.info(
                                            f"KG: +{kg_result['entities_new']} entities, "
                                            f"+{kg_result['relations_new']} relations"
                                        )
                                except Exception as e:
                                    self.log.debug(f"KG extraction failed: {e}")
                        except Exception as e:
                            self.log.warning(f"L3 save failed: {e}")
                
                # Post-process: вырезаем "поисковый" стиль из ответа.
                cleaned = self._clean_response_style(resp.content)
                
                # === ЯВНОЕ СОХРАНЕНИЕ В ПАМЯТЬ ===
                # "запомни это" → L3 (весь контекст диалога)
                # "запиши это" → L2 (конкретный факт) — через memory_add_fact tool
                msg_lower = task.user_message.lower().strip()
                
                # «запомни это» → сохраняем в L3
                if any(msg_lower.startswith(t) or msg_lower == t for t in [
                    "запомни это", "запомнить это", "сохрани это", "запомни вот это",
                    "хорошая идея", "отличная мысль", "хорошая мысль",
                ]):
                    if self.storage and self._l3:
                        try:
                            # Сохраняем ВСЁ: вопрос + ответ + контекст
                            save_content = (
                                f"Контекст диалога (сохранён пользователем как 'запомни это'):\n\n"
                                f"Вопрос: {task.user_message}\n\n"
                                f"Ответ: {cleaned}\n\n"
                                f"История (последние сообщения):\n"
                            )
                            # Добавляем последние 5 сообщений из истории
                            for hmsg in history_messages[-5:]:
                                role_label = "Пользователь" if hmsg.role == "user" else "Caesar"
                                save_content += f"{role_label}: {hmsg.content[:300]}\n"
                            
                            await self._l3.add(
                                user_id=user_id,
                                channel=channel_name,
                                content=save_content,
                                task_id=task.id,
                                author_id=task.author_id or user_id,
                                metadata={
                                    "source": "user_explicit_save",
                                    "trigger": "запомни_это",
                                    "saved_at": datetime.now().isoformat(),
                                },
                            )
                            self.log.info("L3: saved by user command 'запомни это'")
                        except Exception as e:
                            self.log.warning(f"L3 explicit save failed: {e}")
                
                # Auto-save skill для complex задач с tool calls (не web tools)
                if is_background and used_tool_names and not (used_tool_names & WEB_SOURCE_TOOLS):
                    await self._maybe_auto_save_skill(
                        task, used_tool_names, step,
                        full_tool_history=full_tool_history,
                        emit_key=emit_key,
                    )
                
                return cleaned
        
        # Достигли max_steps
        return await self._force_finish(task, messages, f"Достигнут лимит шагов ({max_steps})")
    
    async def _analyze_request(self, task: Task, has_history: bool) -> dict:
        """Cheap LLM analyzer — анализирует запрос ДО умной LLM.
        
        Возвращает:
        {
            "is_trivial": bool,          # тривиальный (привет, спасибо)?
            "trivial_response": str,     # ответ если тривиальный
            "needs_tools": bool,         # нужны инструменты (web, shell)?
            "needs_memory": bool,        # нужен поиск в памяти?
            "complexity": "simple|medium|complex",
        }
        
        Если cheap LLM не настроена — возвращает пустой dict (fallback на умную).
        """
        # Если cheap LLM не настроена — пропускаем анализ
        if not self.llm or not self.llm.cheap.api_key:
            return {}
        
        user_msg = task.user_message.strip()
        
        # Быстрая эвристика БЕЗ LLM — для совсем тривиального
        # (не тратим токены на "привет", "ок", "спасибо")
        trivial_patterns = {
            "привет": "Привет! Чем могу помочь?",
            "здравствуй": "Здравствуйте! Чем могу помочь?",
            "здравствуйте": "Здравствуйте! Чем могу помочь?",
            "хай": "Привет! Чем могу помочь?",
            "hello": "Hello! How can I help?",
            "hi": "Hi! How can I help?",
            "ок": None,  # None = не отвечаем, пропускаем
            "окей": None,
            "хорошо": None,
            "понятно": None,
            "ладно": None,
            "да": None,
            "нет": None,
            "угу": None,
            "ага": None,
            "спасибо": "Пожалуйста!",
            "благодарю": "Пожалуйста!",
            "thanks": "You're welcome!",
            "thank you": "You're welcome!",
        }
        msg_lower = user_msg.lower().rstrip("!.,?")
        if msg_lower in trivial_patterns:
            response = trivial_patterns[msg_lower]
            if response is None:
                # Подтверждение (ок, да) — пропускаем к умной если есть история,
                # иначе просто игнорируем
                return {}
            return {
                "is_trivial": True,
                "trivial_response": response,
                "needs_tools": False,
                "needs_memory": False,
                "complexity": "simple",
            }
        
        # Cheap LLM анализ для нетривиальных запросов
        system_prompt = (
            "Ты — анализатор запросов. Проанализируй запрос пользователя "
            "и верни ТОЛЬКО JSON (без markdown, без объяснений):\n\n"
            "{\n"
            '  "is_trivial": false,\n'
            '  "trivial_response": "",\n'
            '  "needs_tools": false,\n'
            '  "needs_memory": false,\n'
            '  "complexity": "simple"\n'
            "}\n\n"
            "ПРАВИЛА:\n"
            "- is_trivial=true если это приветствие/прощание/благодарность/подтверждение\n"
            "  (привет, пока, спасибо, ок, да, нет). Тогда trivial_response = короткий ответ.\n"
            "- needs_tools=true если нужны действия: поиск в интернете, выполнение команд, "
            "чтение файлов, github, новости, перевод, анализ кода.\n"
            "- needs_memory=true если пользователь спрашивает про прошлые разговоры, "
            "загруженные документы, или использует слова 'вспомни', 'помнишь', 'что я говорил'.\n"
            "- complexity: simple (быстрый ответ), medium (нужно подумать/поискать), "
            "  complex (исследование, много шагов, анализ).\n\n"
            "ПРИМЕРЫ:\n"
            '"Как дела?" → {"is_trivial": true, "trivial_response": "Да нормально, работаю. А у тебя?", "needs_tools": false, "needs_memory": false, "complexity": "simple"}\n'
            '"Что такое агрессия?" → {"is_trivial": false, "trivial_response": "", "needs_tools": false, "needs_memory": true, "complexity": "simple"}\n'
            '"Найди новости про Hermes" → {"is_trivial": false, "trivial_response": "", "needs_tools": true, "needs_memory": false, "complexity": "medium"}\n'
            '"Проанализируй код Linux kernel" → {"is_trivial": false, "trivial_response": "", "needs_tools": true, "needs_memory": false, "complexity": "complex"}\n'
            '"Запомни что я люблю шашлык" → {"is_trivial": false, "trivial_response": "", "needs_tools": false, "needs_memory": false, "complexity": "simple"}\n'
        )
        
        try:
            resp = await self.llm.cheap_chat(
                messages=[
                    LLMMessage(role="system", content=system_prompt),
                    LLMMessage(role="user", content=user_msg[:500]),  # обрезаем длинные
                ],
                temperature=0.1,  # детерминированно
                max_tokens=200,
            )
        except Exception as e:
            self.log.warning(f"Cheap analyzer failed: {e}")
            return {}
        
        # Парсим JSON из ответа
        import json as _json
        import re as _re
        content = (resp.content or "").strip()
        
        # Убираем markdown обёртку если есть
        if content.startswith("```"):
            content = _re.sub(r"^```(?:json)?\s*", "", content)
            content = _re.sub(r"\s*```$", "", content)
        
        try:
            analysis = _json.loads(content)
            self.log.info(
                f"Cheap analyzer: trivial={analysis.get('is_trivial')}, "
                f"tools={analysis.get('needs_tools')}, "
                f"memory={analysis.get('needs_memory')}, "
                f"complexity={analysis.get('complexity')}"
            )
            return analysis
        except _json.JSONDecodeError:
            self.log.warning(f"Cheap analyzer returned non-JSON: {content[:100]}")
            return {}
    
    def _build_system_prompt(
        self,
        task: Task,
        memory_context: str = "",
        history_count: int = 0,
        l3_context: str = "",
    ) -> str:
        """Системный промпт для умной LLM."""
        prompt = f"""Ты — Caesar, AI-агент на Ubuntu. Помогай пользователю. Инструменты используй когда нужно.

ПРАВИЛА:
- Действуй сам, не переспрашивай. Не пиши "хочешь продолжить?" — продолжай.
- Если не получается после 3 попыток — честно скажи.
- Отвечай прямо, без "я нашёл", "согласно документу" — просто отвечай как собеседник.
- Пиши обычным текстом, без markdown. Списки — через тире.
- Если есть контекст из L3 (памяти) — используй его ПРЕЖДЕ интернета.
- "что нового про X" → ищи свежие новости (time_filter=week). "расскажи про X" → обзор.
- "запомни X" → вызови memory_add_fact. "удали X" → memory_delete.
- web_search пустой → github_releases, hn_search, reddit_search, wikipedia_read.
- "выполни команду X" → shell_exec. Не отказывайся до попытки.
- После 3 неудач — честно сообщи об ошибке с stderr.

ИНСТРУМЕНТЫ:
- shell_exec — команды терминала
- web_search — поиск (Bing+DDG), time_filter: day|week|month|year
- github_releases — релизы проектов (лучший способ узнать "что нового")
- memory_add_fact / memory_delete — L2 память
- memory_search — поиск по памяти
- read_file / write_file / edit_file — файлы
- skill_find — найти скилл
- cron_add / cron_list / cron_remove — планировщик
"""
        # ИЗНАЧАЛЬНАЯ ЗАДАЧА — чтобы LLM могла self-check перед вопросами
        if task.original_directive and task.original_directive != task.user_message:
            prompt += (
                f"\nИЗНАЧАЛЬНАЯ ЗАДАЧА ПОЛЬЗОВАТЕЛЯ (исходная постановка):\n"
                f"{task.original_directive[:1000]}\n"
                f"\nЭто полная изначальная задача. Если у тебя возникает вопрос — "
                f"сначала проверь можно ли ответить из этого текста.\n"
            )
        
        if history_count > 0:
            prompt += f"\nВ истории диалога {history_count} предыдущих сообщений. Учитывай их при ответе.\n"
        
        if memory_context:
            prompt += f"\n{memory_context}\n"
        
        if l3_context:
            prompt += f"\n{l3_context}\n"
            prompt += (
                "\nКАК ОТВЕЧАТЬ (ВАЖНО):\n"
                "- Отвечай НА ВОПРОС пользователя прямо, как обычный собеседник.\n"
                "- Используй материалы выше как СВОИ ЗНАНИЯ — не говори 'я нашёл', 'в документе сказано', 'согласно источнику'.\n"
                "- НЕ рассказывай где ты это взял, что ты думаешь по этому поводу, или что ты что-то нашёл.\n"
                "- Просто дай ответ на вопрос, как если бы ты всегда это знал.\n"
                "- Плохо: 'Я нашёл документ. В нём говорится, что агрессия — это...'\n"
                "- Хорошо: 'Агрессия — это форма поведения, направленная на причинение вреда...'\n"
            )
        
        return prompt
    
    # Инструменты, чьи вызовы имеют смысл сохранять в exact_recipe.
    # Это детерминированные действия (shell, file writes, code edits) —
    # их повторное выполнение даст тот же результат.
    RECIPE_WORTHY_TOOLS = {
        "shell_exec", "write_file", "edit_file",
        "self_edit", "self_install_package",
        "cron_add",
    }
    
    # Инструменты, которые НЕ имеют смысла сохранять (read-only или
    # non-deterministic). Если скилл состоит ТОЛЬКО из таких — не сохраняем.
    READ_ONLY_TOOLS = {
        "read_file", "find_files", "grep",
        "web_search", "web_fetch", "http_request",
        "github_releases", "github_search",
        "rss_read", "tg_read_channel",
        "hn_search", "reddit_search", "wikipedia_read",
        "parse_pdf", "parse_docx", "parse_xlsx", "parse_csv",
        "self_read", "self_scan", "self_test",
        "memory_search", "skill_find", "cron_list",
    }
    
    async def _maybe_auto_save_skill(
        self, task: Task, used_tools: set, steps: int,
        full_tool_history: list[dict] | None = None,
        emit_key: str = "",
    ) -> None:
        """Auto-save skill после complex задачи.
        
        V2 — с recipe extraction. Сохраняет реальные шаги из full_tool_history,
        а не пустой recipe как в V1.
        
        Критерии сохранения:
        - background задача (complex)
        - не использовались web tools (ответ из интернета — не рецепт)
        - ≥ 3 шагов
        - есть хотя бы 1 recipe-worthy tool call (shell_exec, write_file, etc.)
        - нет дубликата (skill с таким trigger уже есть)
        
        Что сохраняется:
        - exact_recipe: последовательность recipe-worthy tool calls с аргументами
        - anti_patterns: тексты ошибок из failed tool calls
        - pitfalls: 'Auto-saved skill — проверь recipe перед повторным использованием'
        - needs_validation=True (первое применение должно быть под контролем)
        
        После сохранения — отправляет INFO_NOTIFICATION пользователю с
        именем скилла и командой для удаления если не нужен.
        
        Args:
            emit_key: ключ event_bus (обычно task.source_chat_id) для отправки
                уведомления пользователю.
        """
        if not self._l4:
            return
        
        # Нужно минимум 3 шага
        if steps < 3:
            return
        
        # Нужна полная история tool calls
        if not full_tool_history:
            return
        
        # Извлекаем recipe-worthy steps
        recipe_steps = []
        anti_patterns = []
        for entry in full_tool_history:
            tool = entry.get("tool", "")
            args = entry.get("args", {}) or {}
            success = entry.get("success", True)
            error = entry.get("error", "")
            
            if tool in self.RECIPE_WORTHY_TOOLS:
                recipe_steps.append({
                    "tool": tool,
                    "args": args,
                    "step": entry.get("step", 0),
                })
            
            # Собираем anti_patterns из ошибок
            if not success and error:
                anti_patterns.append({
                    "error": error,
                    "tool": tool,
                    "args_preview": str(args)[:200],
                    "happened": datetime.now().isoformat(),
                })
        
        # Нет recipe-worthy шагей → не сохраняем (только read-only бесполезно)
        if not recipe_steps:
            self.log.info(
                f"Auto-save skill skipped: no recipe-worthy tools "
                f"(only read-only used: {used_tools})"
            )
            return
        
        # Генерируем имя скилла из запроса (без LLM — детерминированно)
        import re as _re
        msg = task.user_message.lower()[:60]
        # Убираем лишнее
        msg = _re.sub(r'[^\w\s-]', '', msg)
        msg = _re.sub(r'\s+', '_', msg.strip())
        # Оставляем только значимые слова (длинее 3 символов)
        words = [w for w in msg.split("_") if len(w) > 2]
        if not words:
            return
        
        skill_name = "auto_" + "_".join(words[:3])
        if len(skill_name) > 60:
            skill_name = skill_name[:60]
        
        # Проверяем не существует ли уже
        existing = self._l4.get_skill(skill_name)
        if existing:
            self.log.info(
                f"Skill '{skill_name}' already exists v{existing.version}, "
                f"skipping auto-save"
            )
            return
        
        # Создаём скилл с реальным recipe
        try:
            from caesar.memory.l4 import Skill
            
            # Готовим pitfalls — предупреждаем что это auto-saved
            pitfalls = [
                "Auto-saved skill — recipe сгенерирован автоматически из "
                "выполненной задачи. Проверь шаги перед повторным использованием.",
                f"Исходная задача: '{task.user_message[:200]}'",
            ]
            if anti_patterns:
                pitfalls.append(
                    f"Во время выполнения было {len(anti_patterns)} ошибок — "
                    f"они сохранены в anti_patterns. Учти их при следующем применении."
                )
            
            skill = Skill(
                name=skill_name,
                trigger=task.user_message[:200],
                version=1,
                created_at=datetime.now().isoformat(),
                notes=(
                    f"Auto-saved after complex task ({steps} steps, "
                    f"tools: {sorted(used_tools)}, "
                    f"recipe steps: {len(recipe_steps)})"
                ),
                exact_recipe=recipe_steps,
                anti_patterns=anti_patterns,
                pitfalls=pitfalls,
                needs_validation=True,  # первое применение — под контролем
            )
            
            self._l4.save_skill(skill)
            self.log.info(
                f"Auto-saved skill: '{skill_name}' "
                f"(trigger: '{task.user_message[:50]}...', "
                f"recipe: {len(recipe_steps)} steps, "
                f"anti_patterns: {len(anti_patterns)})"
            )
            
            # Уведомляем пользователя через event
            if self.event_bus and emit_key:
                try:
                    from caesar.core.events import info_notification
                    msg_parts = [
                        f"💾 Автоматически сохранил скилл: `{skill_name}`",
                        f"   • Шагов в recipe: {len(recipe_steps)}",
                        f"   • Анти-паттернов: {len(anti_patterns)}",
                        f"   • trigger: «{task.user_message[:60]}»",
                        "",
                        "В следующий раз когда попросишь похожее — выполню быстрее.",
                        f"Если не нужен — удали: `caesar skill remove {skill_name}`",
                    ]
                    await self.event_bus.emit(
                        emit_key,
                        info_notification("\n".join(msg_parts)),
                    )
                except Exception as e:
                    self.log.debug(f"Failed to send skill notification: {e}")
        except Exception as e:
            self.log.warning(f"Auto-save skill failed: {e}")
    
    def _gap_analysis(self, query: str, user_id: str, l3_results: list) -> str:
        """Gap Analysis — что мозг НЕ знает.
        
        V2 — расширенная версия. Проверяет:
        1. L3 пустой — нет ничего похожего в памяти
        2. Все чанки старые — нет свежих данных (>30 дней)
        3. Stale entities — сущности которые не упоминались > 30 дней
        4. Thin entities — сущности с mention_count=1 (мало данных)
        5. Missing relations — entity без связей
        6. Только consolidated — есть саммари, но нет свежих отдельных обсуждений
        
        Возвращает текст с предупреждениями или пустую строку.
        """
        gaps = []
        
        # 1. Анализ свежести найденных чанков
        if l3_results:
            now = datetime.now()
            stale_chunks = 0
            fresh_chunks = 0
            oldest_days = 0
            has_consolidated_only = True
            
            for r in l3_results:
                meta = getattr(r, "metadata", {}) or {}
                # created_at теперь сохраняется в metadata в l3.search
                created_at_str = meta.get("created_at") or ""
                if not created_at_str:
                    # Пробуем из chunk_id если есть mapping (обычно нет)
                    continue
                
                try:
                    created_at_str_norm = created_at_str.replace("T", " ").split(".")[0]
                    created_at = datetime.strptime(created_at_str_norm, "%Y-%m-%d %H:%M:%S")
                    age_days = (now - created_at).days
                    if age_days > 30:
                        stale_chunks += 1
                        if age_days > oldest_days:
                            oldest_days = age_days
                    else:
                        fresh_chunks += 1
                    
                    # Если это не consolidated — значит есть индивидуальные обсуждения
                    if meta.get("type") != "consolidated":
                        has_consolidated_only = False
                except (ValueError, TypeError):
                    continue
            
            # Если все чанки старые — предупреждаем
            if stale_chunks > 0 and fresh_chunks == 0 and oldest_days > 0:
                gaps.append(
                    f"- В памяти нет свежих данных по этому запросу "
                    f"(последнее упоминание — {oldest_days} дн. назад). "
                    f"Возможно стоит поискать в интернете или спросить позже."
                )
            elif has_consolidated_only and fresh_chunks == 0:
                # Этот case: чанки свежие (не старые), но все consolidated
                # (нет индивидуальных обсуждений за последний месяц)
                gaps.append(
                    "- По этой теме есть только consolidated саммари, "
                    "но нет свежих индивидуальных обсуждений за последний месяц."
                )
            elif has_consolidated_only and fresh_chunks > 0 and stale_chunks == 0:
                # Все чанки свежие и consolidated — меньше повода для тревоги,
                # но всё равно стоит упомянуть
                gaps.append(
                    "- По этой теме есть только consolidated саммари "
                    "(без индивидуальных обсуждений)."
                )
        
        # 2. Анализ entities из запроса через KG
        try:
            # Ищем entities в запросе
            from caesar.memory.knowledge_graph import extract_entities
            query_entities = extract_entities(query)
            
            for ent in query_entities[:3]:  # проверяем первые 3
                name = ent["name"]
                
                # Ищем entity в KG
                kg_ents = self._kg.search_entities(user_id, name, limit=1)
                
                if not kg_ents:
                    # Entity не найден — мозг не знает про это
                    gaps.append(f"- '{name}': нет данных в памяти")
                    continue
                
                kg_ent = kg_ents[0]
                
                # Проверяем staleness
                last_seen = kg_ent.get("last_seen", "")
                if last_seen:
                    try:
                        last_dt = datetime.fromisoformat(
                            str(last_seen).replace("Z", "").replace("T", " ").split(".")[0]
                        )
                        days_since = (datetime.now() - last_dt).days
                        if days_since > 30:
                            gaps.append(
                                f"- '{name}': не упоминался {days_since} дней — "
                                f"возможно информация устарела"
                            )
                    except Exception:
                        pass
                
                # Проверяем thinness
                mention_count = kg_ent.get("mention_count", 0)
                if mention_count == 1:
                    gaps.append(f"- '{name}': упомянут только 1 раз — мало данных")
                
                # Проверяем relations
                rels = self._kg.get_relations(user_id, name, "both")
                if not rels:
                    gaps.append(f"- '{name}': нет связей с другими сущностями")
        
        except Exception:
            pass
        
        return "\n".join(gaps) if gaps else ""
    
    def _gap_analysis_empty_l3(self, query: str, user_id: str) -> str:
        """Gap Analysis для случая когда L3 search ВЕРНУЛ ПУСТОЙ список.
        
        Проверяет:
        1. Есть ли вообще чанки в L3 для этого user_id
        2. Если есть — значит по этому конкретному запросу ничего не нашлось
        3. Проверяет entities из запроса в KG (как в _gap_analysis)
        
        Возвращает текст с предупреждениями или пустую строку.
        """
        gaps = []
        
        # Проверяем есть ли вообще чанки
        try:
            with self.storage._conn() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) as cnt FROM l3_chunks WHERE user_id = ?",
                    (user_id,),
                ).fetchone()
                total_chunks = row["cnt"] if row else 0
        except Exception:
            total_chunks = 0
        
        if total_chunks > 0:
            # В памяти что-то есть, но по этому запросу — пусто
            gaps.append(
                f"- По этому запросу в памяти ничего не найдено "
                f"(всего чанков: {total_chunks}). Возможно стоит "
                f"переформулировать или это новая тема."
            )
        
        # Проверяем entities из запроса
        try:
            from caesar.memory.knowledge_graph import extract_entities
            query_entities = extract_entities(query)
            
            for ent in query_entities[:3]:
                name = ent["name"]
                kg_ents = self._kg.search_entities(user_id, name, limit=1)
                if not kg_ents:
                    gaps.append(f"- '{name}': нет данных в памяти")
        except Exception:
            pass
        
        return "\n".join(gaps) if gaps else ""
    
    def _detect_direct_command(self, user_message: str, user_id: str = "") -> str | None:
        """Детектор прямых команд — когда пользователь явно просит
        выполнить shell команду.
        
        Args:
            user_message: сообщение пользователя
            user_id: ID пользователя (для проверки god_mode)
        
        Возвращает команду для выполнения, или None если это не прямой запрос.
        """
        if not user_message:
            return None
        
        msg = user_message.strip()
        msg_lower = msg.lower()
        
        import re as _re
        
        # Список опасных паттернов — НЕ выполняем напрямую.
        # ВАЖНО: в full mode (config.mode=full) ИЛИ god mode НЕ блокируем НИЧЕГО.
        # is_dangerous_command (rm -rf /) внутри ShellExecTool тоже игнорируется.
        tools = getattr(self, "tools", None)
        access_mode = getattr(tools, "access_mode", "sandboxed") if tools else "sandboxed"
        
        # Проверяем god_mode для этого пользователя
        god_mode = False
        storage = getattr(self, "storage", None)
        if storage and user_id:
            try:
                god_mode = storage.get_user_god_mode(user_id)
            except Exception:
                pass
        
        if access_mode == "full" or god_mode:
            # Full/god mode — не блокируем ничего в детекторе.
            pass
        else:
            dangerous_patterns = [
                r"\brm\s+-rf?\b",
                r"\bsudo\b",
                r"\bchmod\s+-R\b",
                r"\bchown\s+-R\b",
                r"\bmkfs\b",
                r"\bdd\s+if=",
                r">\s*/dev/",
                r"\bapt\s+(remove|purge)\b",
                r"\bpip\s+uninstall\b",
                r"\bshutdown\b",
                r"\breboot\b",
                r"\bhalt\b",
            ]
            for pattern in dangerous_patterns:
                if _re.search(pattern, msg_lower):
                    return None  # Пусть LLM обработает с подтверждением
        
        # Паттерны "выполни команду X" / "запусти X"
        # Поддерживаем кавычки и без
        command_patterns = [
            # "выполни команду X" / "выполни команду \"X\""
            (r"^\s*выполни\s+команд[уые]\s+[\"'`]?(.+?)[\"'`]?\s*$", 1),
            # "выполни X" — но не "выполни задачу" etc
            (r"^\s*выполни\s+[\"'`]?(.+?)[\"'`]?\s*$", 1),
            # "запусти команду X"
            (r"^\s*запусти\s+команд[уые]\s+[\"'`]?(.+?)[\"'`]?\s*$", 1),
            # "запусти X"
            (r"^\s*запусти\s+[\"'`]?(.+?)[\"'`]?\s*$", 1),
            # "run command X" / "run X"
            (r"^\s*run\s+(?:command\s+)?[\"'`]?(.+?)[\"'`]?\s*$", 1),
            # "execute X"
            (r"^\s*execute\s+[\"'`]?(.+?)[\"'`]?\s*$", 1),
        ]
        
        for pattern, group in command_patterns:
            m = _re.match(pattern, msg, _re.IGNORECASE)
            if m:
                cmd = m.group(group).strip()
                # Убираем trailing точку/вопрос
                cmd = cmd.rstrip(".?")
                if cmd and len(cmd) > 1:
                    return cmd
        
        # Паттерны с предустановленными командами
        if msg_lower in ("покажи логи", "покажи лог", "покажи логи демона", "покажи лог демона",
                         "логи демона", "логи caesar", "log", "logs"):
            return "journalctl --user -u caesar-daemon -n 50 --no-pager"
        
        if msg_lower in ("статус сервиса", "статус демона", "статус caesar",
                         "service status", "daemon status"):
            return "systemctl --user status caesar-daemon --no-pager"
        
        # Рестарт daemon — пользователь явно просит перезапустить бота.
        # Это ОПАСНО (сервис упадёт), но это явная воля пользователя.
        # Без этого пользователь без SSH доступа не может управлять сервером.
        restart_triggers = [
            "рестартни daemon", "рестартни демона", "рестартни caesar",
            "перезапусти daemon", "перезапусти демона", "перезапусти caesar",
            "перезагрузи daemon", "перезагрузи демона", "перезагрузи caesar",
            "рестарт daemon", "рестарт демона", "рестарт caesar",
            "restart daemon", "restart caesar", "restart bot",
            "перезапусти бота", "рестартни бота", "перезагрузи бота",
            "рестартни сервер", "перезапусти сервер",
        ]
        if msg_lower in restart_triggers:
            return "systemctl --user restart caesar-daemon"
        
        # Прямые команды в начале сообщения (без преамбулы).
        # Любая команда начинающаяся с этих префиксов — выполняется напрямую,
        # минуя LLM. systemctl — ВСЕ варианты (status, restart, stop, start).
        # Если это restart caesar-daemon — будет перехвачено выше is_daemon_restart.
        direct_command_starts = [
            "cat ", "ls ", "grep ", "find ", "head ", "tail ", "wc ",
            "ps ", "df ", "du ", "journalctl ", "systemctl ",
            "git log", "git status", "git diff", "git show",
            "date", "whoami", "hostname", "uname",
            "echo ", "kill ", "pkill ", "killall ",
            "apt ", "pip ", "npm ",
            "curl ", "wget ",
            "chmod ", "chown ", "mkdir ", "rmdir ", "cp ", "mv ",
        ]
        
        # Если сообщение начинается с одной из этих команд — это прямая команда
        for prefix in direct_command_starts:
            if msg_lower.startswith(prefix) or msg.startswith(prefix):
                # Проверяем что это не слишком длинный текст (вероятно не команда)
                if len(msg) < 500:
                    # Убираем trailing точку
                    return msg.rstrip(".")
        
        # "что в файле X" → cat X
        m = _re.match(r"^\s*что\s+(?:в\s+)?файле\s+[\"'`]?(.+?)[\"'`]?\s*[\.?]?\s*$", msg, _re.IGNORECASE)
        if m:
            path = m.group(1).strip()
            return f"cat {path}"
        
        return None
    
    def _should_search_l3(self, user_message: str, has_history: bool, has_l2_facts: bool) -> bool:
        """Решить, нужен ли авто-поиск в L3.
        
        Логика:
        - Если L1 (история) и L2 (факты) пустые — ОБЯЗАТЕЛЬНО ищем в L3.
          Это случай когда пользователь спрашивает про загруженный документ
          или про давний диалог — в недавней истории этого нет.
        - Если L1 или L2 есть — ищем в L3 только если запрос содержит
          "вспомни", "что я говорил", "согласно", "по документу" и т.д.
          (явный сигнал что нужно искать в долгой памяти).
        """
        if not user_message or len(user_message.strip()) < 3:
            return False
        
        # Нет ни истории ни фактов — точно ищем в L3
        if not has_history and not has_l2_facts:
            return True
        
        # Есть история/факты — ищем только при явных сигналах
        msg_lower = user_message.lower()
        l3_triggers = [
            "вспомни", "помнишь", "что я говорил", "что мы обсуждали",
            "согласно", "по документу", "в документе", "из документа",
            "раньше я", "я говорил", "мы говорили", "в прошлый раз",
            "как ты говорил", "как мы делали", "по тексту", "в тексте",
            "книга", "пересказ", "автор", "глава",
        ]
        return any(trigger in msg_lower for trigger in l3_triggers)
    
    def _should_save_to_l3(
        self, user_message: str, assistant_content: str, tool_calls_count: int,
        used_tool_names: set | None = None, used_web_tools: set | None = None,
    ) -> bool:
        """Решить, стоит ли сохранять диалог в L3.
        
        L3 — это записная книжка, а не лог всех разговоров.
        Сохраняем ТОЛЬКО важное:
        
        ВСЕГДА СОХРАНЯЕМ:
        - Пользователь явно просит: "запомни", "сохрани", "запиши"
        - Ответ содержит код (```...```)
        - Ответ содержит URL (http://, https://)
        - Ответ > 800 символов (substantial explanation)
        - Было > 2 tool calls (research/complex task)
        
        НИКОГДА НЕ СОХРАНЯЕМ:
        - Trivial: "привет", "ок", "хорошо", "понятно"
        - Ответы < 100 символов без кода/URL
        - Болтовня ни о чём (шашлык-машлык)
        - Ответ из интернета (web_search/hn_search/etc) — интернет уже это знает,
          не дублируем. ИСКЛЮЧЕНИЕ: explicit "запомни" — тогда сохраняем даже
          из интернета (пользователь явно попросил).
        """
        triggers = self._l3_save_triggers(
            user_message, assistant_content, tool_calls_count,
            used_tool_names, used_web_tools,
        )
        return len(triggers) > 0
    
    def _l3_save_triggers(
        self, user_message: str, assistant_content: str, tool_calls_count: int,
        used_tool_names: set | None = None, used_web_tools: set | None = None,
    ) -> list[str]:
        """Вернуть список триггеров почему сохраняем в L3 (для логов/метаданных)."""
        triggers = []
        
        msg_lower = user_message.lower()
        content_lower = assistant_content.lower()
        content_stripped = assistant_content.strip()
        
        # 1. Явный запрос "запомни/сохрани/запиши это"
        explicit_triggers = [
            "запомни", "сохрани", "запиши", "запомнить",
            "имей в виду", "учти на будущее", "важно:",
        ]
        is_explicit = any(t in msg_lower for t in explicit_triggers)
        if is_explicit:
            triggers.append("explicit_request")
        
        # 2. Ответ содержит код (``` блоки)
        if "```" in assistant_content:
            triggers.append("code_block")
        
        # 3. Ответ содержит URL
        if "http://" in content_lower or "https://" in content_lower:
            triggers.append("urls")
        
        # 4. Substantial explanation (>800 символов)
        if len(content_stripped) > 800:
            triggers.append("long_explanation")
        
        # 5. Complex task (>2 tool calls — research/deep work)
        # НО: если это web tools — не считаем complex_task, ответ из интернета
        # не должен авто-сохраняться.
        if tool_calls_count > 2 and not used_web_tools:
            triggers.append("complex_task")
        
        # 6. Содержит инструкции/шаги (1. 2. 3. или - шаг1)
        import re
        if re.search(r'\n\d+\.\s', assistant_content) or re.search(r'\n- \S', assistant_content):
            if len(content_stripped) > 80:
                triggers.append("structured_steps")
        
        # 7. Содержит факт/определение
        fact_indicators = [
            "определение", "по определению", "означает", "это значит",
            "работает так:", "состоит из", "включает в себя",
            "преимущество", "недостаток", "особенность",
        ]
        if any(t in content_lower for t in fact_indicators) and len(content_stripped) > 200:
            triggers.append("factual_content")
        
        # Фильтр от trivial
        trivial_responses = [
            "привет", "ок", "хорошо", "понятно", "да", "нет",
            "спасибо", "пожалуйста", "ладно", "угу", "ага",
            "норм", "окей", "хорош",
        ]
        is_trivial = (
            len(content_stripped) < 100 and
            not triggers and
            any(content_stripped.lower().startswith(t) for t in trivial_responses)
        )
        if is_trivial:
            return []
        
        # ВАЖНО: если ответ пришёл из интернета (web tools) и пользователь
        # НЕ просил явно "запомни" — НЕ сохраняем в L3.
        # Интернет уже это знает, не дублируем.
        if used_web_tools and not is_explicit:
            self.log.info(
                f"L3 save BLOCKED: web tools used ({used_web_tools}) "
                f"without explicit 'запомни' request"
            )
            return []
        
        return triggers

    
    def _get_max_steps(self, complexity: TaskComplexity | str) -> int:
        c = complexity.value if hasattr(complexity, 'value') else str(complexity)
        return {
            "simple": self.config.orchestrator.max_steps_simple,
            "medium": self.config.orchestrator.max_steps_medium,
            "complex": self.config.orchestrator.max_steps_complex,
        }.get(c, 25)
    
    def _get_max_tokens(self, complexity: TaskComplexity | str) -> int:
        c = complexity.value if hasattr(complexity, 'value') else str(complexity)
        return {
            "simple": self.config.orchestrator.max_tokens_simple,
            "medium": self.config.orchestrator.max_tokens_medium,
            "complex": self.config.orchestrator.max_tokens_complex,
        }.get(c, self.config.orchestrator.max_tokens_simple)
    
    def _clean_response_style(self, text: str) -> str:
        """Вырезает 'поисковый' стиль + LLM артефакты из ответа.
        
        1. Убирает </think> теги и всё что после них если LLM застряла в thinking loop
        2. Детектит повторяющийся контент (LLM зациклилась на одной фразе)
        3. Убирает поисковые преамбулы
        """
        if not text:
            return text
        
        import re
        
        # === КРИТИЧНО: detect и cut thinking loops ===
        # LLM (особенно с reasoning) может застрять в цикле:
        # </think>Попробую через webcache...</think>Попробую через webcache...
        # повторяя одну фразу сотни раз
        
        # 1. Убираем всё что содержит </think> — это reasoning артефакт
        # Если в тексте есть </think>, берём только то что ПОСЛЕ последнего </think>
        if "</think>" in text:
            parts = text.rsplit("</think>", 1)
            after_think = parts[-1].strip()
            if after_think and len(after_think) > 20:
                text = after_think
            else:
                # Если после </think> пусто — берём текст ДО первого </think>
                text = text.split("</think>")[0].strip()
                # Убираем <think> тоже
                text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
        
        # Убираем одиночные <think> теги
        text = re.sub(r"</?think>", "", text).strip()
        
        # 2. Детектим повторяющийся контент
        # Если текст содержит ОДНУ фразу повторённую > 5 раз — это цикл
        # Проверяем: берём фразу до первой точки, ищем её повторения
        if len(text) > 300:
            first_sentence = text.split('.')[0].strip() if '.' in text else text[:50].strip()
            if first_sentence and len(first_sentence) > 15:
                count = text.count(first_sentence)
                if count > 5:
                    text = first_sentence + ".\n\n⚠️ (Ответ был обрезан — LLM зациклилась)"
        
        # 3. Ограничиваем длину ответа — 10000 символов максимум
        if len(text) > 10000:
            text = text[:10000] + "\n\n⚠️ (Ответ обрезан — слишком длинный)"
        
        # Паттерны вступлений которые нужно убрать (в начале ответа)
        intro_patterns = [
            # "По результатам поиска в L3, вот что можно собрать из документа X:"
            r"^По результатам поиска[^:]*:\s*\n*",
            # "По результатам поиска[^:]*"
            r"^По результатам[^\.]*\.\s*",
            # "Вот что я нашёл:" / "Вот что нашёл:"
            r"^Вот что (я )?нашёл[^:]*:\s*\n*",
            r"^Вот что (я )?нашёл:\s*\n*",
            # "Я нашёл документ. В нём говорится..." — убираем оба предложения
            r"^Я нашёл (документ|материал|информацию|файл)[^\.]*\.\s*(В нём (говорится|указывается|написано)[^\.]*\.\s*)?",
            r"^Я (нашёл|нашла)[^\.]*\.\s*",
            # "Нашёл файл X на диске." / "Нашёл документ X."
            r"^Нашёл (файл|документ|материал)[^\.]*\.\s*",
            r"^Нашла (файл|документ|материал)[^\.]*\.\s*",
            # "Согласно документу/материалу/источнику..."
            r"^Согласно (документу|материалу|источнику|загруженному)[^:]*:\s*\n*",
            r"^Согласно (документу|материалу|источнику)[^\.]*\.\s*",
            # "В документе/материале говорится..." — убираем полностью предложение
            r"^В (документе|материале|источнике) (говорится|указывается|написано|отмечается)[^\.]*\.\s*",
            r"^В (документе|материале|источнике)[^\.]*\.\s*",
            # "У Проппа/в книге X..." — ссылка на источник
            r"^У [А-ЯЁ][^\.]* (говорится|указывается|написано|отмечается|связано)[^\.]*\.\s*",
            r"^У [А-ЯЁ][^\.]*\.\s*",
            r"^В книге [^\.]*\.\s*",
            r"^В тексте [^\.]*\.\s*",
            # "Из документа следует..."
            r"^Из (документа|материала|источника) следует[^:]*:\s*\n*",
            r"^Из (документа|материала|источника)[^\.]*\.\s*",
            # "Вот ответ на ваш вопрос:"
            r"^Вот ответ[^:]*:\s*\n*",
            # "Вот информация..."
            r"^Вот информация[^:]*:\s*\n*",
            # "На основе найденных материалов:"
            r"^На основе (найденных|полученных)[^:]*:\s*\n*",
            # "Из найденного материала можно сделать вывод..."
            r"^Из найденного[^:]*:\s*\n*",
            r"^Из (полученного|найденного) материала[^\.]*\.\s*",
            # "Вот что у Проппа связано с..." / "Вот что у X связано с..."
            r"^Вот что у [А-ЯЁ][^:]*:\s*\n*",
            r"^Вот что (связано|написано|говорится)[^:]*:\s*\n*",
        ]
        
        cleaned = text
        for pattern in intro_patterns:
            cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE)
        
        # ВЫРЕЗАЕМ ВОПРОСЫ "продолжить?" из КОНЦА ответа
        # Эти вопросы не нужны — пользователь уже сказал что хочет
        trailing_question_patterns = [
            r"\s*(Хочешь|Хотите),?\s+я\s+(продолж|начн|попробу|сдела)[^?]*\??\s*$",
            r"\s*(Продолжить|Продолжаем)\??\s*$",
            r"\s*(Идём|Идем)\s+дальше\??\s*$",
            r"\s*(Начать|Начинаем)\??\s*$",
            r"\s*(Могу|Можно)\s+ли\s+я\s+(продолж|начат)[^?]*\??\s*$",
            r"\s*(Начать|Продолжить)\s+(внедрение|с\s+первой|со\s+следующ)[^?]*\??\s*$",
            r"\s*(Внедрять|Внедрить)\s*\??\s*$",
            r"\s*(Что\s+скажешь|Как\s+решишь)\??\s*$",
            r"\s*Будем\s+(продолжать|внедрять)\??\s*$",
            r"\s*Желаете\s+(продолжить|начать)\??\s*$",
            r"\s*(Приступить|Приступаем)\s*(к|со)\s*[^?]*\??\s*$",
            r"\s*Готовы?\s+(продолжить|начать)\??\s*$",
            r"\s*(Нужно\s+ли|Стоит\s+ли)\s+(продолж|внедр)[^?]*\??\s*$",
            r"\s*(Подтверждаешь|Подтвердить)\??\s*$",
            r"\s*(Делаем|Сделать)\s*\??\s*$",
        ]
        
        for pattern in trailing_question_patterns:
            cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE)
        
        # Убираем XML/HTML разметку которую некоторые LLM добавляют
        # (GLM, Qwen и др. оборачивают ответы в теги)
        xml_patterns = [
            r'</?function_results>',
            r'</?function_calls>',
            r'</?html>.*?</?body>\s*',
            r'</?body>',
            r'</?tool_call>',
            r'</?response>',
            r'</?result>',
            r'</?output>',
            r'<think>.*?</think>',  # thinking blocks
            r'</?thinking>',
        ]
        for pattern in xml_patterns:
            cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE | re.DOTALL)
        
        # Убираем ведущие пробелы/переносы после очистки
        cleaned = cleaned.lstrip("\n ").strip()
        
        # Если после очистки ничего не осталось — значит весь текст был вступлением.
        # Возвращаем оригинал (лучше кривой ответ чем пустой).
        if not cleaned:
            return text
        
        # Если осталось слишком мало (< 10 символов) — тоже оригинал
        if len(cleaned) < 10:
            return text
        
        # Логируем если что-то убрали
        if cleaned != text:
            self.log.info(f"Response style cleaned: removed {len(text) - len(cleaned)} chars of intro")
        
        return cleaned
    
    def _is_duplicate_call(self, call: dict, recent: list[dict]) -> bool:
        """Проверить, был ли такой же вызов недавно."""
        for r in recent[-3:]:
            if r.get("name") == call.get("name") and r.get("args") == call.get("args"):
                return True
        return False
    
    async def _force_finish(self, task: Task, messages: list[LLMMessage], reason: str) -> str:
        """Принудительно завершить задачу с partial delivery.
        
        Собирает полезный частичный результат из:
        1. Последнего assistant-сообщения с контентом
        2. ВСЕХ tool results (role=tool) — извлекаем meaningful данные
        3. Если ничего нет — честно говорим что не нашли
        """
        self.log.warning(f"Task {task.id} force finish: {reason}")
        
        partial_parts: list[str] = []
        
        # 1. Последний assistant контент (если есть)
        for m in reversed(messages):
            if m.role == "assistant" and m.content and m.content.strip():
                partial_parts.append(m.content.strip())
                break
        
        # 2. Агрегируем meaningful tool results
        tool_findings: list[str] = []
        for m in messages:
            if m.role != "tool" or not m.content:
                continue
            try:
                data = json.loads(m.content)
            except (json.JSONDecodeError, TypeError):
                continue
            
            # Пропускаем skipped results
            if isinstance(data, dict) and data.get("skipped"):
                continue
            
            tool_name = m.name or "tool"
            
            # web_search / hn_search / reddit_search — извлекаем результаты
            if isinstance(data, dict):
                results = data.get("results") or data.get("posts") or []
                if isinstance(results, list) and results:
                    findings = []
                    for r in results[:5]:
                        if isinstance(r, dict):
                            title = r.get("title", "")
                            url = r.get("url", "") or r.get("link", "")
                            if title or url:
                                findings.append(f"  - {title}" + (f" ({url})" if url else ""))
                    if findings:
                        engine = data.get("engine", tool_name)
                        tool_findings.append(f"[{engine}] Найдено:")
                        tool_findings.extend(findings)
                
                # wikipedia_read / web_fetch / другие — content
                elif data.get("content") and isinstance(data["content"], str):
                    content_preview = data["content"][:500]
                    tool_findings.append(f"[{tool_name}] {content_preview}")
                
                # text — для инструментов которые возвращают data["text"]
                elif data.get("text") and isinstance(data["text"], str):
                    text_preview = data["text"][:500]
                    tool_findings.append(f"[{tool_name}] {text_preview}")
        
        if tool_findings:
            partial_parts.append("Что удалось найти:")
            partial_parts.extend(tool_findings)
        
        if partial_parts:
            return f"⚠️ {reason}\n\nЧастичный результат:\n\n" + "\n".join(partial_parts)
        
        return f"⚠️ {reason}\n\nНе удалось получить частичный результат."

    
    def _echo(self, message: str) -> str:
        return (
            f"🤖 Эхо-режим\n\n"
            f"Получил сообщение: {message}\n\n"
            f"LLM API ключ не настроен. Запусти: caesar setup"
        )
