"""Morning Briefing — утренний дайджест.

Собирает информацию за ночь и отправляет пользователю:
1. Held notifications — cron задачи которые были отложены (quiet hours)
2. Dream cycle results — что добавлено/обогащено/объединено
3. Cron failures — задачи которые упали за ночь
4. Token usage summary — сколько токенов потрачено вчера
5. KG stats — новые entities/relations

Отправляется каждый день в 09:00 (configurable) через cron.
"""

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from caesar.config import Config
from caesar.core.events import EventBus, Event, info_notification
from caesar.logging_setup import get_logger
from caesar.memory.storage import Storage


class MorningBriefing:
    """Утренний дайджест."""
    
    def __init__(
        self,
        config: Config,
        storage: Storage,
        event_bus: EventBus | None = None,
    ):
        self.config = config
        self.storage = storage
        self.event_bus = event_bus
        self.log = get_logger("briefing")
    
    async def generate_and_send(
        self,
        user_id: str = "",
        channel_id: str = "",
    ) -> str:
        """Сгенерировать и отправить утренний дайджест.
        
        Returns: текст дайджеста.
        """
        self.log.info(f"🌅 Generating morning briefing for user={user_id or 'all'}")
        
        sections = []
        
        # 1. Held notifications
        held = self._collect_held_notifications()
        if held:
            sections.append(self._format_held(held))
        
        # 2. Dream cycle results
        dream_report = self._collect_dream_report()
        if dream_report:
            sections.append(dream_report)
        
        # 3. Cron failures
        failures = self._collect_cron_failures()
        if failures:
            sections.append(self._format_failures(failures))
        
        # 4. Token usage summary (вчера)
        tokens = self._collect_token_usage()
        if tokens:
            sections.append(tokens)
        
        # 5. KG stats
        kg_stats = self._collect_kg_stats(user_id)
        if kg_stats:
            sections.append(kg_stats)
        
        # 6. Active cron tasks (напоминание)
        cron_tasks = self._collect_cron_tasks(user_id)
        if cron_tasks:
            sections.append(cron_tasks)

        # 7. (T1) Решения/победы/инциденты за неделю — из категоризировованных L2-фактов.
        events = self._collect_recent_events(user_id)
        if events:
            sections.append(events)

        # Формируем итоговый текст
        if not sections:
            briefing = (
                "🌅 Доброе утро!\n\n"
                "За ночь ничего нового не произошло. "
                "Cron задач не было, dream cycle ничего не нашёл.\n\n"
                "Хорошего дня!"
            )
        else:
            briefing = "🌅 Доброе утро!\n\n" + "\n\n".join(sections) + "\n\nХорошего дня!"
        
        self.log.info(f"Morning briefing generated ({len(briefing)} chars)")
        
        # Отправляем через event_bus если есть
        if self.event_bus and channel_id:
            await self.event_bus.emit(
                channel_id,
                info_notification(briefing),
            )
        
        return briefing
    
    def _collect_held_notifications(self) -> list[dict]:
        """Собрать отложенные уведомления (quiet hours)."""
        held_file = self.storage.db_path.parent / "held_notifications.json"
        
        if not held_file.exists():
            return []
        
        try:
            with open(held_file, "r", encoding="utf-8") as f:
                held = json.load(f)
            
            # Очищаем файл после чтения
            with open(held_file, "w", encoding="utf-8") as f:
                json.dump([], f)
            
            return held
        except Exception as e:
            self.log.warning(f"Failed to read held notifications: {e}")
            return []
    
    def _format_held(self, held: list[dict]) -> str:
        """Форматировать отложенные уведомления."""
        lines = [f"📥 Отложенные уведомления ({len(held)}):"]
        for h in held[:10]:  # максимум 10
            task = h.get("task", "?")[:80]
            held_at = h.get("held_at", "")[:16]
            lines.append(f"  • {task} (отложено {held_at})")
        if len(held) > 10:
            lines.append(f"  ... и ещё {len(held) - 10}")
        return "\n".join(lines)
    
    def _collect_dream_report(self) -> str | None:
        """Собрать отчёт dream cycle если он запускался."""
        dream_file = self.storage.db_path.parent / "last_dream_report.json"
        
        if not dream_file.exists():
            return None
        
        try:
            with open(dream_file, "r", encoding="utf-8") as f:
                report = json.load(f)
            
            # Проверяем что отчёт за сегодняшнюю ночь
            report_time = report.get("timestamp", "")
            if report_time:
                report_dt = datetime.fromisoformat(report_time)
                now = datetime.now()
                if (now - report_dt).total_seconds() > 12 * 3600:  # > 12 часов
                    return None  # старый отчёт
            
            lines = ["🌙 Dream Cycle (за ночь):"]
            if report.get("entities_extracted", 0) > 0:
                lines.append(f"  Новых сущностей: {report['entities_extracted']}")
            if report.get("entities_enriched", 0) > 0:
                lines.append(f"  Дополнено: {report['entities_enriched']}")
            if report.get("duplicates_merged", 0) > 0:
                lines.append(f"  Дубликатов объединено: {report['duplicates_merged']}")
            if report.get("citations_fixed", 0) > 0:
                lines.append(f"  Цитат исправлено: {report['citations_fixed']}")
            
            if len(lines) == 1:
                return None  # ничего не произошло
            
            duration = report.get("duration_sec", 0)
            lines.append(f"  Время: {duration:.0f} сек")
            
            return "\n".join(lines)
        except Exception as e:
            self.log.warning(f"Failed to read dream report: {e}")
            return None
    
    def _collect_cron_failures(self) -> list[dict]:
        """Собрать cron задачи которые упали за последние 12 часов."""
        try:
            with self.storage._conn() as conn:
                rows = conn.execute(
                    """SELECT id, schedule_human, task_to_execute, last_run_at,
                              consecutive_failures
                       FROM cron_tasks
                       WHERE consecutive_failures > 0
                       AND last_run_at > datetime('now', '-12 hours')
                       ORDER BY last_run_at DESC
                       LIMIT 10"""
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []
    
    def _format_failures(self, failures: list[dict]) -> str:
        """Форматировать cron failures."""
        lines = [f"⚠️ Cron задачи с ошибками ({len(failures)}):"]
        for f in failures:
            task = f.get("task_to_execute", "?")[:60]
            fails = f.get("consecutive_failures", 0)
            lines.append(f"  • {task} — {fails} неудач подряд")
        return "\n".join(lines)

    def _collect_recent_events(self, user_id: str) -> str | None:
        """(T1) Решения/победы/инциденты за последние 7 дней (из категоризованных L2)."""
        try:
            with self.storage._conn() as conn:
                rows = conn.execute(
                    """SELECT entity, attribute, value, category, valid_from
                       FROM l2_facts
                       WHERE user_id = ? AND category IN ('decision','win','incident')
                         AND valid_from > datetime('now','-7 days')
                         AND valid_until IS NULL
                       ORDER BY valid_from DESC LIMIT 20""",
                    (user_id,),
                ).fetchall()
            if not rows:
                return None
            return self._format_recent_events([dict(r) for r in rows])
        except Exception as e:
            self.log.warning(f"Failed to collect recent events: {e}")
            return None

    def _format_recent_events(self, events: list[dict]) -> str:
        """Сгруппировать события по категории и отформатировать."""
        labels = {"decision": "Решения", "win": "Победы", "incident": "Инциденты"}
        groups: dict[str, list[dict]] = {"decision": [], "win": [], "incident": []}
        for e in events:
            cat = e.get("category", "fact")
            if cat in groups:
                groups[cat].append(e)
        parts: list[str] = []
        for cat in ("decision", "win", "incident"):
            items = groups[cat]
            if not items:
                continue
            parts.append(f"{labels[cat]} ({len(items)}):")
            for e in items[:8]:
                txt = f"{e.get('entity','?')} — {e.get('attribute','?')}: {e.get('value','')}"[:90]
                parts.append(f"  • {txt}")
            if len(items) > 8:
                parts.append(f"  … и ещё {len(items) - 8}")
        return "\n".join(parts)

    def _collect_token_usage(self) -> str | None:
        """Собрать статистику токенов за вчера."""
        try:
            with self.storage._conn() as conn:
                # Вчера
                row = conn.execute(
                    """SELECT 
                        SUM(prompt_tokens) as prompt,
                        SUM(completion_tokens) as completion,
                        SUM(total_tokens) as total,
                        COUNT(*) as calls
                       FROM token_usage
                       WHERE timestamp >= datetime('now', '-1 day', 'start of day')
                       AND timestamp < datetime('now', 'start of day')
                    """
                ).fetchone()
                
                if not row or not row["total"]:
                    # Может сегодня уже есть
                    row = conn.execute(
                        """SELECT 
                            SUM(prompt_tokens) as prompt,
                            SUM(completion_tokens) as completion,
                            SUM(total_tokens) as total,
                            COUNT(*) as calls
                           FROM token_usage
                           WHERE timestamp >= datetime('now', 'start of day')
                        """
                    ).fetchone()
                
                if not row or not row["total"]:
                    return None
                
                total = row["total"]
                calls = row["calls"]
                prompt = row["prompt"] or 0
                completion = row["completion"] or 0
                
                lines = [f"📊 Токены за последний день:"]
                lines.append(f"  Всего: {total:,} ({calls} вызовов)")
                lines.append(f"  Prompt: {prompt:,} / Completion: {completion:,}")
                
                return "\n".join(lines)
        except Exception as e:
            self.log.debug(f"Token usage collection failed: {e}")
            return None
    
    def _collect_kg_stats(self, user_id: str) -> str | None:
        """Собрать статистику Knowledge Graph."""
        try:
            with self.storage._conn() as conn:
                if user_id:
                    ent_row = conn.execute(
                        "SELECT COUNT(*) as cnt FROM kg_entities WHERE user_id = ?",
                        (user_id,),
                    ).fetchone()
                    rel_row = conn.execute(
                        "SELECT COUNT(*) as cnt FROM kg_relations WHERE user_id = ?",
                        (user_id,),
                    ).fetchone()
                else:
                    ent_row = conn.execute(
                        "SELECT COUNT(*) as cnt FROM kg_entities"
                    ).fetchone()
                    rel_row = conn.execute(
                        "SELECT COUNT(*) as cnt FROM kg_relations"
                    ).fetchone()
                
                entities = ent_row["cnt"] if ent_row else 0
                relations = rel_row["cnt"] if rel_row else 0
                
                if entities == 0 and relations == 0:
                    return None
                
                lines = [f"🧠 Knowledge Graph:"]
                lines.append(f"  Сущностей: {entities}")
                lines.append(f"  Связей: {relations}")
                
                # Новые за последние 24 часа
                if user_id:
                    new_row = conn.execute(
                        """SELECT COUNT(*) as cnt FROM kg_entities 
                           WHERE user_id = ? AND first_seen > datetime('now', '-1 day')""",
                        (user_id,),
                    ).fetchone()
                else:
                    new_row = conn.execute(
                        """SELECT COUNT(*) as cnt FROM kg_entities 
                           WHERE first_seen > datetime('now', '-1 day')"""
                    ).fetchone()
                
                new_entities = new_row["cnt"] if new_row else 0
                if new_entities > 0:
                    lines.append(f"  Новых за сутки: {new_entities}")
                
                return "\n".join(lines)
        except Exception:
            return None
    
    def _collect_cron_tasks(self, user_id: str) -> str | None:
        """Собрать активные cron задачи (напоминание)."""
        try:
            with self.storage._conn() as conn:
                if user_id:
                    rows = conn.execute(
                        """SELECT schedule_human, task_to_execute, next_run_at
                           FROM cron_tasks WHERE enabled = 1 AND user_id = ?
                           ORDER BY next_run_at ASC LIMIT 5""",
                        (user_id,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """SELECT schedule_human, task_to_execute, next_run_at
                           FROM cron_tasks WHERE enabled = 1
                           ORDER BY next_run_at ASC LIMIT 5"""
                    ).fetchall()
                
                if not rows:
                    return None
                
                lines = ["⏰ Активные cron задачи:"]
                for r in rows:
                    schedule = r["schedule_human"] or "?"
                    task = r["task_to_execute"][:50]
                    next_run = r["next_run_at"][:16] if r["next_run_at"] else "?"
                    lines.append(f"  • {schedule}: {task}")
                    lines.append(f"    Следующий: {next_run}")
                
                return "\n".join(lines)
        except Exception:
            return None
