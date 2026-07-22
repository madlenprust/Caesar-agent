"""Шина событий — ядро эмитит нейтральные events, адаптеры рендерят.

См. roadmap раздел 13.3 (capability-based rendering).

Ядро НЕ знает про Telegram, CLI, web. Оно эмитит абстрактные events:
- ProgressStart(task_id)
- ProgressUpdate(task_id, icon)
- AnswerReady(task_id, content)
- QuestionAsked(task_id, question, options)
- FileReady(task_id, path)
- ErrorOccurred(task_id, message)
- InfoNotification(message)
- WarningNotification(message)

Адаптеры каналов подписываются на эти events и рендерят каждый по-своему.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Awaitable


@dataclass
class Event:
    """Базовый event."""
    type: str
    task_id: str | None = None
    timestamp: datetime = field(default_factory=datetime.now)
    data: dict = field(default_factory=dict)


# Типы events (см. roadmap раздел 13.3)
EVENT_PROGRESS_START = "progress_start"
EVENT_PROGRESS_UPDATE = "progress_update"
EVENT_ANSWER_READY = "answer_ready"
EVENT_QUESTION_ASKED = "question_asked"
EVENT_FILE_READY = "file_ready"
EVENT_ERROR_OCCURRED = "error_occurred"
EVENT_INFO_NOTIFICATION = "info_notification"
EVENT_WARNING_NOTIFICATION = "warning_notification"


# Удобные конструкторы
def progress_start(task_id: str) -> Event:
    return Event(type=EVENT_PROGRESS_START, task_id=task_id)

def progress_update(task_id: str, icon: str) -> Event:
    return Event(type=EVENT_PROGRESS_UPDATE, task_id=task_id, data={"icon": icon})

def answer_ready(task_id: str, content: str, format: str = "markdown") -> Event:
    return Event(
        type=EVENT_ANSWER_READY,
        task_id=task_id,
        data={"content": content, "format": format},
    )

def question_asked(
    task_id: str,
    question: str,
    options: list[dict] | None = None,
) -> Event:
    return Event(
        type=EVENT_QUESTION_ASKED,
        task_id=task_id,
        data={"question": question, "options": options or []},
    )

def file_ready(task_id: str, path: str, description: str = "") -> Event:
    return Event(
        type=EVENT_FILE_READY,
        task_id=task_id,
        data={"path": path, "description": description},
    )

def error_occurred(task_id: str | None, message: str) -> Event:
    return Event(
        type=EVENT_ERROR_OCCURRED,
        task_id=task_id,
        data={"message": message},
    )

def info_notification(message: str) -> Event:
    return Event(type=EVENT_INFO_NOTIFICATION, data={"message": message})

def warning_notification(message: str) -> Event:
    return Event(type=EVENT_WARNING_NOTIFICATION, data={"message": message})


# Тип обработчика events
EventHandler = Callable[[Event], Awaitable[None]]


class EventBus:
    """Шина событий.
    
    Адаптеры каналов подписываются на events определённого channel_id
    (конкретного пользователя/сессии). Ядро эмитит events для конкретного
    channel_id, шина маршрутизирует их подписчикам.
    """
    
    def __init__(self):
        self._subscribers: dict[str, list[EventHandler]] = {}
        self._log_subscribers: list[EventHandler] = []  # глобальные (для логирования)
    
    def subscribe(self, channel_id: str, handler: EventHandler) -> None:
        """Подписать обработчик на events конкретного канала."""
        self._subscribers.setdefault(channel_id, []).append(handler)
    
    def unsubscribe(self, channel_id: str, handler: EventHandler) -> None:
        """Отписать обработчик."""
        if channel_id in self._subscribers:
            try:
                self._subscribers[channel_id].remove(handler)
            except ValueError:
                pass  # handler не в списке
    
    def subscribe_global(self, handler: EventHandler) -> None:
        """Подписать обработчик на ВСЕ events (для логирования, аудита)."""
        self._log_subscribers.append(handler)
    
    async def emit(self, channel_id: str, event: Event) -> None:
        """Эмитить event для конкретного канала.
        
        Все подписчики этого канала получат event.
        Также все глобальные подписчики получат event.
        """
        # Сначала глобальные (логирование и т.д.)
        # Копируем список: unsubscribe во время await может мутировать
        # итерируемый список → гонка (RuntimeError/пропуск обработчиков).
        for handler in list(self._log_subscribers):
            try:
                await handler(event)
            except Exception:
                pass  # не падаем из-за логгера

        # Потом подписчики канала — копируем по той же причине
        handlers = list(self._subscribers.get(channel_id, []))
        for handler in handlers:
            try:
                await handler(event)
            except Exception:
                pass  # один упавший обработчик не должен ломать другие
