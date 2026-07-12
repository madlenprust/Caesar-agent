"""Главный daemon агента.

Работает 24/7 через systemd. Слушает unix socket для CLI-клиентов
и (опционально) Telegram через Bot API.
"""

import asyncio
import json
import signal
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from caesar.config import Config, SOCKET_PATH, IS_DEV
from caesar.logging_setup import setup_logging, get_logger
from caesar.core.events import EventBus, Event
from caesar.core.queue import TaskQueue
from caesar.core.orchestrator import Orchestrator
from caesar.core.llm import LLMRouter
from caesar.memory.storage import Storage
from caesar.memory.l3 import L3Memory
from caesar.memory.l4 import L4Skills
from caesar.memory.knowledge_graph import KnowledgeGraph
from caesar.tools import ToolRegistry
from caesar.channels.cli_adapter import CLIAdapter
from caesar.channels.telegram_adapter import TelegramAdapter


class AgentDaemon:
    """Главный процесс агента."""
    
    def __init__(self, config: Config):
        self.config = config
        self.log = get_logger("daemon")
        
        self.event_bus = EventBus()
        self.queue = TaskQueue(config)
        
        # Хранилище
        self.storage = Storage()
        
        # Память
        l3_model_key = getattr(config.l3, "model", "multilingual-minilm") if hasattr(config, "l3") else "multilingual-minilm"
        self.l3 = L3Memory(self.storage, model_key=l3_model_key)
        self.l4 = L4Skills(self.storage)
        self.kg = KnowledgeGraph(self.storage)
        
        # LLM
        self.llm = LLMRouter(config)
        
        # Инструменты
        access_mode = config.mode if config.mode != "auto" else "sandboxed"
        self.tools = ToolRegistry(
            storage=self.storage,
            l3_memory=self.l3,
            l4_skills=self.l4,
            access_mode=access_mode,
        )
        
        # Оркестратор
        self.orchestrator = Orchestrator(
            config=config,
            event_bus=self.event_bus,
            storage=self.storage,
            llm_router=self.llm,
            tool_registry=self.tools,
        )
        self.orchestrator._l3 = self.l3  # для сохранения в L3
        self.orchestrator._l4 = self.l4  # для поиска скиллов
        self.orchestrator._kg = self.kg  # knowledge graph
        
        # SkillExecutor — применяет скиллы (exact_recipe) без LLM
        from caesar.core.skill_executor import SkillExecutor
        self.orchestrator._skill_executor = SkillExecutor(
            l4_skills=self.l4,
            llm_router=self.llm,
            tool_registry=self.tools,
        )
        
        # Связываем queue → orchestrator
        self.queue.set_task_handler(self.orchestrator.handle_task)
        # Связываем queue → storage (для persistence задач при рестарте)
        self.queue.set_storage(self.storage)
        
        # Адаптеры каналов
        self.cli_adapter = CLIAdapter(
            config, self.event_bus, self.queue,
            storage=self.storage,
            daemon=self,
        )
        self.telegram_adapter = TelegramAdapter(config, self.event_bus, self.queue, storage=self.storage)
        
        # Cron планировщик
        from caesar.core.cron import CronScheduler
        self.cron = CronScheduler(config, self.storage, self.queue)
        
        # Передаём cron_scheduler в cron tools
        for tool in self.tools._tools.values():
            if hasattr(tool, "cron_scheduler"):
                tool.cron_scheduler = self.cron
        
        self._server: asyncio.AbstractServer | None = None
        self._running = False
    
    async def start(self) -> None:
        self.log.info(f"Agent daemon starting (dev={IS_DEV})")
        self.log.info(f"Socket: {SOCKET_PATH}")
        
        # Записываем время старта для status report (uptime)
        from datetime import datetime as _dt
        self._start_time = _dt.now()
        
        SOCKET_PATH.parent.mkdir(parents=True, exist_ok=True)
        if SOCKET_PATH.exists():
            SOCKET_PATH.unlink()
        
        self._server = await asyncio.start_unix_server(
            self._handle_cli_client,
            path=str(SOCKET_PATH),
        )
        SOCKET_PATH.chmod(0o660)
        
        await self.cli_adapter.start()
        await self.telegram_adapter.start()
        await self.queue.start()
        await self.orchestrator.start()
        await self.cron.start()
        
        # Auto-register dream cycle и morning briefing как cron задачи
        await self._register_auto_cron()
        
        self._running = True
        self.log.info(f"Agent daemon ready (tools: {len(self.tools.list_names())})")
        
        try:
            from systemd import daemon as sd
            sd.notify("READY=1")
        except ImportError:
            pass
        
        # Проверяем — это рестарт (не первый старт)?
        # Если да — отправляем 'готово' в TG кому писал пользователь до рестарта.
        await self._notify_restart_complete()
    
    async def _notify_restart_complete(self) -> None:
        """После рестарта daemon-а отправить 'готово' в TG если есть chat_id.
        
        При рестарте через 'перезапусти демона' или 'обновись' — chat_id
        сохраняется в /tmp/caesar-restart-chat-id. Новый daemon при старте
        читает этот файл и отправляет 'готово' через Telegram API.
        
        Раньше 'готово' отправлял отдельный subprocess, но systemd
        KillMode=control-group убивает setsid bash вместе с daemon.
        Поэтому единственный надёжный путь — daemon сам отправляет
        через уже инициализированный TG adapter.
        """
        try:
            chat_id_file = Path("/tmp/caesar-restart-chat-id")
            if not chat_id_file.exists():
                return  # Первый старт, не рестарт
            
            chat_id_str = chat_id_file.read_text().strip()
            if not chat_id_str:
                return
            
            # Удаляем файл чтобы при следующем рестарте не отправить дважды
            try:
                chat_id_file.unlink()
            except Exception:
                pass
            
            # Даём TG adapter время полностью стартовать (polling loop)
            await asyncio.sleep(3)
            
            # Проверяем что TG adapter жив и имеет bot_token
            if not self.config.telegram.bot_token:
                self.log.warning("Cannot send restart notification: no bot_token")
                return
            
            if not self.telegram_adapter._running:
                self.log.warning("Cannot send restart notification: TG adapter not running")
                return
            
            # Отправляем через TG adapter
            try:
                chat_id = int(chat_id_str)
                result = await self.telegram_adapter._send_message(
                    chat_id,
                    "✅ Caesar перезапущен и готов к работе.\n"
                    "Можешь продолжать — пиши задачи как обычно."
                )
                if result:
                    self.log.info(f"Restart notification sent to chat_id={chat_id}")
                else:
                    self.log.error(
                        f"Restart notification FAILED for chat_id={chat_id} — "
                        f"_send_message returned None (TG API error)"
                    )
            except ValueError:
                self.log.warning(f"Invalid chat_id in restart file: {chat_id_str}")
            except Exception as e:
                self.log.error(f"Failed to send restart notification: {type(e).__name__}: {e}")
        except Exception as e:
            self.log.warning(f"Restart notification check failed: {type(e).__name__}: {e}")
    
    async def _register_auto_cron(self) -> None:
        """Auto-register dream cycle и morning briefing как cron задачи.
        
        Эти задачи создаются автоматически если cron включён.
        Пользователь не должен их создавать вручную.
        """
        if not getattr(self.config.cron, "enabled", False):
            return
        
        if not self.cron._scheduler:
            return
        
        from apscheduler.triggers.cron import CronTrigger
        
        # 1. Dream Cycle — каждый день в 2:00
        dream_time = getattr(self.config.cron, "dream_cycle_time", "02:00")
        dream_h, dream_m = dream_time.split(":")
        
        try:
            self.cron._scheduler.add_job(
                self._run_dream_cycle,
                trigger=CronTrigger(hour=int(dream_h), minute=int(dream_m)),
                id="auto-dream-cycle",
                replace_existing=True,
            )
            self.log.info(f"Auto-cron: dream cycle registered at {dream_time}")
        except Exception as e:
            self.log.warning(f"Failed to register dream cycle cron: {e}")
        
        # 2. Morning Briefing — каждый день в 9:00
        # Только если не отключено пользователем через TG
        import yaml as _yaml
        from caesar.config import CONFIG_PATH
        
        # Читаем из config.yaml напрямую
        briefing_enabled = True
        try:
            if CONFIG_PATH.exists():
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    cfg_data = _yaml.safe_load(f) or {}
                briefing_enabled = cfg_data.get("cron", {}).get("morning_briefing_enabled", True)
        except Exception:
            pass
        
        if briefing_enabled:
            briefing_time = getattr(self.config.cron, "morning_briefing_time", "09:00")
            briefing_h, briefing_m = briefing_time.split(":")
            
            try:
                self.cron._scheduler.add_job(
                    self._run_morning_briefing,
                    trigger=CronTrigger(hour=int(briefing_h), minute=int(briefing_m)),
                    id="auto-morning-briefing",
                    replace_existing=True,
                )
                self.log.info(f"Auto-cron: morning briefing registered at {briefing_time}")
            except Exception as e:
                self.log.warning(f"Failed to register morning briefing cron: {e}")
        else:
            self.log.info("Morning briefing disabled by user")
        
        # 3. Auto-cleanup — раз в неделю (воскресенье 3:00)
        # Только если не отключено
        cleanup_enabled = True
        try:
            if CONFIG_PATH.exists():
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    cfg_data = _yaml.safe_load(f) or {}
                cleanup_enabled = cfg_data.get("cron", {}).get("auto_cleanup_enabled", True)
        except Exception:
            pass
        
        if cleanup_enabled:
            try:
                self.cron._scheduler.add_job(
                    self._run_auto_cleanup,
                    trigger=CronTrigger(day_of_week="sun", hour=3, minute=0),
                    id="auto-weekly-cleanup",
                    replace_existing=True,
                )
                self.log.info("Auto-cron: weekly cleanup registered (Sun 03:00)")
            except Exception as e:
                self.log.warning(f"Failed to register weekly cleanup cron: {e}")
        else:
            self.log.info("Auto-cleanup disabled by user")
    
    async def _run_dream_cycle(self) -> None:
        """Запустить dream cycle (вызывается cron в 2:00)."""
        self.log.info("🌙 Dream cycle triggered by cron")
        
        try:
            from caesar.core.dream import DreamCycle
            dream = DreamCycle(
                config=self.config,
                storage=self.storage,
                kg=self.kg,
                llm_router=self.llm,
                event_bus=self.event_bus,
                l3_memory=self.l3,
            )
            
            report = await dream.run()
            
            # Сохраняем отчёт для morning briefing
            import json as _json
            report["timestamp"] = datetime.now().isoformat()
            report_file = self.storage.db_path.parent / "last_dream_report.json"
            with open(report_file, "w", encoding="utf-8") as f:
                _json.dump(report, f, ensure_ascii=False, indent=2)
            
            self.log.info(f"Dream cycle done: {report}")
        except Exception as e:
            self.log.error(f"Dream cycle failed: {e}")
    
    async def _run_dream_cycle_on_demand(
        self, force_all: bool = False, force_topic_only: bool = True,
    ) -> dict:
        """Запустить dream cycle по требованию (через socket API).
        
        Используется для команды 'проиндексируй память' из Telegram/CLI.
        
        Args:
            force_all: консолидировать ВСЕ чанки, не только за 24 часа
            force_topic_only: запустить ТОЛЬКО topic consolidation (без enrich и т.д.)
        
        Returns:
            Report dict от DreamCycle.run()
        """
        self.log.info(
            f"🧠 On-demand dream cycle: force_all={force_all}, "
            f"topic_only={force_topic_only}"
        )
        
        try:
            from caesar.core.dream import DreamCycle
            dream = DreamCycle(
                config=self.config,
                storage=self.storage,
                kg=self.kg,
                llm_router=self.llm,
                event_bus=self.event_bus,
                l3_memory=self.l3,
            )
            
            report = await dream.run(
                force_topic_consolidation=force_topic_only,
                force_all=force_all,
            )
            
            self.log.info(f"On-demand dream cycle done: {report}")
            return report
        except Exception as e:
            self.log.exception(f"On-demand dream cycle failed: {e}")
            return {"error": str(e), "topics_consolidated": 0, "chunks_created": 0}
    
    async def _run_morning_briefing(self) -> None:
        """Отправить morning briefing (вызывается cron в 9:00)."""
        self.log.info("🌅 Morning briefing triggered by cron")
        
        try:
            from caesar.core.briefing import MorningBriefing
            
            briefing = MorningBriefing(
                config=self.config,
                storage=self.storage,
                event_bus=self.event_bus,
            )
            
            # Находим user_id и channel_id для отправки
            # Берём первого пользователя с TG привязкой
            import os as _os
            user_id = f"cli-{_os.getuid()}"
            existing = self.storage.get_user_by_uid(_os.getuid())
            if existing:
                user_id = existing["id"]
            
            # Получаем source_chat_id для TG — ищем по ВСЕМ пользователям
            # т.к. CLI user (cli-1000) и TG user (user-tg-208118) могут быть разными
            tg_chat_id = ""
            with self.storage._conn() as conn:
                row = conn.execute(
                    "SELECT source_chat_id FROM channels WHERE source = 'telegram' LIMIT 1",
                ).fetchone()
                if row:
                    tg_chat_id = row["source_chat_id"]
            
            text = await briefing.generate_and_send(
                user_id=user_id,
                channel_id=tg_chat_id,
            )
            
            self.log.info(f"Morning briefing sent ({len(text)} chars)")
        except Exception as e:
            self.log.error(f"Morning briefing failed: {e}")
    
    async def _run_auto_cleanup(self) -> None:
        """Weekly auto-cleanup (вызывается cron в воскресенье 3:00)."""
        self.log.info("🧹 Weekly auto-cleanup triggered by cron")
        
        try:
            import subprocess
            
            venv_python = str(Path.home() / ".local/share/caesar/venv/bin/python")
            if not Path(venv_python).exists():
                venv_python = "python3"
            
            # Run audit --fix
            proc = await asyncio.create_subprocess_exec(
                venv_python, "-m", "caesar.management", "db", "audit", "--fix",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=120)
            
            # Run vacuum
            proc = await asyncio.create_subprocess_exec(
                venv_python, "-m", "caesar.management", "db", "vacuum",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=120)
            
            # Run archive-logs
            proc = await asyncio.create_subprocess_exec(
                venv_python, "-m", "caesar.management", "db", "archive-logs",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=120)
            
            self.log.info("Weekly auto-cleanup complete")
        except Exception as e:
            self.log.error(f"Auto-cleanup failed: {e}")
    
    def _is_quiet_hours(self) -> bool:
        """Проверить тихие часы."""
        try:
            from datetime import time as dt_time
            start_str = getattr(self.config.cron, "quiet_hours_start", "23:00")
            end_str = getattr(self.config.cron, "quiet_hours_end", "08:00")
            
            start_h, start_m = map(int, start_str.split(":"))
            end_h, end_m = map(int, end_str.split(":"))
            
            now = datetime.now().time()
            start = dt_time(start_h, start_m)
            end = dt_time(end_h, end_m)
            
            if start <= end:
                return start <= now <= end
            else:
                return now >= start or now <= end
        except Exception:
            return False
    
    async def stop(self) -> None:
        self.log.info("Agent daemon stopping...")
        self._running = False
        
        # Persist unfinished tasks ПЕРЕД остановкой queue — чтобы даже если
        # graceful shutdown не успеет завершить задачи, они сохранились в БД
        # и подхватились при следующем старте.
        try:
            persisted = self.queue.persist_unfinished_tasks()
            if persisted > 0:
                self.log.info(f"Persisted {persisted} unfinished task(s) to DB")
        except Exception as e:
            self.log.error(f"Failed to persist unfinished tasks: {e}")
        
        # Graceful stop — ждём завершения активных задач (до 180 сек)
        # Если не успели — они уже сохранены в БД выше, подхватятся после рестарта
        await self.orchestrator.stop()
        await self.queue.stop(timeout=180.0)
        await self.cron.stop()
        await self.telegram_adapter.stop()
        await self.cli_adapter.stop()
        
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        
        if SOCKET_PATH.exists():
            SOCKET_PATH.unlink()
        
        try:
            from systemd import daemon as sd
            sd.notify("STOPPING=1")
        except ImportError:
            pass
        
        self.log.info("Agent daemon stopped")
    
    async def _handle_cli_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            line = await reader.readline()
            if not line:
                return
            
            request = json.loads(line.decode("utf-8"))
            response = await self.cli_adapter.handle_request(request)
            
            response_data = json.dumps(response, ensure_ascii=False) + "\n"
            writer.write(response_data.encode("utf-8"))
            await writer.drain()
        except json.JSONDecodeError as e:
            error = {"error": "invalid_json", "message": str(e)}
            writer.write((json.dumps(error) + "\n").encode("utf-8"))
            await writer.drain()
        except Exception as e:
            self.log.exception(f"Error handling CLI client: {e}")
            error = {"error": "internal", "message": str(e)}
            try:
                writer.write((json.dumps(error) + "\n").encode("utf-8"))
                await writer.drain()
            except Exception:
                pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
    
    async def heartbeat(self) -> None:
        try:
            from systemd import daemon as sd
        except ImportError:
            return
        
        while self._running:
            try:
                sd.notify("WATCHDOG=1")
            except Exception:
                pass
            await asyncio.sleep(30)


async def main() -> int:
    config = Config.load()
    setup_logging()
    log = get_logger("main")
    
    daemon = AgentDaemon(config)
    
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    
    def _signal_handler(signame: str):
        def handler():
            log.info(f"Received {signame}")
            stop_event.set()
        return handler
    
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler(sig.name))
    
    try:
        await daemon.start()
        daemon._heartbeat_task = asyncio.create_task(daemon.heartbeat())
        log.info("Daemon running, waiting for stop signal...")
        await stop_event.wait()
    except Exception as e:
        log.exception(f"Daemon crashed: {e}")
        return 1
    finally:
        await daemon.stop()
    
    return 0


if __name__ == "__main__":
    if "--graceful-stop" in sys.argv:
        print("Graceful stop not implemented yet")
        sys.exit(0)
    
    sys.exit(asyncio.run(main()))
