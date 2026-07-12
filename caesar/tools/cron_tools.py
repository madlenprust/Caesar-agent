"""Cron инструменты — постановка задач по расписанию.

Позволяет агенту через tool-call ставить cron задачи:
  "Каждый день в 9:00 делай дайджест новостей"

LLM вызывает cron_add с текстом расписания и описанием задачи.
"""

import asyncio
from typing import Any

from caesar.core.cron import parse_schedule, cron_to_human, CronScheduler
from caesar.logging_setup import get_logger
from caesar.tools.base import Tool, ToolResult


class CronAddTool(Tool):
    """Поставить задачу по расписанию."""
    
    name = "cron_add"
    description = (
        "Поставить задачу по расписанию. "
        "schedule_text — расписание на русском: 'каждый день в 9:00', 'по будням в 18:00', "
        "'каждый понедельник в 10:00', 'каждые 30 минут', 'каждое утро', 'каждый вечер'. "
        "task_description — что делать при срабатывании."
    )
    category = "scheduler"
    parameters_schema = {
        "type": "object",
        "properties": {
            "schedule_text": {
                "type": "string",
                "description": "Расписание: 'каждый день в 9:00', 'по будням в 18:00', 'каждые 30 минут'",
            },
            "task_description": {
                "type": "string",
                "description": "Что делать: 'найди новости про AI и сделай дайджест'",
            },
        },
        "required": ["schedule_text", "task_description"],
    }
    
    # Устанавливается orchestrator-ом через set_context
    default_user_id: str = ""
    default_channel_id: str = ""
    cron_scheduler: CronScheduler | None = None
    
    async def execute(
        self,
        schedule_text: str,
        task_description: str,
        **_,
    ) -> ToolResult:
        if not self.cron_scheduler:
            return ToolResult(
                success=False,
                error="Cron планировщик не настроен. Включи: caesar enable cron",
            )
        
        # Парсим расписание
        parsed = parse_schedule(schedule_text)
        if not parsed:
            return ToolResult(
                success=False,
                error=(
                    f"Не удалось распознать расписание '{schedule_text}'. "
                    f"Примеры: 'каждый день в 9:00', 'по будням в 18:00', "
                    f"'каждый понедельник в 10:00', 'каждые 30 минут', 'каждое утро'"
                ),
            )
        
        cron_expr, human_readable = parsed
        
        try:
            cron_id = await self.cron_scheduler.add_cron_task(
                user_id=self.default_user_id,
                schedule=cron_expr,
                schedule_human=human_readable,
                task_to_execute=task_description,
                channel_id=self.default_channel_id,
            )
            
            return ToolResult(
                success=True,
                data={
                    "cron_id": cron_id,
                    "schedule": cron_expr,
                    "schedule_human": human_readable,
                    "task": task_description,
                    "message": f"Задача поставлена: {human_readable} — '{task_description[:60]}'",
                },
            )
        except Exception as e:
            return ToolResult(success=False, error=str(e))


class CronListTool(Tool):
    """Показать список cron задач пользователя."""
    
    name = "cron_list"
    description = "Показать список задач по расписанию (cron)."
    category = "scheduler"
    parameters_schema = {
        "type": "object",
        "properties": {},
    }
    
    default_user_id: str = ""
    cron_scheduler: CronScheduler | None = None
    
    async def execute(self, **_) -> ToolResult:
        if not self.cron_scheduler:
            return ToolResult(success=False, error="Cron не настроен")
        
        tasks = self.cron_scheduler.list_cron_tasks(self.default_user_id)
        
        return ToolResult(
            success=True,
            data={
                "tasks": [
                    {
                        "id": t["id"],
                        "schedule": t.get("schedule_human") or t["schedule"],
                        "task": t["task_to_execute"],
                        "enabled": bool(t.get("enabled", 1)),
                        "next_run": t.get("next_run_at", ""),
                        "last_run": t.get("last_run_at", ""),
                        "failures": t.get("consecutive_failures", 0),
                    }
                    for t in tasks
                ],
                "total": len(tasks),
            },
        )


class CronRemoveTool(Tool):
    """Удалить cron задачу."""
    
    name = "cron_remove"
    description = "Удалить задачу по расписанию. Нужен cron_id (получи через cron_list)."
    category = "scheduler"
    parameters_schema = {
        "type": "object",
        "properties": {
            "cron_id": {"type": "string", "description": "ID задачи (из cron_list)"},
        },
        "required": ["cron_id"],
    }
    
    cron_scheduler: CronScheduler | None = None
    
    async def execute(self, cron_id: str, **_) -> ToolResult:
        if not self.cron_scheduler:
            return ToolResult(success=False, error="Cron не настроен")
        
        await self.cron_scheduler.remove_cron_task(cron_id)
        
        return ToolResult(
            success=True,
            data={
                "removed": cron_id,
                "message": f"Задача {cron_id} удалена",
            },
        )


def get_cron_tools(cron_scheduler=None) -> list[Tool]:
    """Создать cron инструменты."""
    add_tool = CronAddTool()
    add_tool.cron_scheduler = cron_scheduler
    
    list_tool = CronListTool()
    list_tool.cron_scheduler = cron_scheduler
    
    remove_tool = CronRemoveTool()
    remove_tool.cron_scheduler = cron_scheduler
    
    return [add_tool, list_tool, remove_tool]
