"""Cron-планировщик.

См. roadmap раздел 13.7-13.11.

Использует APScheduler с SQLite jobstore.
Пользователь ставит задачи через разговор с агентом:
  "Каждый день в 9:00 делай дайджест новостей и публикуй в @news"

3 неудачи подряд → отключение.
"""

import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from caesar.config import Config
from caesar.core.queue import TaskQueue, TaskPriority, TaskComplexity
from caesar.logging_setup import get_logger
from caesar.memory.storage import Storage


# Словарь паттернов расписания для русского языка
SCHEDULE_PATTERNS = [
    # "каждый день в 9:00" — 5-field cron: min hour day month weekday
    (re.compile(r"каждый\s+день\s+(?:в\s+)?(\d{1,2})(?::(\d{2}))?", re.IGNORECASE),
     lambda m: (f"{m.group(2) or '0'} {m.group(1)} * * *",
                f"Каждый день в {m.group(1)}:{m.group(2) or '00'}")),
    # "по будням в 9:00"
    (re.compile(r"по\s+будням\s+(?:в\s+)?(\d{1,2})(?::(\d{2}))?", re.IGNORECASE),
     lambda m: (f"{m.group(2) or '0'} {m.group(1)} * * 1-5",
                f"По будням в {m.group(1)}:{m.group(2) or '00'}")),
    # "каждый понедельник в 9:00"
    (re.compile(r"каждый\s+(понедельник|вторник|среду|четверг|пятницу|субботу|воскресенье)\s+(?:в\s+)?(\d{1,2})(?::(\d{2}))?", re.IGNORECASE),
     lambda m: (f"{m.group(3) or '0'} {m.group(2)} * * {_weekday_to_cron(m.group(1))}",
                f"Каждый {m.group(1)} в {m.group(2)}:{m.group(3) or '00'}")),
    # "каждый час"
    (re.compile(r"каждый\s+час", re.IGNORECASE),
     lambda m: ("0 * * * *", "Каждый час")),
    # "каждые N минут"
    (re.compile(r"каждые\s+(\d+)\s+минут", re.IGNORECASE),
     lambda m: (f"*/{m.group(1)} * * * *", f"Каждые {m.group(1)} минут")),
    # "каждые N часов"
    (re.compile(r"каждые\s+(\d+)\s+час", re.IGNORECASE),
     lambda m: (f"0 */{m.group(1)} * * *", f"Каждые {m.group(1)} часов")),
    # "раз в неделю"
    (re.compile(r"раз\s+в\s+неделю", re.IGNORECASE),
     lambda m: ("0 0 * * 0", "Раз в неделю (воскресенье 00:00)")),
    # "каждое утро" / "каждый вечер"
    (re.compile(r"каждое\s+утро", re.IGNORECASE),
     lambda m: ("0 9 * * *", "Каждое утро (09:00)")),
    (re.compile(r"каждый\s+вечер", re.IGNORECASE),
     lambda m: ("0 19 * * *", "Каждый вечер (19:00)")),
]

WEEKDAY_MAP = {
    "понедельник": "1", "вторник": "2", "среду": "3", "четверг": "4",
    "пятницу": "5", "субботу": "6", "воскресенье": "0",
}


def _weekday_to_cron(name: str) -> str:
    return WEEKDAY_MAP.get(name.lower(), "*")


def parse_schedule(text: str) -> tuple[str, str] | None:
    """Распарсить текст в cron-расписание.
    
    Возвращает (cron_expression, human_readable) или None.
    """
    for pattern, builder in SCHEDULE_PATTERNS:
        m = pattern.search(text)
        if m:
            return builder(m)
    return None


def cron_to_human(cron: str) -> str:
    """Конвертировать cron в человекочитаемый текст (грубо)."""
    parts = cron.split()
    if len(parts) != 5:
        return cron
    minute, hour, day, month, weekday = parts
    if weekday == "*":
        if hour == "*":
            return f"Каждый час (в {minute} мин)"
        if day == "*":
            return f"Каждый день в {hour}:{minute.zfill(2)}"
        return f"Каждый месяц в {day} число, {hour}:{minute.zfill(2)}"
    if weekday == "1-5":
        return f"По будням в {hour}:{minute.zfill(2)}"
    return cron


class CronScheduler:
    """Планировщик cron-задач.
    
    Использует APScheduler с SQLite jobstore.
    """
    
    def __init__(self, config: Config, storage: Storage, queue: TaskQueue):
        self.config = config
        self.storage = storage
        self.queue = queue
        self.log = get_logger("cron")
        self._scheduler = None
        self._running = False
    
    async def start(self) -> None:
        try:
            from apscheduler.schedulers.asyncio import AsyncIOScheduler
            from apscheduler.jobstores.memory import MemoryJobStore
            from apscheduler.triggers.cron import CronTrigger
        except ImportError as e:
            self.log.warning(f"APScheduler не установлен ({e}). Cron не активен.")
            self.log.warning("Установи: caesar enable cron")
            return
        
        # Memory jobstore — не требует SQLAlchemy, не сериализует функции.
        # Cron задачи регистрируются при каждом старте daemon, persistence не нужна.
        self._scheduler = AsyncIOScheduler(
            jobstores={"default": MemoryJobStore()},
            timezone=self.config.timezone,
        )
        self._scheduler.start()
        self._running = True
        
        # Загружаем все активные cron_tasks из БД
        await self._load_existing_tasks()
        
        self.log.info("Cron scheduler started")
    
    async def stop(self) -> None:
        if self._scheduler:
            self._scheduler.shutdown(wait=False)
        self._running = False
        self.log.info("Cron scheduler stopped")
    
    async def _load_existing_tasks(self) -> None:
        """Загрузить все активные cron_tasks из БД в планировщик."""
        try:
            with self.storage._conn() as conn:
                rows = conn.execute(
                    "SELECT * FROM cron_tasks WHERE enabled = 1"
                ).fetchall()
        except Exception as e:
            self.log.error(f"Failed to load cron tasks: {e}")
            return
        
        if not rows:
            self.log.info("No active cron tasks to load")
            return
        
        from apscheduler.triggers.cron import CronTrigger
        
        loaded = 0
        for row in rows:
            d = dict(row)
            try:
                trigger = CronTrigger.from_crontab(
                    d["schedule"],
                    timezone=d.get("timezone") or self.config.timezone,
                )
                self._scheduler.add_job(
                    self._fire_cron,
                    trigger=trigger,
                    args=[d["id"]],
                    id=d["id"],
                    replace_existing=True,
                )
                loaded += 1
            except Exception as e:
                self.log.warning(f"Failed to load cron task {d['id']}: {e}")
        
        self.log.info(f"Loaded {loaded}/{len(rows)} cron tasks from DB")
    
    async def add_cron_task(
        self,
        user_id: str,
        schedule: str,
        schedule_human: str,
        task_to_execute: str,
        channel_id: str | None = None,
        timezone: str | None = None,
        notify_on_success: bool = False,
        notify_on_failure: bool = True,
    ) -> str:
        """Добавить cron-задачу.
        
        schedule — cron-формат "0 9 * * *"
        task_to_execute — что делать при срабатывании
        """
        # Считаем next_run_at
        try:
            from apscheduler.triggers.cron import CronTrigger
            trigger = CronTrigger.from_crontab(schedule, timezone=timezone or self.config.timezone)
            next_run = trigger.get_next_fire_time(None, datetime.now(trigger.timezone))
            next_run_at = next_run.isoformat() if next_run else None
        except Exception as e:
            self.log.error(f"Invalid cron schedule '{schedule}': {e}")
            raise ValueError(f"Invalid cron: {e}")
        
        cron_id = self.storage.add_cron_task({
            "user_id": user_id,
            "channel_id": channel_id,
            "schedule": schedule,
            "schedule_human": schedule_human,
            "task_to_execute": task_to_execute,
            "timezone": timezone or self.config.timezone,
            "notify_on_success": int(notify_on_success),
            "notify_on_failure": int(notify_on_failure),
            "next_run_at": next_run_at,
        })
        
        # Добавляем в APScheduler
        if self._scheduler:
            from apscheduler.triggers.cron import CronTrigger
            trigger = CronTrigger.from_crontab(schedule, timezone=timezone or self.config.timezone)
            self._scheduler.add_job(
                self._fire_cron,
                trigger=trigger,
                args=[cron_id],
                id=cron_id,
                replace_existing=True,
            )
            self.log.info(f"Cron task {cron_id} added: '{task_to_execute[:50]}...'")
        
        return cron_id
    
    async def _fire_cron(self, cron_id: str) -> None:
        """Срабатывание cron-задачи — создаёт task в очереди."""
        try:
            await self._fire_cron_impl(cron_id)
        except Exception as e:
            self.log.error(
                f"Cron task {cron_id} failed: {type(e).__name__}: {e}",
                exc_info=True,
            )
    
    async def _fire_cron_impl(self, cron_id: str) -> None:
        """Внутренняя реализация _fire_cron (без try/except — для тестов)."""
        # Получаем cron_task из БД
        with self.storage._conn() as conn:
            row = conn.execute("SELECT * FROM cron_tasks WHERE id = ?", (cron_id,)).fetchone()
        
        if not row:
            self.log.warning(f"Cron task {cron_id} not found in DB")
            return
        
        d = dict(row)
        if not d["enabled"]:
            self.log.info(f"Cron task {cron_id} disabled, skipping")
            return
        
        self.log.info(f"Cron firing: {cron_id} '{d['task_to_execute'][:50]}...'")
        
        # Проверяем quiet hours — если сейчас тихие часы, переносим firing
        # на конец тихих часов (DEFERRED), а не теряем задачу. В конце quiet
        # _fire_cron_impl запустится снова, _is_quiet_hours() уже False → выполнится.
        if self._is_quiet_hours():
            run_at = self._next_quiet_end_datetime()
            self.log.info(f"Cron {cron_id} deferred — quiet hours, rescheduled to {run_at.isoformat()}")
            try:
                if self._scheduler:
                    self._scheduler.add_job(
                        self._fire_cron_impl, "date",
                        args=[cron_id], run_date=run_at,
                        id=f"{cron_id}:deferred", replace_existing=True,
                    )
            except Exception as e:
                self.log.warning(f"Cron {cron_id}: cannot defer ({e}); will fire next cycle")
            return
        
        # ВАЖНО: source_chat_id для задачи должен быть Telegram chat_id
        # (например "208118"), НЕ внутренний channel_id и НЕ CLI session.
        # TG adapter подписан на str(chat_id) — только числовой TG chat_id работает.
        channel_id = d.get("channel_id") or f"channel:{d['user_id']}:main"
        source_chat_id = ""
        try:
            with self.storage._conn() as conn:
                ch_row = conn.execute(
                    "SELECT source_chat_id FROM channels WHERE id = ?",
                    (channel_id,),
                ).fetchone()
                if ch_row and ch_row["source_chat_id"]:
                    candidate = str(ch_row["source_chat_id"])
                    # Проверяем — это TG chat_id (число) или CLI session?
                    if candidate.isdigit():
                        source_chat_id = candidate
                        self.log.info(
                            f"Cron {cron_id}: source_chat_id resolved to "
                            f"{source_chat_id} from channel {channel_id}"
                        )
                    else:
                        self.log.warning(
                            f"Cron {cron_id}: source_chat_id '{candidate}' is not a TG chat_id "
                            f"(channel {channel_id}), trying TG fallback"
                        )
                
                # Если не нашли TG chat_id — fallback: любой telegram channel
                if not source_chat_id:
                    ch_row = conn.execute(
                        """SELECT source_chat_id FROM channels 
                           WHERE source = 'telegram' 
                           AND source_chat_id IS NOT NULL LIMIT 1""",
                    ).fetchone()
                    if ch_row and ch_row["source_chat_id"]:
                        candidate = str(ch_row["source_chat_id"])
                        if candidate.isdigit():
                            source_chat_id = candidate
                            self.log.info(
                                f"Cron {cron_id}: source_chat_id fallback to "
                                f"{source_chat_id} (any TG channel)"
                            )
        except Exception as e:
            self.log.warning(f"Cron {cron_id}: failed to resolve source_chat_id: {e}")
        
        # Создаём задачу в очереди (фоновый пул)
        await self.queue.add_task(
            user_message=d["task_to_execute"],
            user_id=d["user_id"],
            channel_id=channel_id,
            author_id=d["user_id"],
            source="cron",
            source_chat_id=source_chat_id,  # Telegram chat_id, не внутренний
            priority=TaskPriority.NORMAL,
            complexity=TaskComplexity.MEDIUM,
        )
        
        # Считаем next_run_at
        try:
            from apscheduler.triggers.cron import CronTrigger
            trigger = CronTrigger.from_crontab(d["schedule"], timezone=d["timezone"])
            next_run = trigger.get_next_fire_time(None, datetime.now(trigger.timezone))
            next_run_at = next_run.isoformat() if next_run else None
        except Exception:
            next_run_at = None
        
        # Обновляем last_run, next_run
        # (success посчитается когда задача завершится — TODO: подписка на завершение)
        # Пока считаем что успешно запустили
        self.storage.update_cron_run(cron_id, success=True, next_run_at=next_run_at)
    
    async def remove_cron_task(self, cron_id: str) -> None:
        """Удалить cron-задачу."""
        if self._scheduler:
            try:
                self._scheduler.remove_job(cron_id)
            except Exception:
                pass
        with self.storage._conn() as conn:
            conn.execute("DELETE FROM cron_tasks WHERE id = ?", (cron_id,))
    
    async def disable_cron_task(self, cron_id: str) -> None:
        """Отключить cron-задачу."""
        if self._scheduler:
            try:
                self._scheduler.remove_job(cron_id)
            except Exception:
                pass
        self.storage.disable_cron_task(cron_id)
    
    def list_cron_tasks(self, user_id: str, only_enabled: bool = False) -> list[dict]:
        """Список cron-задач пользователя."""
        return self.storage.list_cron_tasks(user_id, only_enabled)
    
    def _is_quiet_hours(self) -> bool:
        """Проверить сейчас ли тихие часы."""
        try:
            from datetime import datetime, time
            start_str = getattr(self.config.cron, "quiet_hours_start", "23:00")
            end_str = getattr(self.config.cron, "quiet_hours_end", "08:00")
            
            start_h, start_m = map(int, start_str.split(":"))
            end_h, end_m = map(int, end_str.split(":"))
            
            now = datetime.now().time()
            start = time(start_h, start_m)
            end = time(end_h, end_m)
            
            if start <= end:
                # Обычный случай: 08:00 - 17:00
                return start <= now <= end
            else:
                # Переход через полночь: 23:00 - 08:00
                return now >= start or now <= end
        except Exception:
            return False

    def _next_quiet_end_datetime(self) -> "datetime":
        """Ближайший момент окончания тихих часов (для deferred-переноса cron).

        Если сегодня end уже прошёл, а мы всё ещё в quiet (cross-midnight:
        сейчас 23:30, end 08:00) — берём завтрашний end.
        """
        from datetime import datetime, timedelta
        end_str = getattr(self.config.cron, "quiet_hours_end", "08:00")
        end_h, end_m = map(int, end_str.split(":"))
        now = datetime.now()
        end_today = now.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
        if end_today <= now:
            return end_today + timedelta(days=1)
        return end_today
    
    async def _hold_notification(self, cron_task: dict) -> None:
        """Сохранить отложенное уведомление для morning briefing."""
        try:
            held_file = self.storage.db_path.parent / "held_notifications.json"
            import json as _json
            
            held = []
            if held_file.exists():
                with open(held_file, "r") as f:
                    held = _json.load(f)
            
            held.append({
                "cron_id": cron_task["id"],
                "task": cron_task["task_to_execute"],
                "held_at": datetime.now().isoformat(),
            })
            
            with open(held_file, "w") as f:
                _json.dump(held, f, ensure_ascii=False, indent=2)
            
            self.log.info(f"Held notification saved: {cron_task['task_to_execute'][:50]}")
        except Exception as e:
            self.log.warning(f"Failed to hold notification: {e}")
