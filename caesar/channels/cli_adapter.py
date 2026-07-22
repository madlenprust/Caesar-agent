"""CLI-адаптер.

Подключается к daemon через unix socket. Два режима:
- `agent "текст"` → one-shot
- `agent` → REPL

См. roadmap раздел 13.2.
"""

import asyncio
import os
import uuid
from datetime import datetime
from typing import Any

from caesar.config import Config
from caesar.core.events import (
    EventBus,
    Event,
    EVENT_PROGRESS_START,
    EVENT_PROGRESS_UPDATE,
    EVENT_ANSWER_READY,
    EVENT_QUESTION_ASKED,
    EVENT_FILE_READY,
    EVENT_ERROR_OCCURRED,
    EVENT_INFO_NOTIFICATION,
    EVENT_WARNING_NOTIFICATION,
)
from caesar.core.queue import TaskQueue, TaskPriority, TaskComplexity, Task
from caesar.logging_setup import get_logger


class CLISession:
    """Активная CLI-сессия."""
    
    def __init__(self, session_id: str, user_id: str, channel_id: str):
        self.session_id = session_id
        self.user_id = user_id
        self.channel_id = channel_id
        self.events_queue: asyncio.Queue = asyncio.Queue()
        self.last_icon: str | None = None
        # task_id → Promise-результат для опроса статуса
        self.task_ids: list[str] = []
        # Метка последней активности — для вытеснения простаивающих сессий
        self.last_activity: datetime = datetime.now()
    
    async def emit(self, event: Event) -> None:
        """Получить event от ядра, положить в очередь клиенту."""
        # Для прогресса — не дублируем подряд одинаковые иконки
        if event.type == EVENT_PROGRESS_UPDATE:
            icon = event.data.get("icon")
            if icon == self.last_icon:
                return
            self.last_icon = icon
        elif event.type == EVENT_PROGRESS_START:
            self.last_icon = "🧠"
        
        await self.events_queue.put(event)


class CLIAdapter:
    """CLI-адаптер на стороне daemon-а."""
    
    def __init__(
        self,
        config: Config,
        event_bus: EventBus,
        queue: TaskQueue,
        storage=None,
        daemon: Any = None,
    ):
        self.config = config
        self.event_bus = event_bus
        self.queue = queue
        self.storage = storage
        # Ссылка на AgentDaemon — для вызова on-demand методов
        # (например, _run_dream_cycle_on_demand для команды 'проиндексируй память')
        self._daemon = daemon
        self.log = get_logger("cli_adapter")
        self._sessions: dict[str, CLISession] = {}
    
    async def start(self) -> None:
        self.log.info("CLI adapter started")
    
    async def stop(self) -> None:
        # Отписываем и очищаем все сессии при остановке daemon-а
        for sid, session in list(self._sessions.items()):
            self.event_bus.unsubscribe(sid, session.emit)
        self._sessions.clear()
        self.log.info("CLI adapter stopped")
    
    async def handle_request(self, request: dict) -> dict:
        action = request.get("action", "")

        # Вытесняем простаивающие сессии при любом обращении —
        # иначе self._sessions растёт без ограничений (CLI session leak).
        self._cleanup_stale_sessions()

        if action == "send_message":
            return await self._handle_send_message(request)
        elif action == "get_events":
            return await self._handle_get_events(request)
        elif action == "get_status":
            return await self._handle_get_status(request)
        elif action == "list_tasks":
            return await self._handle_list_tasks(request)
        elif action == "index_memory":
            return await self._handle_index_memory(request)
        else:
            return {"error": "unknown_action", "message": f"Unknown action: {action}"}
    
    async def _handle_index_memory(self, request: dict) -> dict:
        """Команда 'проиндексируй память' — запустить on-demand dream cycle.
        
        Триггерит topic consolidation (группировку чанков по темам с созданием
        consolidated summaries). По умолчанию обрабатывает ВСЕ не-консолидированные
        чанки (force_all=True), не только за 24 часа.
        
        Args (в request):
            force_all: bool (default True) — консолидировать все чанки
            topic_only: bool (default True) — только topic consolidation, без enrich
        
        Returns:
            {
                "topics_consolidated": N,
                "chunks_created": N,
                "chunks_processed": N,
                "duration_sec": float,
            }
        """
        force_all = request.get("force_all", True)
        topic_only = request.get("topic_only", True)
        
        if not self._daemon:
            return {
                "error": "no_daemon",
                "message": "CLIAdapter not connected to daemon",
            }
        
        try:
            report = await self._daemon._run_dream_cycle_on_demand(
                force_all=force_all,
                force_topic_only=topic_only,
            )
            return report
        except Exception as e:
            self.log.exception(f"index_memory failed: {e}")
            return {"error": str(e)}
    
    async def _handle_send_message(self, request: dict) -> dict:
        """Обработать сообщение от пользователя — ставит задачу в очередь."""
        # Roadmap раздел 14.7: терминал и главный TG-бот = одна сессия (main)
        # user_id определяется через unix_uid — если пользователь уже зарегистрирован
        # (например через TG), используем тот же user_id
        raw_user_id = request.get("user_id", f"cli-{os.getuid()}")
        channel_name = request.get("channel_name", "main")
        message = request.get("message", "")
        session_id = request.get("session_id", "")
        
        if not message:
            return {"error": "empty_message"}
        
        # Ищем существующего пользователя по unix_uid
        user_id = raw_user_id
        if self.storage:
            existing = self.storage.get_user_by_uid(os.getuid())
            if existing:
                user_id = existing["id"]
            else:
                # Создаём нового с cli- префиксом
                user_id = raw_user_id
        
        # Детерминированный channel_id — одинаковый для CLI и TG (main)
        channel_id = f"channel:{user_id}:{channel_name}"
        
        if not session_id:
            session_id = f"cli-session-{user_id}-{channel_name}"
        
        # СОЗДАЁМ user и channel в БД если их ещё нет (для FOREIGN KEY)
        if self.storage:
            self.storage.upsert_user(
                user_id=user_id,
                unix_uid=os.getuid(),
                display_name=os.environ.get("USER", "user"),
            )
            self.storage.upsert_channel(
                channel_id=channel_id,
                user_id=user_id,
                source="cli",
                source_chat_id=session_id,
                display_name=channel_name,
            )
        
        # Получаем или создаём сессию
        if session_id not in self._sessions:
            session = CLISession(session_id, user_id, channel_id)
            self._sessions[session_id] = session
            # Подписка на session_id (уникальный для CLI) — НЕ на channel_id
            self.event_bus.subscribe(session_id, session.emit)
        else:
            session = self._sessions[session_id]
        session.last_activity = datetime.now()
        
        # Определяем сложность (простая эвристика, потом заменится cheap LLM)
        complexity = self._estimate_complexity(message)
        
        # Создаём задачу
        task = await self.queue.add_task(
            user_message=message,
            user_id=user_id,
            channel_id=channel_id,
            author_id=user_id,
            source="cli",
            source_chat_id=session_id,
            priority=TaskPriority.HIGH,
            complexity=complexity,
        )
        
        session.task_ids.append(task.id)
        
        return {
            "task_id": task.id,
            "session_id": session_id,
            "channel_id": channel_id,
        }
    
    def _estimate_complexity(self, message: str) -> TaskComplexity:
        """Грубая оценка сложности без LLM (V0.2 заменится на cheap LLM)."""
        msg_lower = message.lower()
        complex_markers = [
            "проанализируй", "изучи", "найди баги", "отрефактори",
            "оптимизируй", "перепиши", "создай проект", "разработай",
            "анализ кодовой", "индексируй",
        ]
        medium_markers = [
            "найди новости", "сделай сводку", "отчёт", "дай обзор",
            "собери информацию", "переведи", "напиши текст",
        ]
        if any(m in msg_lower for m in complex_markers):
            return TaskComplexity.COMPLEX
        if any(m in msg_lower for m in medium_markers):
            return TaskComplexity.MEDIUM
        return TaskComplexity.SIMPLE
    
    async def _handle_get_events(self, request: dict) -> dict:
        """Получить events для сессии."""
        session_id = request.get("session_id", "")
        timeout = request.get("timeout", 30)
        
        session = self._sessions.get(session_id)
        if not session:
            return {"error": "unknown_session"}

        session.last_activity = datetime.now()
        events = []
        try:
            event = await asyncio.wait_for(
                session.events_queue.get(),
                timeout=timeout,
            )
            events.append(self._serialize_event(event))
            
            while not session.events_queue.empty():
                event = session.events_queue.get_nowait()
                events.append(self._serialize_event(event))
        except asyncio.TimeoutError:
            pass
        
        return {"events": events}
    
    def _serialize_event(self, event: Event) -> dict:
        return {
            "type": event.type,
            "task_id": event.task_id,
            "timestamp": event.timestamp.isoformat(),
            "data": event.data,
        }

    # Сколько секунд сессия может простаивать без опроса, прежде чем её
    # вытеснят. CLI-клиент опрашивает get_events активно; простаивающие
    # сессии (закрытый терминал, упавший процесс) иначе копятся в памяти.
    SESSION_TTL_SEC = 3600

    def _cleanup_stale_sessions(self) -> None:
        """Удалить сессии без активности дольше SESSION_TTL_SEC."""
        now = datetime.now()
        stale = [
            sid for sid, s in self._sessions.items()
            if (now - s.last_activity).total_seconds() > self.SESSION_TTL_SEC
        ]
        for sid in stale:
            session = self._sessions.pop(sid, None)
            if session:
                self.event_bus.unsubscribe(sid, session.emit)
                self.log.info(f"Evicted stale CLI session {sid}")
    
    async def _handle_get_status(self, request: dict) -> dict:
        """Расширенный статус: daemon, memory, cron, tokens, recent dialogs."""
        from caesar.core.status import generate_status_report
        
        # Получаем user_id из запроса (опционально — для фильтрации recent dialogs)
        user_id = request.get("user_id", "")
        
        # Uptime — если daemon передал, используем, иначе None
        uptime = None
        if hasattr(self, "_daemon") and self._daemon:
            uptime = getattr(self._daemon, "_start_time", None)
            if uptime:
                uptime = (datetime.now() - uptime).total_seconds()
        
        # Version — из пакета
        try:
            from caesar import __version__
            version = __version__
        except Exception:
            version = "unknown"
        
        report = generate_status_report(
            storage=self.storage,
            queue=self.queue,
            version=version,
            uptime_seconds=uptime,
            user_id=user_id,
        )
        
        # Добавляем workers info от queue
        if self.queue:
            report.setdefault("daemon", {})["workers"] = {
                "interactive_active": self.queue.get_active_count("interactive"),
                "interactive_max": 5,
                "background_active": self.queue.get_active_count("background"),
                "background_max": 10,
                "interactive_pending": self.queue.get_pending_count("interactive"),
                "background_pending": self.queue.get_pending_count("background"),
            }
        
        # Добавляем active sessions count (старое поле для обратной совместимости)
        report["status"] = "running"
        report["active_sessions"] = len(self._sessions)
        
        return report
    
    async def _handle_list_tasks(self, request: dict) -> dict:
        """Список активных и pending задач."""
        active = self.queue.list_active_tasks()
        pending = self.queue.list_pending_tasks()
        return {
            "active": [
                {
                    "id": t.id,
                    "status": t.status.value if hasattr(t.status, 'value') else str(t.status),
                    "message": t.user_message[:80],
                    "step": t.current_step,
                }
                for t in active
            ],
            "pending": [
                {
                    "id": t.id,
                    "message": t.user_message[:80],
                    "priority": t.priority.name,
                }
                for t in pending
            ],
        }
