"""Очередь задач.

См. roadmap раздел 12.

3 приоритета:
- high (1): интерактивные задачи от пользователя (TG, CLI)
- normal (2): cron-задачи, webhook
- low (3): self-maintenance

5 интерактивных workers + 10 фоновых. Без прерываний (preemption).
Если все 5 заняты — новая задача ждёт, пишем позицию.

Жизненный цикл: pending → running → completed/failed/waiting_for_user
"""

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from caesar.config import Config
from caesar.logging_setup import get_logger


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_FOR_USER = "waiting_for_user"
    COMPLETED = "completed"
    FAILED = "failed"


class TaskPriority(int, Enum):
    HIGH = 1    # интерактивные от пользователя
    NORMAL = 2  # cron, webhook
    LOW = 3     # self-maintenance


class TaskComplexity(str, Enum):
    SIMPLE = "simple"
    MEDIUM = "medium"
    COMPLEX = "complex"


@dataclass
class Task:
    """Задача в очереди."""
    id: str = field(default_factory=lambda: f"task-{uuid.uuid4().hex[:12]}")
    user_id: str = ""
    channel_id: str = ""
    author_id: str = ""
    
    source: str = ""  # "telegram" | "cli" | "cron" | "webhook"
    source_chat_id: str = ""
    
    user_message: str = ""
    original_directive: str = ""  # изначальная постановка задачи (для self-check)
    
    status: TaskStatus = TaskStatus.PENDING
    priority: TaskPriority = TaskPriority.HIGH
    complexity: TaskComplexity = TaskComplexity.SIMPLE
    
    created_at: datetime = field(default_factory=datetime.now)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    
    worker_id: str | None = None
    
    plan: dict | None = None
    current_step: int = 0
    
    tokens_used: int = 0
    cost_rub: float = 0.0
    
    result: str | None = None
    error: str | None = None
    
    retry_count: int = 0
    
    waiting_question: str | None = None
    waiting_since: datetime | None = None


class TaskQueue:
    """Очередь задач с двумя пулами workers.
    
    Интерактивный пул (5 workers) — для задач от пользователя.
    Фоновый пул (10 workers) — для cron, self-maintenance, долгих задач.
    """
    
    def __init__(self, config: Config):
        self.config = config
        self.log = get_logger("queue")
        
        # Задачи в RAM (также дублируются в SQLite для персистентности)
        self._tasks: dict[str, Task] = {}
        
        # Две очереди: интерактивная и фоновая
        self._interactive_queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._background_queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        
        # Workers
        self._interactive_workers: list[asyncio.Task] = []
        self._background_workers: list[asyncio.Task] = []
        
        # Свободные workers
        self._interactive_free = config.queue.max_interactive_workers
        self._background_free = config.queue.max_background_workers
        
        # Callback когда worker берёт задачу (для оркестратора)
        self._task_handler: Any = None
        
        self._running = False
    
    def set_task_handler(self, handler) -> None:
        """Установить обработчик задач (обычно оркестратор)."""
        self._task_handler = handler
    
    def set_storage(self, storage) -> None:
        """Установить storage для persistence задач."""
        self._storage = storage
    
    async def start(self) -> None:
        """Запустить workers и восстановить незавершённые задачи."""
        self._running = True
        
        # Restore unfinished tasks from previous run (if storage is set)
        if hasattr(self, '_storage') and self._storage:
            await self._restore_persisted_tasks()
        
        # Интерактивные workers
        for i in range(self.config.queue.max_interactive_workers):
            worker = asyncio.create_task(self._interactive_worker_loop(i))
            self._interactive_workers.append(worker)
        
        # Фоновые workers
        for i in range(self.config.queue.max_background_workers):
            worker = asyncio.create_task(self._background_worker_loop(i))
            self._background_workers.append(worker)
        
        self.log.info(
            f"Queue started: "
            f"{self.config.queue.max_interactive_workers} interactive + "
            f"{self.config.queue.max_background_workers} background workers"
        )
    
    async def stop(self, timeout: float = 180.0) -> None:
        """Остановить workers gracefully.
        
        НЕ отменяем workers сразу — даём им завершить текущие задачи.
        Если task handler в середине LLM-вызова или tool execution,
        задача завершится нормально (ответ дойдёт до пользователя).
        
        Args:
            timeout: сколько ждать завершения активных задач (секунды).
                     После timeout — force cancel.
        """
        self._running = False
        
        active = self.list_active_tasks()
        if active:
            self.log.info(
                f"Graceful shutdown: waiting for {len(active)} active task(s) "
                f"(timeout={timeout}s)..."
            )
            for t in active:
                self.log.info(
                    f"  - {t.id} [{t.status.value}]: {t.user_message[:60]}... "
                    f"(step {t.current_step})"
                )
        
        # Put sentinel values to wake workers blocked on queue.get()
        # Workers see task_id=None and exit cleanly
        for _ in range(self.config.queue.max_interactive_workers):
            await self._interactive_queue.put((float('inf'), float('inf'), None))
        for _ in range(self.config.queue.max_background_workers):
            await self._background_queue.put((float('inf'), float('inf'), None))
        
        all_workers = self._interactive_workers + self._background_workers
        if all_workers:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*all_workers, return_exceptions=True),
                    timeout=timeout,
                )
                self.log.info("All workers stopped gracefully")
            except asyncio.TimeoutError:
                # Some tasks didn't finish in time — force cancel
                self.log.warning(
                    f"Timeout after {timeout}s — force cancelling "
                    f"{len([w for w in all_workers if not w.done()])} worker(s)"
                )
                for worker in all_workers:
                    if not worker.done():
                        worker.cancel()
                await asyncio.gather(*all_workers, return_exceptions=True)
        
        self.log.info("Queue stopped")
    
    async def add_task(
        self,
        user_message: str,
        user_id: str,
        channel_id: str,
        author_id: str = "",
        source: str = "cli",
        source_chat_id: str = "",
        priority: TaskPriority = TaskPriority.HIGH,
        complexity: TaskComplexity = TaskComplexity.SIMPLE,
        original_directive: str = "",
    ) -> Task:
        """Добавить задачу в очередь."""
        task = Task(
            user_message=user_message,
            user_id=user_id,
            channel_id=channel_id,
            author_id=author_id or user_id,
            source=source,
            source_chat_id=source_chat_id,
            priority=priority,
            complexity=complexity,
            original_directive=original_directive or user_message,
        )
        
        self._tasks[task.id] = task
        
        # В какую очередь?
        # Long-running (>5 мин) → фоновая
        # Иначе → интерактивная
        if complexity == TaskComplexity.COMPLEX:
            queue = self._background_queue
            pool = "background"
        else:
            queue = self._interactive_queue
            pool = "interactive"
        
        # PriorityQueue сортирует по (priority, created_at)
        await queue.put((
            task.priority.value,
            task.created_at.timestamp(),
            task.id,
        ))
        
        self.log.info(
            f"Task {task.id} added to {pool} queue: "
            f"'{user_message[:50]}...' priority={priority.name}"
        )
        
        return task
    
    async def _interactive_worker_loop(self, worker_id: int) -> None:
        """Цикл интерактивного worker."""
        await self._worker_loop(worker_id, "interactive", self._interactive_queue)
    
    async def _background_worker_loop(self, worker_id: int) -> None:
        """Цикл фонового worker."""
        await self._worker_loop(worker_id, "background", self._background_queue)
    
    async def _worker_loop(
        self,
        worker_id: int,
        pool: str,
        queue: asyncio.PriorityQueue,
    ) -> None:
        """Общий цикл worker."""
        while self._running:
            try:
                # Ждём задачу
                priority, created_ts, task_id = await queue.get()
                
                # Sentinel for shutdown — wake up from queue.get()
                if task_id is None:
                    queue.task_done()
                    break
                
                task = self._tasks.get(task_id)
                if task is None:
                    self.log.warning(f"Task {task_id} not found, skipping")
                    queue.task_done()
                    continue
                
                # Обновляем статус
                task.status = TaskStatus.RUNNING
                task.started_at = datetime.now()
                task.worker_id = f"{pool}-{worker_id}"
                
                # Декрементируем свободные workers
                if pool == "interactive":
                    self._interactive_free = max(0, self._interactive_free - 1)
                else:
                    self._background_free = max(0, self._background_free - 1)
                
                self.log.info(
                    f"Worker {pool}-{worker_id} picked task {task_id}: "
                    f"'{task.user_message[:50]}...'"
                )
                
                # Выполняем через оркестратор
                if self._task_handler:
                    try:
                        await self._task_handler(task)
                    except Exception as e:
                        self.log.exception(f"Task {task_id} failed: {e}")
                        task.status = TaskStatus.FAILED
                        task.error = str(e)
                        task.completed_at = datetime.now()
                
                # Отмечаем завершение
                if task.status == TaskStatus.RUNNING:
                    task.status = TaskStatus.COMPLETED
                    task.completed_at = datetime.now()
                
                # Инкрементируем свободные workers
                if pool == "interactive":
                    self._interactive_free += 1
                else:
                    self._background_free += 1
                
                queue.task_done()
                
                # Очищаем старые завершённые задачи (оставляем последние 100)
                if len(self._tasks) > 100:
                    completed = [
                        tid for tid, t in self._tasks.items()
                        if t.status in (TaskStatus.COMPLETED, TaskStatus.FAILED)
                    ]
                    for tid in completed[:-50]:  # оставляем 50 последних
                        del self._tasks[tid]
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.log.exception(f"Worker {pool}-{worker_id} error: {e}")
    
    def get_task(self, task_id: str) -> Task | None:
        """Получить задачу по ID."""
        return self._tasks.get(task_id)
    
    def get_pending_count(self, pool: str = "interactive") -> int:
        """Сколько задач ждёт в очереди."""
        if pool == "interactive":
            return self._interactive_queue.qsize()
        return self._background_queue.qsize()
    
    def get_active_count(self, pool: str = "interactive") -> int:
        """Сколько задач сейчас выполняется."""
        if pool == "interactive":
            return (
                self.config.queue.max_interactive_workers
                - self._interactive_free
            )
        return (
            self.config.queue.max_background_workers
            - self._background_free
        )
    
    def list_active_tasks(self) -> list[Task]:
        """Список активных задач."""
        return [
            task for task in self._tasks.values()
            if task.status in (TaskStatus.RUNNING, TaskStatus.WAITING_FOR_USER)
        ]
    
    def list_pending_tasks(self) -> list[Task]:
        """Список задач в очереди."""
        return [
            task for task in self._tasks.values()
            if task.status == TaskStatus.PENDING
        ]
    
    # === Persistence (graceful restart) ===
    
    async def _restore_persisted_tasks(self) -> None:
        """Восстановить незавершённые задачи из БД после рестарта daemon.
        
        Задачи со статусом 'running' или 'waiting_for_user' были прерваны
        рестартом — пере-добавляем их в очередь (они начнутся заново).
        Задачи со статусом 'pending' — просто пере-добавляем.
        """
        try:
            unfinished = self._storage.get_unfinished_tasks()
        except Exception as e:
            self.log.error(f"Failed to load unfinished tasks: {e}")
            return
        
        if not unfinished:
            return
        
        self.log.info(f"Restoring {len(unfinished)} unfinished task(s) from DB...")
        
        for row in unfinished:
            # Конвертием SQLite row в Task
            try:
                task = Task(
                    id=row["id"],
                    user_id=row["user_id"],
                    channel_id=row["channel_id"],
                    author_id=row.get("author_id", "") or "",
                    source=row.get("source", "") or "",
                    source_chat_id=row.get("source_chat_id", "") or "",
                    user_message=row["user_message"],
                    original_directive=row.get("original_directive", "") or row["user_message"],
                    status=TaskStatus.PENDING,  # сбрасываем в pending
                    priority=TaskPriority(row.get("priority", 2)),
                    complexity=TaskComplexity(row.get("complexity", "simple")),
                )
                # Восстанавливаем поля которые были до рестарта
                task.current_step = row.get("current_step", 0) or 0
                task.tokens_used = row.get("tokens_used", 0) or 0
                task.cost_rub = row.get("cost_rub", 0.0) or 0.0
                task.retry_count = row.get("retry_count", 0) or 0
                task.worker_id = row.get("worker_id")
                
                # Если была 'running' — это прерванная задача, рестартуем
                old_status = row.get("status", "pending")
                self.log.info(
                    f"  Restoring {task.id} (was {old_status}, step {task.current_step}, "
                    f"tokens {task.tokens_used}): "
                    f"'{task.user_message[:60]}...'"
                )
                
                # Добавляем в _tasks и в очередь
                self._tasks[task.id] = task
                
                if task.complexity == TaskComplexity.COMPLEX:
                    queue = self._background_queue
                    pool = "background"
                else:
                    queue = self._interactive_queue
                    pool = "interactive"
                
                # Используем оригинальный created_at для сохранения порядка
                created_ts = row.get("created_at")
                if isinstance(created_ts, str):
                    from datetime import datetime as dt
                    try:
                        created_ts = dt.fromisoformat(created_ts.replace("Z", "")).timestamp()
                    except Exception:
                        created_ts = task.created_at.timestamp()
                else:
                    created_ts = task.created_at.timestamp()
                
                await queue.put((
                    task.priority.value,
                    created_ts,
                    task.id,
                ))
                
            except Exception as e:
                self.log.error(f"Failed to restore task {row.get('id', '?')}: {e}")
        
        # Чистим из БД — они теперь в RAM, будут сохранены при следующем shutdown
        try:
            self._storage.clear_unfinished_tasks()
        except Exception:
            pass
        
        self.log.info(f"Restored {len(unfinished)} task(s), re-queued for execution")
    
    def persist_unfinished_tasks(self) -> int:
        """Сохранить незавершённые задачи в БД перед shutdown.
        
        Вызывается из daemon.stop() ПЕРЕД queue.stop() — чтобы задачи
        сохранились даже если graceful shutdown не успел их завершить.
        
        Returns: count сохранённых задач.
        """
        if not hasattr(self, '_storage') or not self._storage:
            return 0
        
        unfinished = [
            t for t in self._tasks.values()
            if t.status in (TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.WAITING_FOR_USER)
        ]
        
        if not unfinished:
            return 0
        
        saved = 0
        for task in unfinished:
            try:
                self._storage.save_task({
                    "id": task.id,
                    "user_id": task.user_id,
                    "channel_id": task.channel_id,
                    "author_id": task.author_id,
                    "source": task.source,
                    "source_chat_id": task.source_chat_id,
                    "user_message": task.user_message,
                    "original_directive": task.original_directive,
                    "status": task.status.value if hasattr(task.status, 'value') else str(task.status),
                    "priority": task.priority.value,
                    "complexity": task.complexity.value if hasattr(task.complexity, 'value') else str(task.complexity),
                    "created_at": task.created_at.isoformat() if task.created_at else None,
                    "started_at": task.started_at.isoformat() if task.started_at else None,
                    "completed_at": None,  # не завершена
                    "worker_id": task.worker_id,
                    "current_step": task.current_step,
                    "tokens_used": task.tokens_used,
                    "cost_rub": task.cost_rub,
                    "result": None,  # не завершена
                    "error": None,
                    "retry_count": task.retry_count,
                    "waiting_question": task.waiting_question,
                    "waiting_since": task.waiting_since.isoformat() if task.waiting_since else None,
                })
                saved += 1
            except Exception as e:
                self.log.error(f"Failed to persist task {task.id}: {e}")
        
        self.log.info(f"Persisted {saved}/{len(unfinished)} unfinished task(s) to DB")
        return saved
