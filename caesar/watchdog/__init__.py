"""Умный watchdog — второй агент который надзирает за основным.

Запускается каждые 2 минуты. Читает последние сообщения диалога,
анализирует через cheap LLM, принимает решения:

1. Агент ответил и ждёт "продолжай"? → отправляет "продолжай"
2. Задача зависла (running > 5 мин)? → останавливает + уведомляет
3. Агент выдал ошибку? → анализирует, пытается починить
4. Всё нормально? → успокаивается

НЕ перезапускает daemon — это делает systemd Restart=always.
НЕ заменяет пользователя — только когда агент явно ждёт ответа.
"""

import asyncio
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from caesar.config import Config, SOCKET_PATH, IS_DEV, DB_PATH, CONFIG_PATH
from caesar.logging_setup import setup_logging, get_logger


class SmartWatchdog:
    """Умный наблюдатель за агентом.
    
    Каждые 2 минуты:
    1. Читает последние сообщения из БД
    2. Через cheap LLM анализирует состояние диалога
    3. Принимает решение: действовать или нет
    """
    
    def __init__(self, config: Config):
        self.config = config
        self.log = get_logger("watchdog")
        self._running = False
        self._interval_sec = 60  # 1 минута — чаще проверяем
        self._stuck_timeout_sec = 180  # 3 минуты = завис (было 5, слишком долго)
        self._bot_token = config.telegram.bot_token
        self._last_action_time = {}  # channel_id → timestamp последнего действия
        self._action_cooldown = 120  # 2 минуты между действиями (было 5)
        self._llm_base_url = None
        self._llm_api_key = None
        self._llm_model = None
        self._init_llm()
    
    def _init_llm(self):
        """Инициализация cheap LLM для анализа."""
        # Авто-настройка как в LLMRouter
        if not self.config.llm.cheap_api_key and self.config.llm.smart_api_key:
            self._llm_api_key = self.config.llm.smart_api_key
            self._llm_base_url = self.config.llm.smart_base_url or "https://api.openai.com/v1"
            if self.config.llm.cheap_model == "gpt-4o-mini" and self.config.llm.smart_provider != "openai":
                self._llm_model = self.config.llm.smart_model
            else:
                self._llm_model = self.config.llm.cheap_model
        else:
            self._llm_api_key = self.config.llm.cheap_api_key
            self._llm_base_url = self.config.llm.cheap_base_url or "https://api.openai.com/v1"
            self._llm_model = self.config.llm.cheap_model
    
    async def start(self) -> None:
        self._running = True
        self.log.info(f"Smart Watchdog started (interval={self._interval_sec}s)")
        
        try:
            from systemd import daemon as sd
            sd.notify("READY=1")
        except ImportError:
            pass
        
        while self._running:
            try:
                await self._check()
            except Exception as e:
                self.log.exception(f"Watchdog check failed: {e}")
            
            await asyncio.sleep(self._interval_sec)
    
    async def stop(self) -> None:
        self._running = False
        self.log.info("Smart Watchdog stopped")
    
    async def _check(self) -> None:
        """Одна итерация проверки."""
        # 1. Проверяем зависшие задачи
        await self._check_stuck_tasks()
        
        # 2. Анализируем последние диалоги через LLM
        if self._llm_api_key:
            await self._check_dialogs()
        else:
            # Fallback: regex-проверка патологий
            await self._check_pathologies_regex()
        
        # 3. Проверяем недоставленные cron ответы
        await self._check_undelivered_cron()
    
    async def _check_stuck_tasks(self) -> None:
        """Проверить зависшие задачи — running > max_time(complexity) + 60с.

        Только TRUE hangs: задача превысила свой лимит + self-cancel (0.7.4
        asyncio.wait_fог) не сработал = event-loop заблокирован. НЕ фложит
        legit задачи внутри их max_time. На true hang — mark failed + notify +
        RESTART daemon (пинок).
        """
        conn = None
        try:
            import sqlite3
            conn = sqlite3.connect(str(DB_PATH), timeout=5)
            conn.row_factory = sqlite3.Row

            now = datetime.now()
            rows = conn.execute("""
                SELECT id, started_at, complexity, channel_id, user_id,
                       user_message, source_chat_id, source
                FROM tasks
                WHERE status = 'running'
            """).fetchall()

            for row in rows:
                d = dict(row)
                try:
                    started = datetime.fromisoformat(
                        str(d['started_at']).replace("T", " ").split(".")[0]
                    )
                except Exception:
                    continue

                # Per-task max_time by complexity (как orchestrator._max_time_sec)
                complexity = d.get("complexity", "simple")
                max_time_sec = {
                    "simple": 600, "medium": 3600, "complex": 14400,
                }.get(complexity, 600)
                stuck_threshold = max_time_sec + 60  # buffer
                elapsed = (now - started).total_seconds()

                if elapsed < stuck_threshold:
                    continue  # внутри лимита — не зависла

                stuck_min = elapsed / 60
                self.log.warning(
                    f"TRUE STUCK {d['id']}: {stuck_min:.0f} min "
                    f"(max={max_time_sec // 60}min, complexity={complexity}) — "
                    f"self-cancel failed, restarting daemon"
                )

                conn.execute(
                    "UPDATE tasks SET status = 'failed', error = ? WHERE id = ?",
                    (f"Истинное зависание ({stuck_min:.0f} мин, max={max_time_sec // 60}мин) "
                     f"— убит watchdog + daemon restart", d['id']),
                )
                conn.commit()

                await self._notify_user(
                    d.get('source_chat_id', ''),
                    f"⚠️ Задача зависла намертво ({stuck_min:.0f} мин) — перезапускаю daemon.\n"
                    f"Задача: «{d['user_message'][:80]}»\n"
                    f"Попробуй ещё раз после рестарта."
                )

                # Пинок: рестарт daemon (true hang = event-loop заблокирован)
                try:
                    import subprocess
                    subprocess.run(
                        ["systemctl", "--user", "restart", "caesar-daemon"],
                        capture_output=True, timeout=10,
                    )
                    self.log.warning("Watchdog: restarted caesar-daemon (true hang)")
                except Exception as e:
                    self.log.error(f"Watchdog: cannot restart daemon: {e}")
                break  # один рестарт за цикл — не флудим

        except Exception as e:
            self.log.debug(f"Cannot check stuck tasks: {e}")
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
    
    async def _check_dialogs(self) -> None:
        """Анализировать последние диалоги через cheap LLM.
        
        Для каждого канала берём последние 6 сообщений, отправляем в LLM,
        получаем решение: OK / CONTINUE / STUCK / ERROR.
        """
        conn = None
        try:
            import sqlite3
            conn = sqlite3.connect(str(DB_PATH), timeout=5)
            conn.row_factory = sqlite3.Row
            
            # Находим все активные каналы с сообщениями за последние 10 минут
            cutoff = datetime.now() - timedelta(minutes=10)
            
            channels = conn.execute("""
                SELECT DISTINCT channel_id FROM conversation_messages
                WHERE timestamp >= ?
                ORDER BY timestamp DESC LIMIT 5
            """, (cutoff.strftime("%Y-%m-%d %H:%M:%S"),)).fetchall()
            
            for ch_row in channels:
                channel_id = ch_row["channel_id"]
                
                # Cooldown — не действуем чаще чем раз в 5 минут на канал
                last_action = self._last_action_time.get(channel_id, 0)
                if time.time() - last_action < self._action_cooldown:
                    continue
                
                # Берём последние 6 сообщений
                msgs = conn.execute("""
                    SELECT role, content, timestamp FROM conversation_messages
                    WHERE channel_id = ?
                    ORDER BY timestamp DESC LIMIT 6
                """, (channel_id,)).fetchall()
                
                if len(msgs) < 2:
                    continue  # Мало сообщений для анализа
                
                # Разворачиваем — от старых к новым
                msgs = list(reversed(msgs))
                
                # Проверяем — последнее сообщение от assistant?
                last_msg = msgs[-1]
                if last_msg["role"] != "assistant":
                    continue  # Последнее слово за пользователем — не вмешиваемся
                
                # Формируем диалог для LLM
                dialog_text = ""
                for m in msgs:
                    role = "Пользователь" if m["role"] == "user" else "Caesar"
                    content = (m["content"] or "")[:500]
                    dialog_text += f"{role}: {content}\n"
                
                # Анализируем через LLM
                decision = await self._analyze_dialog(dialog_text)
                
                if decision and decision != "OK":
                    self.log.info(
                        f"Watchdog decision for {channel_id}: {decision}"
                    )
                    
                    if decision == "CONTINUE":
                        # Агент ждёт "продолжай" — отправляем
                        await self._send_to_channel(channel_id, conn, "продолжай")
                        self._last_action_time[channel_id] = time.time()
                    
                    elif decision == "STUCK":
                        # Диалог застрял — уведомляем
                        await self._notify_user_by_channel(channel_id, conn,
                            "⚠️ Похоже диалог застрял. Напиши что-нибудь или /clear."
                        )
                        self._last_action_time[channel_id] = time.time()
                    
                    elif decision == "ERROR":
                        # Агент выдал ошибку — уведомляем
                        await self._notify_user_by_channel(channel_id, conn,
                            "⚠️ Последний ответ содержал ошибку. "
                            "Попробуй переформулировать или /clear."
                        )
                        self._last_action_time[channel_id] = time.time()
            
        except Exception as e:
            self.log.debug(f"Cannot check dialogs: {e}")
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
    
    async def _analyze_dialog(self, dialog_text: str) -> str | None:
        """Анализировать диалог через cheap LLM.
        
        Возвращает: OK | CONTINUE | STUCK | ERROR | None
        """
        if not self._llm_api_key:
            return None
        
        prompt = (
            "Ты — наблюдатель за AI-агентом Caesar. Проанализируй последний диалог.\n\n"
            "Определи состояние:\n"
            "- OK: агент дал полный ответ, не ждёт ничего от пользователя\n"
            "- CONTINUE: агент спрашивает «хочешь продолжить?», «следующий шаг?», "
            "«уточните» — ждёт разрешения продолжить\n"
            "- STUCK: агент повторяет одно и то же, диалог застрял\n"
            "- ERROR: агент выдал ошибку или не смог выполнить задачу\n\n"
            "Ответь ОДНИМ словом: OK, CONTINUE, STUCK или ERROR.\n\n"
            f"ДИАЛОГ:\n{dialog_text}"
        )
        
        url = f"{self._llm_base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._llm_api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self._llm_model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "max_tokens": 10,
        }
        
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(url, json=payload, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                    content = content.strip().upper()
                    if "CONTINUE" in content:
                        return "CONTINUE"
                    elif "STUCK" in content:
                        return "STUCK"
                    elif "ERROR" in content:
                        return "ERROR"
                    else:
                        return "OK"
                else:
                    self.log.warning(f"Watchdog LLM HTTP {resp.status_code}")
                    return None
        except Exception as e:
            self.log.debug(f"Watchdog LLM failed: {e}")
            return None
    
    async def _check_pathologies_regex(self) -> None:
        """Fallback: regex-проверка патологий (без LLM)."""
        pathology_patterns = [
            r"хочешь.*продолж",
            r"продолжить\?",
            r"следующий\s+шаг",
            r"уточните",
            r"подскажите",
            r"что\s+делать\s+дальше",
        ]
        regexes = [re.compile(p, re.IGNORECASE) for p in pathology_patterns]
        
        conn = None
        try:
            import sqlite3
            conn = sqlite3.connect(str(DB_PATH), timeout=5)
            conn.row_factory = sqlite3.Row
            
            cutoff = datetime.now() - timedelta(minutes=5)
            
            rows = conn.execute("""
                SELECT channel_id, content FROM conversation_messages
                WHERE role = 'assistant' AND timestamp >= ?
                ORDER BY timestamp DESC LIMIT 10
            """, (cutoff.strftime("%Y-%m-%d %H:%M:%S"),)).fetchall()
            
            for row in rows:
                channel_id = row["channel_id"]
                content = row["content"] or ""
                
                # Cooldown
                last_action = self._last_action_time.get(channel_id, 0)
                if time.time() - last_action < self._action_cooldown:
                    continue
                
                # Проверяем последние 2 предложения
                sentences = re.split(r'[.!?]\s+', content.strip())
                last_two = " ".join(sentences[-2:]) if len(sentences) >= 2 else content
                
                for regex in regexes:
                    if regex.search(last_two):
                        self.log.info(
                            f"PATHOLOGY detected in {channel_id}: "
                            f"pattern='{regex.pattern}' — sending 'продолжай'"
                        )
                        await self._send_to_channel(channel_id, conn, "продолжай")
                        self._last_action_time[channel_id] = time.time()
                        break
            
        except Exception as e:
            self.log.debug(f"Cannot check pathologies: {e}")
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
    
    async def _check_undelivered_cron(self) -> None:
        """Проверить cron задачи которые не доставлены до TG."""
        conn = None
        try:
            import sqlite3
            conn = sqlite3.connect(str(DB_PATH), timeout=5)
            conn.row_factory = sqlite3.Row
            
            cutoff = datetime.now() - timedelta(minutes=5)
            
            rows = conn.execute("""
                SELECT id, result, source, source_chat_id, completed_at
                FROM tasks
                WHERE source = 'cron' 
                AND status = 'completed' 
                AND result IS NOT NULL
                AND completed_at >= ?
                ORDER BY completed_at DESC LIMIT 5
            """, (cutoff.strftime("%Y-%m-%d %H:%M:%S"),)).fetchall()
            
            for row in rows:
                d = dict(row)
                source_chat = d.get('source_chat_id', '') or ''
                
                if not source_chat.isdigit():
                    self.log.warning(
                        f"CRON UNDELIVERED: task {d['id']} — "
                        f"source_chat_id='{source_chat}' not TG. Re-sending."
                    )
                    
                    tg_chat_id = self._find_tg_chat_id(conn)
                    if tg_chat_id and d.get('result'):
                        success = await self._send_tg_message(
                            tg_chat_id,
                            d['result'][:3500]
                        )
                        if success:
                            # Помечаем что доставлено — только если send реально прошёл
                            conn.execute(
                                "UPDATE tasks SET source_chat_id = ? WHERE id = ?",
                                (tg_chat_id, d['id']),
                            )
                            conn.commit()
                            self.log.info(
                                f"CRON re-delivered: task {d['id']} → TG {tg_chat_id}"
                            )
                        else:
                            self.log.warning(
                                f"CRON re-deliver FAILED for task {d['id']} — will retry next iteration"
                            )
            
        except Exception as e:
            self.log.debug(f"Cannot check undelivered cron: {e}")
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
    
    def _find_tg_chat_id(self, conn) -> str | None:
        """Найти Telegram chat_id из таблицы channels."""
        try:
            row = conn.execute(
                """SELECT source_chat_id FROM channels 
                   WHERE source = 'telegram' 
                   AND source_chat_id IS NOT NULL LIMIT 1""",
            ).fetchone()
            if row and row["source_chat_id"]:
                candidate = str(row["source_chat_id"])
                if candidate.isdigit():
                    return candidate
        except Exception:
            pass
        return None
    
    async def _send_to_channel(self, channel_id: str, conn, message: str) -> None:
        """Отправить сообщение в канал — как если бы пользователь написал.
        
        Ищем TG chat_id для этого channel_id, отправляем через TG API.
        Содержимое сохраняем в conversation_messages как user message.
        """
        # Ищем TG chat_id
        tg_chat_id = None
        try:
            row = conn.execute(
                "SELECT source_chat_id FROM channels WHERE id = ?",
                (channel_id,),
            ).fetchone()
            if row and row["source_chat_id"]:
                candidate = str(row["source_chat_id"])
                if candidate.isdigit():
                    tg_chat_id = candidate
        except Exception:
            pass
        
        if not tg_chat_id:
            tg_chat_id = self._find_tg_chat_id(conn)
        
        if tg_chat_id:
            # Сохраняем в БД как user message
            try:
                import uuid
                conn.execute(
                    """INSERT INTO conversation_messages (id, channel_id, role, content)
                       VALUES (?, ?, 'user', ?)""",
                    (f"msg-{uuid.uuid4().hex[:12]}", channel_id, message),
                )
                conn.commit()
            except Exception:
                pass
            
            # Отправляем в TG
            await self._send_tg_message(tg_chat_id, f"🔄 [watchdog] {message}")
            
            # Создаём задачу для агента
            try:
                # Получаем user_id для этого канала
                row = conn.execute(
                    "SELECT user_id FROM channels WHERE id = ?",
                    (channel_id,),
                ).fetchone()
                user_id = row["user_id"] if row else ""
                
                if user_id:
                    import socket as socket_mod
                    import json as json_mod
                    if SOCKET_PATH.exists():
                        sock = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
                        sock.settimeout(5)
                        sock.connect(str(SOCKET_PATH))
                        request = {
                            "action": "send_message",
                            "user_id": user_id,
                            "channel_name": channel_id.rsplit(":", 1)[-1] if ":" in channel_id else "main",
                            "message": message,
                        }
                        sock.sendall((json_mod.dumps(request) + "\n").encode())
                        sock.recv(65536)
                        sock.close()
                        self.log.info(
                            f"Watchdog sent '{message}' to channel {channel_id} "
                            f"via socket API"
                        )
            except Exception as e:
                self.log.warning(f"Watchdog could not create task: {e}")
    
    async def _notify_user(self, source_chat_id: str, message: str) -> None:
        """Уведомить пользователя о проблеме через TG."""
        tg_chat_id = None
        
        if source_chat_id and source_chat_id.isdigit():
            tg_chat_id = source_chat_id
        else:
            conn = None
            try:
                import sqlite3
                conn = sqlite3.connect(str(DB_PATH), timeout=5)
                conn.row_factory = sqlite3.Row
                tg_chat_id = self._find_tg_chat_id(conn)
            except Exception:
                pass
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
        
        if tg_chat_id:
            await self._send_tg_message(tg_chat_id, message)
        else:
            self.log.warning("Cannot notify user: no TG chat_id found")
    
    async def _notify_user_by_channel(self, channel_id: str, conn, message: str) -> None:
        """Уведомить пользователя по channel_id."""
        tg_chat_id = None
        try:
            row = conn.execute(
                "SELECT source_chat_id FROM channels WHERE id = ?",
                (channel_id,),
            ).fetchone()
            if row and row["source_chat_id"]:
                candidate = str(row["source_chat_id"])
                if candidate.isdigit():
                    tg_chat_id = candidate
        except Exception:
            pass
        
        if not tg_chat_id:
            tg_chat_id = self._find_tg_chat_id(conn)
        
        if tg_chat_id:
            await self._send_tg_message(tg_chat_id, message)
    
    async def _send_tg_message(self, chat_id: str, text: str) -> bool:
        """Отправить сообщение напрямую через Telegram Bot API."""
        if not self._bot_token:
            self.log.warning("Cannot send TG message: no bot_token")
            return False
        
        url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
                resp = await client.post(url, json={
                    "chat_id": int(chat_id),
                    "text": text,
                })
                if resp.status_code == 200:
                    result = resp.json()
                    if result.get("ok"):
                        return True
                    self.log.error(f"TG API not ok: {result.get('description')}")
                else:
                    self.log.error(f"TG API HTTP {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            self.log.error(f"TG send failed: {type(e).__name__}: {e}")
        return False


async def main() -> int:
    config = Config.load()
    setup_logging()
    
    watchdog = SmartWatchdog(config)
    
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    
    def _signal_handler(signame: str):
        def handler():
            get_logger("watchdog.main").info(f"Received {signame}")
            stop_event.set()
        return handler
    
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler(sig.name))
    
    try:
        # Запускаем watchdog как task чтобы stop_event был достижим
        task = asyncio.create_task(watchdog.start())
        await stop_event.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    finally:
        await watchdog.stop()
    
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
