"""Telegram-адаптер.

См. roadmap раздел 13 (Каналы ввода/вывода).

Использует Bot API (long polling).
- Карточка прогресса с накапливающимися эмодзи
- sendMessageDraft для streaming (Bot API 9.3+) — TODO
- Inline buttons для подтверждений и выбора
- telegramify-markdown для конверсии Markdown → MarkdownV2
- Два режима: web (t.me/s/ парсинг) для чтения каналов, MTProto (опция)
"""

import asyncio
import json
import re
import subprocess
import time
from typing import Any

import httpx

from caesar.config import CONFIG_DIR, Config
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
from caesar.core.queue import TaskQueue, TaskPriority, TaskComplexity
from caesar.logging_setup import get_logger


# Лимиты TG
TG_MAX_MESSAGE_LENGTH = 4096
TG_EDIT_THROTTLE_SEC = 0.5  # не чаще 2 раз в сек


class TgSession:
    """Активная TG-сессия (один чат)."""
    
    def __init__(self, chat_id: int, user_id_tg: int, channel_id: str):
        self.chat_id = chat_id
        self.user_id_tg = user_id_tg
        self.channel_id = channel_id
        # internal user_id (из БД) — устанавливается при создании сессии
        self.user_id: str = ""
        # message_id карточки прогресса (для редактирования)
        self.progress_message_id: int | None = None
        self.last_icon: str | None = None
        self.icons_sequence: list[str] = []  # все показанные иконки
        self.last_edit_at: float = 0
        # последний момент активности в чате — для TTL-чистки неактивных сессий
        self.last_activity: float = time.time()
        self.last_progress_text: str = ""
        self.last_answer_message_id: int | None = None
        # GOD MODE — снимает ВСЕ блокировки. Активируется секретным словом.
        # В god mode бот может выполнить ЛЮБУЮ команду: rm -rf /, sudo, reboot, и т.д.
        self.god_mode: bool = False
        # event handler — сохраняем чтобы можно было отписаться при /clear
        self._event_handler: Any = None


def authorize_tg_message(
    chat_id: int | None,
    user_tg_id: int | None,
    chat_type: str,
    tg_config,
) -> tuple[bool, str]:
    """Решает, пускать ли сообщение. Возвращает (allowed, reason).

    Чистая функция (без I/O) — тестируется unit-тестами. Привязка нового
    владельца по коду — отдельный flow в _handle_message.

    Политика:
    - не привязан (allowed_chat_ids пуст) → открыт (opt-in, без abrupt-локаута);
    - владелец (chat_id в allowed_chat_ids) → OK;
    - группа + allow_group_chats: group_access all → OK; owner → только если
      user_tg_id == owner_user_id (без повторной авторизации);
    - иначе → отказ (в приват — с сообщением, в группе — молча).
    """
    allowed_ids = list(getattr(tg_config, "allowed_chat_ids", None) or [])
    is_group = chat_type in ("group", "supergroup") or (chat_id is not None and chat_id < 0)
    if not allowed_ids:
        return True, "unpaired-open"
    if chat_id in allowed_ids:
        return True, "owner"
    if is_group and getattr(tg_config, "allow_group_chats", False):
        if getattr(tg_config, "group_access", "owner") == "all":
            return True, "group-all"
        owner_uid = getattr(tg_config, "owner_user_id", 0)
        if owner_uid and user_tg_id == owner_uid:
            return True, "group-owner"
        return False, "group-not-owner"
    return False, "not-owner"


class TelegramAdapter:
    """Telegram-адаптер через Bot API (long polling)."""
    
    def __init__(self, config: Config, event_bus: EventBus, queue: TaskQueue, storage=None):
        self.config = config
        self.event_bus = event_bus
        self.queue = queue
        self.storage = storage
        self.log = get_logger("telegram_adapter")
        
        self.bot_token = config.telegram.bot_token
        self.api_base = "https://api.telegram.org"
        
        # chat_id → TgSession
        self._sessions: dict[int, TgSession] = {}
        # tg_user_id → user_id (внутренний)
        self._tg_to_user: dict[int, str] = {}
        # chat_id → последний документ (для сценария: файл → 'запомни')
        # Хранит {file_name, content_text, user_id, timestamp, ext}
        # Очищается после передачи агенту или через 10 минут TTL
        self._last_documents: dict[int, dict] = {}
        # Session TTL — неактивные сессии старше этого (сек) удаляются
        self._session_ttl_seconds = 86400  # 24 часа
        # chat_id, которым уже сказали "бот не привязан" (чтобы не спамить)
        self._pairing_nagged: set = set()
        
        self._polling_task: asyncio.Task | None = None
        self._running = False
        self._last_update_id = 0

    def _prune_expired_sessions(self) -> int:
        """Удалить неактивные TG-сессии старше TTL.

        Без этого self._sessions и подписки event_bus росли бы безгранично
        для каждого чата, который хоть раз написал боту. Возвращает число
        удалённых сессий (для тестов).
        """
        now = time.time()
        expired = [
            cid for cid, s in self._sessions.items()
            if now - s.last_activity > self._session_ttl_seconds
        ]
        for cid in expired:
            session = self._sessions.pop(cid, None)
            if session is None:
                continue
            if session._event_handler is not None:
                try:
                    self.event_bus.unsubscribe(str(cid), session._event_handler)
                except Exception as e:
                    self.log.debug(f"Cannot unsubscribe expired session {cid}: {e}")
            self._last_documents.pop(cid, None)
        if expired:
            self.log.info(f"Pruned {len(expired)} expired TG session(s)")
        return len(expired)

    async def start(self) -> None:
        if not self.bot_token:
            self.log.info("Telegram disabled (no bot_token configured)")
            return

        if not getattr(self.config.telegram, "allowed_chat_ids", None):
            self.log.warning(
                "TG: allowed_chat_ids пуст — бот открыт (любой знающий username "
                "может писать). Чтобы закрыть — telegram.allowed_chat_ids в config.yaml."
            )
        
        # Проверяем токен
        me = await self._api_call("getMe")
        if not me:
            self.log.error("Telegram bot token invalid")
            return
        
        self.log.info(f"Telegram bot: @{me.get('username', '?')} (id={me.get('id')})")
        
        self._running = True
        self._polling_task = asyncio.create_task(self._polling_loop())
    
    async def stop(self) -> None:
        if not self.bot_token:
            return
        self._running = False
        if self._polling_task:
            self._polling_task.cancel()
            try:
                await self._polling_task
            except asyncio.CancelledError:
                pass
        self.log.info("Telegram adapter stopped")
    
    async def _api_call(
        self,
        method: str,
        data: dict | None = None,
        files: dict | None = None,
    ) -> dict | None:
        """Вызвать Bot API метод."""
        url = f"{self.api_base}/bot{self.bot_token}/{method}"
        try:
            # timeout=60 — больше чем long polling (30 сек)
            # чтобы TG успел ответить до таймаута httpx
            async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
                if files:
                    resp = await client.post(url, data=data, files=files)
                else:
                    resp = await client.post(url, json=data or {})
                
                if resp.status_code != 200:
                    self.log.error(f"TG API {method} error {resp.status_code}: {resp.text[:200]}")
                    return None
                
                result = resp.json()
                if not result.get("ok"):
                    self.log.error(f"TG API {method} not ok: {result.get('description')}")
                    return None
                return result.get("result")
        except Exception as e:
            self.log.error(f"TG API {method} exception: {type(e).__name__}: {e}")
            return None
    
    async def _polling_loop(self) -> None:
        """Long polling loop для получения обновлений."""
        consecutive_errors = 0
        while self._running:
            self._prune_expired_sessions()
            try:
                updates = await self._api_call("getUpdates", {
                    "offset": self._last_update_id + 1,
                    "timeout": 30,  # long polling
                    "allowed_updates": ["message", "callback_query"],
                })

                # _api_call глотает network-ошибки и возвращает None. Воспринимаем
                # None как сбой → backoff (иначе polling долбит TG каждые ~10с часами
                # и зарабатывает IP-бан — RemoteProtocolError: Server disconnected).
                if updates is None:
                    raise ConnectionError("getUpdates returned None (network/API error)")

                # Сбрасываем счётчик ошибок при успехе
                consecutive_errors = 0

                if not updates:
                    continue

                for update in updates:
                    self._last_update_id = update.get("update_id", self._last_update_id)
                    try:
                        await self._handle_update(update)
                    except Exception as e:
                        # Ошибка в одном update не должна валить polling loop
                        self.log.exception(f"Error handling update {update.get('update_id')}: {e}")
                        await asyncio.sleep(1)

            except asyncio.CancelledError:
                break
            except (ConnectionResetError, ConnectionError, httpx.ConnectError,
                    httpx.ConnectTimeout, httpx.TimeoutException,
                    httpx.RemoteProtocolError, httpx.ReadTimeout) as e:
                # Сетевые ошибки — TG API недоступен. Не падаем, ждём.
                consecutive_errors += 1
                self.log.warning(
                    f"Network error (attempt {consecutive_errors}): "
                    f"{type(e).__name__}: {e}"
                )
                # Exponential backoff: 5s → 10 → 20 → 40 → 60 → 120 → 240 → 300 (cap 5 мин).
                # Cap 5 мин (не 60с) — чтобы не спамить TG при длительном отвале сети.
                delay = min(5 * (2 ** (consecutive_errors - 1)), 300)
                await asyncio.sleep(delay)
            except Exception as e:
                consecutive_errors += 1
                self.log.exception(f"Polling error (attempt {consecutive_errors}): {e}")
                await asyncio.sleep(min(5 * consecutive_errors, 300))
                if consecutive_errors > 20:
                    self.log.error(
                        "Too many consecutive polling errors — TG polling "
                        "may be stuck. Continuing anyway..."
                    )
                    consecutive_errors = 10  # не даём расти бесконечно
    
    async def _handle_update(self, update: dict) -> None:
        """Обработать update от Telegram."""
        if "message" in update:
            await self._handle_message(update["message"])
        elif "callback_query" in update:
            await self._handle_callback(update["callback_query"])
    
    async def _handle_message(self, message: dict) -> None:
        """Обработать входящее сообщение."""
        chat_id = message.get("chat", {}).get("id")
        user_tg = message.get("from", {})
        user_tg_id = user_tg.get("id")
        text = message.get("text", "")
        
        if not chat_id:
            return

        # === Авторизация + привязка (security) ===
        chat_type = message.get("chat", {}).get("type", "private")

        # Привязка: если активен код (caesar pair записал файл) и текст совпал —
        # привязываем отправителя как владельца.
        pairing_file = CONFIG_DIR / "pairing_code"
        if pairing_file.exists():
            try:
                code = pairing_file.read_text(encoding="utf-8").strip()
            except Exception:
                code = ""
            if code and text.strip() == code:
                allowed = list(self.config.telegram.allowed_chat_ids or [])
                if chat_id not in allowed:
                    allowed.append(chat_id)
                    self.config.telegram.allowed_chat_ids = allowed
                self.config.telegram.owner_user_id = user_tg_id or 0
                try:
                    self.config.save()
                except Exception as e:
                    self.log.error(f"Cannot save config after pairing: {e}")
                try:
                    pairing_file.unlink()
                except Exception:
                    pass
                await self._send_message(chat_id, "✅ Привязан! Теперь принимаю только твои команды.")
                self.log.info(f"TG paired: owner chat_id={chat_id} user_id={user_tg_id}")
                return
            # в режиме привязки — чужим не отвечаем командами
            await self._send_message(chat_id, "🔒 Жду код привязки (он показан в терминале при `caesar pair`).")
            return

        # Обычная авторизация (pure-функция — тестируется отдельно).
        ok, reason = authorize_tg_message(chat_id, user_tg_id, chat_type, self.config.telegram)
        if not ok:
            if reason == "group-not-owner":
                return  # в группе чужим молчим — без спама
            self.log.warning(f"TG auth: rejected chat_id={chat_id} user_tg={user_tg_id} reason={reason}")
            await self._send_message(chat_id, "⛔ Доступ запрещён — только владелец.")
            return
        if reason == "unpaired-open":
            # бот не привязан: работаем открыто, но один раз нудим привязаться.
            if chat_id not in self._pairing_nagged:
                self._pairing_nagged.add(chat_id)
                self.log.warning("TG: бот не привязан — работаю открыто. Запусти `caesar pair`.")
                await self._send_message(
                    chat_id,
                    "🔒 Бот не привязан — сейчас работаю открыто (кто угодно с username может писать). "
                    "Запусти на сервере `caesar pair`, чтобы закрыть.",
                )

        # Создаём TG сессию сразу — чтобы /status и другие команды
        # видели активную сессию. Сессия = подписка на events.
        if chat_id not in self._sessions:
            # Нужен user_id — попробуем найти, иначе создадим позже
            user_id = self._tg_to_user.get(user_tg_id, f"user-tg-{user_tg_id}")
            channel_id = f"channel:{user_id}:main"
            session = TgSession(chat_id, user_tg_id, channel_id)
            session.user_id = user_id
            if self.storage:
                session.god_mode = self.storage.get_user_god_mode(user_id)
            self._sessions[chat_id] = session
            handler = self._make_event_handler(session); session._event_handler = handler; self.event_bus.subscribe(str(chat_id), handler)
        
        # Voice / Audio message → STT
        voice = message.get("voice") or message.get("audio")
        if voice:
            # Проверяем включён ли STT
            stt_enabled = getattr(self.config.stt, "enabled", False) if hasattr(self.config, "stt") else False
            if not stt_enabled:
                await self._send_message(
                    chat_id,
                    "🎤 Голосовые сообщения отключены. Включи через caesar setup или "
                    "добавь 'stt: {enabled: true}' в config.yaml.",
                )
                return
            
            # Сразу показываем прогресс — распознавание может занять время
            # (скачивание + ffmpeg + whisper). Без этого пользователь не видит
            # что что-то происходит.
            progress_msg = await self._send_message(chat_id, "Caesar: 🗣️")
            progress_msg_id = progress_msg.get("message_id") if progress_msg else None
            
            # Скачиваем .ogg, транскрибируем
            text = await self._transcribe_voice(voice, chat_id, user_tg_id)
            
            # Удаляем индикатор распознавания
            if progress_msg_id:
                await self._api_call("deleteMessage", {
                    "chat_id": chat_id,
                    "message_id": progress_msg_id,
                })
            
            if not text:
                await self._send_message(chat_id, "🎤 Не удалось распознать голосовое сообщение.")
                return
            self.log.info(f"Voice transcribed for {user_tg_id}: '{text}' (len={len(text)})")
            # НЕ показываем распознанный текст пользователю — неестественно.
            # Текст сразу идёт агенту как обычное сообщение, ответ приходит как обычно.
            # Лог с распознанным текстом доступен в journalctl.
        
        # Document (.txt, .md, .pdf, .docx) → обработка
        document = message.get("document")
        if document:
            caption = message.get("caption", "") or ""
            text = await self._handle_document(document, chat_id, user_tg_id, caption)
            # text будет:
            #   - пустой если документ проиндексирован в L3 (по "запомни")
            #   - содержать текст документа + caption для агента (если не индексировали)
            if not text:
                return
        
        if not text:
            return
        
        # Проверка "запомни"/"сохрани" для последнего документа
        # Сценарий: пользователь отправил файл (без caption), потом пишет "запомни"
        # В этом случае индексируем последний документ в L3
        text_lower = text.strip().lower()
        remember_triggers = ["запомни", "сохрани", "запиши", "запомнить", "сохрани этот", "запомни это"]
        if any(text_lower.startswith(t) or text_lower == t for t in remember_triggers):
            # Проверяем есть ли недавний документ (в течение 10 минут)
            last_doc = self._last_documents.get(chat_id)
            if last_doc:
                import time as _time
                age = _time.time() - last_doc["timestamp"]
                if age < 600:  # 10 минут
                    # Индексируем последний документ
                    await self._index_last_document(chat_id, last_doc)
                    return
        
        # Распознавание коротких команд на естественном языке
        # (без слэша — просто слова). Не отправляем агенту, выполняем напрямую.
        # ВАЖНО: голосовые сообщения через Whisper могут распознаться неточно —
        # с заглавной буквы, с точкой, с лишним пробелом, с ошибкой.
        # Поэтому проверяем не точное совпадение, а startsWith / contains.
        normalized = text.strip().lower().rstrip("!.,? ")
        
        # GOD MODE — секретное слово которое снимает ВСЕ блокировки.
        # После ввода бот может выполнить ЛЮБУЮ команду (rm, sudo, reboot, и т.д.)
        # Действует до /clear или рестарта daemon.
        god_triggers = ("газ в пол", "полный вперёд", "полный вперед", "газвпол", "god mode")
        god_off_triggers = ("god mode off", "выключи god mode", "отключи god mode", "god mode выкл")
        
        if normalized in god_off_triggers or any(t in normalized for t in god_off_triggers):
            # Выключаем god mode
            user_id_for_god = ""
            if chat_id in self._sessions:
                self._sessions[chat_id].god_mode = False
                user_id_for_god = self._sessions[chat_id].user_id if hasattr(self._sessions[chat_id], 'user_id') else ""
            if self.storage and user_id_for_god:
                self.storage.set_user_god_mode(user_id_for_god, False)
            await self._send_message(chat_id, "🔒 GOD MODE выключен. Блокировки снова активны.")
            return
        
        if normalized in god_triggers or any(t in normalized for t in god_triggers):
            # Нужно найти user_id для этого chat_id
            # (сессия может ещё не быть создана)
            user_id_for_god = ""
            if chat_id in self._sessions:
                self._sessions[chat_id].god_mode = True
                user_id_for_god = self._sessions[chat_id].user_id if hasattr(self._sessions[chat_id], 'user_id') else ""
            # Сохраняем в БД чтобы orchestrator мог прочитать
            if self.storage and user_id_for_god:
                self.storage.set_user_god_mode(user_id_for_god, True)
            
            await self._send_message(
                chat_id,
                "🚀 GOD MODE АКТИВИРОВАН!\n\n"
                "Все блокировки сняты. Теперь я могу выполнить ЛЮБУЮ команду:\n"
                "• systemctl restart / stop\n"
                "• sudo ...\n"
                "• rm -rf ...\n"
                "• reboot / shutdown\n\n"
                "God mode сохранён в БД — переживает рестарт daemon.\n"
                "Для выключения: напиши 'god mode off'.\n"
                "⚠️ Будь осторожен — я больше не откажу ни в чём."
            )
            return
        
        # Команды обновления — расширенный список + fuzzy matching
        update_triggers = ("обновись", "обновить", "обновляй", "апдейт", "update", "upgrade")
        # Точное совпадение ИЛИ сообщение состоит из одного слова-триггера
        if normalized in update_triggers or any(
            normalized == t or normalized.startswith(t + " ") for t in update_triggers
        ):
            asyncio.create_task(self._handle_tg_update(chat_id))
            return
        
        # Команды статуса
        status_triggers = ("статус", "status", "статус системы", "статус демона")
        if normalized in status_triggers:
            status = await self._get_status_text(chat_id)
            await self._send_message(chat_id, status)
            return
        
        # Рестарт daemon (для голосовых особенно полезно)
        restart_triggers = (
            "рестартни daemon", "рестартни демона", "рестартни caesar",
            "перезапусти daemon", "перезапусти демона", "перезапусти caesar",
            "перезагрузи daemon", "перезагрузи демона", "перезагрузи caesar",
            "рестартни бота", "перезапусти бота",
        )
        if normalized in restart_triggers:
            # Триггерим через тот же путь что и текстовое сообщение —
            # отправляем в queue, оркестратор детектит и выполнит.
            pass  # Падаем в общий flow ниже — пусть LLM/детектор обработает
        # Диагностические команды для дебага обновлений
        if normalized in ("лог обновления", "лог рестарта", "update log", "лог апдейта"):
            await self._handle_tg_show_update_log(chat_id)
            return
        if normalized in ("очисти", "сбрось", "заново", "clear"):
            if self.storage and chat_id in self._sessions:
                session = self._sessions[chat_id]
                self.storage.clear_conversation(session.channel_id)
                # O5: отписываем handler от event_bus перед удалением сессии
                if session._event_handler:
                    self.event_bus.unsubscribe(str(chat_id), session._event_handler)
                del self._sessions[chat_id]
            # Чистим _last_documents тоже
            self._last_documents.pop(chat_id, None)
            await self._send_message(chat_id, "🧹 Контекст очищен. Начинаю новую сессию.")
            return
        
        # Управление настройками из TG
        if normalized in ("модели", "models", "выбери модель", "настрой модели"):
            await self._handle_tg_models(chat_id)
            return
        if normalized in ("помощь", "help", "команды", "что умеешь", "справка"):
            await self._send_message(chat_id, self._get_help_text(), parse_mode="HTML")
            return
        if normalized in ("брифинг вкл", "брифинг включить", "рассылка вкл", "дайджест вкл"):
            await self._toggle_setting(chat_id, "morning_briefing", True)
            return
        if normalized in ("брифинг выкл", "брифинг выключить", "рассылка выкл", "дайджест выкл"):
            await self._toggle_setting(chat_id, "morning_briefing", False)
            return
        if normalized in ("чистка вкл", "очистка вкл", "cleanup вкл"):
            await self._toggle_setting(chat_id, "auto_cleanup", True)
            return
        if normalized in ("чистка выкл", "очистка выкл", "cleanup выкл"):
            await self._toggle_setting(chat_id, "auto_cleanup", False)
            return
        if normalized in ("настройки", "settings"):
            await self._handle_settings(chat_id)
            return
        
        # Команда "проиндексируй память" — запустить on-demand topic consolidation
        # через socket API к daemon-у.
        if normalized in (
            "проиндексируй память", "индексируй память", "переиндексируй память",
            "реиндексируй память", "проиндексируй", "консолидируй память",
            "индексируй", "переиндексируй", "реиндексируй",
        ):
            asyncio.create_task(self._handle_tg_index_memory(chat_id))
            return
        
        # Блокируем management команды (caesar ...) — их нельзя отправлять агенту
        # Это предотвращает зацикливание когда пользователь случайно кидает
        # 'caesar models list' в чат и агент пытается их выполнить
        text_stripped = text.strip()
        if text_stripped.lower().startswith("caesar "):
            cmd_name = text_stripped.split()[1] if len(text_stripped.split()) > 1 else ""
            management_cmds = {
                "setup", "update", "rollback", "uninstall", "permissions",
                "stats", "enable", "l3", "models", "self-scan", "stop",
                "status", "restart",
            }
            if cmd_name.lower() in management_cmds:
                await self._send_message(
                    chat_id,
                    f"⚠️ 'caesar {cmd_name}' — это консольная команда, её нельзя "
                    f"выполнять в чате. Выполни в терминале:\n\n"
                    f"  caesar {cmd_name}\n\n"
                    f"Если хотел что-то другое — переформулируй.",
                )
                return
        
        # Пропускаем команды (кроме /start)
        if text.startswith("/"):
            if text == "/start":
                await self._send_message(chat_id, "Привет! Я Caesar. Просто напиши мне задачу.\n\nНапиши «помощь» чтобы увидеть все команды.")
            elif text == "/status":
                status = await self._get_status_text(chat_id)
                await self._send_message(chat_id, status)
            elif text in ("/help", "/помощь"):
                await self._send_message(chat_id, self._get_help_text(), parse_mode="HTML")
            elif text in ("/update", "/upgrade"):
                # Запускаем update в фоне — НЕ блокируем polling
                asyncio.create_task(self._handle_tg_update(chat_id))
            elif text == "/clear":
                # Очищаем историю диалога для этого чата
                if self.storage and chat_id in self._sessions:
                    session = self._sessions[chat_id]
                    self.storage.clear_conversation(session.channel_id)
                    # O5: отписываем handler от event_bus
                    if session._event_handler:
                        self.event_bus.unsubscribe(str(chat_id), session._event_handler)
                    del self._sessions[chat_id]
                self._last_documents.pop(chat_id, None)
                await self._send_message(chat_id, "🧹 Контекст очищен. Начинаю новую сессию.")
            elif text in ("/stop", "/abort", "/cancel"):
                # Экстренная остановка текущей задачи
                await self._handle_stop_task(chat_id)
            elif text in ("/settings", "/настройки"):
                await self._handle_settings(chat_id)
            return
        
        self.log.info(f"TG message from {user_tg_id} in chat {chat_id}: {text[:50]}...")
        
        # Получаем или создаём user_id
        # Roadmap раздел 14.7: терминал и главный TG-бот = одна сессия (main)
        # СВЯЗЫВАНИЕ: unix_uid ↔ telegram_id → один user_id для CLI и TG
        user_id = self._tg_to_user.get(user_tg_id)
        if not user_id:
            if self.storage:
                import os as _os
                # Шаг 1: Ищем по telegram_id
                existing_tg = self.storage.get_user_by_telegram(str(user_tg_id))
                # Шаг 2: Ищем по unix_uid (CLI пользователь)
                existing_cli = self.storage.get_user_by_uid(_os.getuid())
                
                if existing_tg and existing_cli and existing_tg["id"] != existing_cli["id"]:
                    # Оба найдены но разные — используем CLI user_id
                    user_id = existing_cli["id"]
                    # Очищаем telegram_id у старого TG user, потом связываем с CLI user
                    try:
                        with self.storage._conn() as conn:
                            conn.execute(
                                "UPDATE users SET telegram_id = NULL WHERE id = ?",
                                (existing_tg["id"],)
                            )
                    except Exception:
                        pass
                    self.storage.upsert_user(
                        user_id=user_id,
                        telegram_id=str(user_tg_id),
                        telegram_username=user_tg.get("username", ""),
                    )
                    self.log.info(f"Merged TG user {existing_tg['id']} → CLI user {user_id}")
                elif existing_cli:
                    # Есть CLI пользователь — связываем
                    user_id = existing_cli["id"]
                    self.storage.upsert_user(
                        user_id=user_id,
                        telegram_id=str(user_tg_id),
                        telegram_username=user_tg.get("username", ""),
                    )
                elif existing_tg:
                    # Есть только TG пользователь — даём ему unix_uid
                    user_id = existing_tg["id"]
                    self.storage.upsert_user(
                        user_id=user_id,
                        unix_uid=_os.getuid(),
                    )
                else:
                    # Нет никого — создаём с cli- префиксом
                    user_id = f"cli-{_os.getuid()}"
                    self.storage.upsert_user(
                        user_id=user_id,
                        unix_uid=_os.getuid(),
                        telegram_id=str(user_tg_id),
                        telegram_username=user_tg.get("username", ""),
                        display_name=user_tg.get("first_name", "") or f"User {_os.getuid()}",
                    )
                self._tg_to_user[user_tg_id] = user_id
            else:
                user_id = f"user-tg-{user_tg_id}"
                self._tg_to_user[user_tg_id] = user_id
        
        # Определяем channel_id
        # Главный бот → channel "main"
        # Если это форум-топик → channel с именем топика
        chat_type = message.get("chat", {}).get("type", "private")
        thread_id = message.get("message_thread_id")
        
        if chat_type == "private":
            channel_name = "main"
        elif thread_id:
            # Forum topic — попробуем получить имя
            forum_name = message.get("forum_topic_created", {}).get("name", f"topic-{thread_id}")
            channel_name = forum_name
        else:
            channel_name = f"chat-{chat_id}"
        
        channel_id = f"channel:{user_id}:{channel_name}"
        
        # Создаём user и channel в БД если их ещё нет
        if self.storage:
            username = user_tg.get("username", "")
            display_name = user_tg.get("first_name", "") or username or f"tg-{user_tg_id}"
            self.storage.upsert_user(
                user_id=user_id,
                telegram_id=str(user_tg_id),
                telegram_username=username,
                display_name=display_name,
            )
            self.storage.upsert_channel(
                channel_id=channel_id,
                user_id=user_id,
                source="telegram",
                source_chat_id=str(chat_id),
                display_name=channel_name,
            )
        
        # Сессия уже создана в _handle_message — обновляем user_id/channel_id
        # если они изменились (например, пользователь был привязан к CLI user)
        if chat_id in self._sessions:
            session = self._sessions[chat_id]
            session.user_id = user_id
            session.channel_id = channel_id
            session.last_activity = time.time()
        else:
            # На всякий случай — если сессия была удалена
            session = TgSession(chat_id, user_tg_id, channel_id)
            session.user_id = user_id
            if self.storage:
                session.god_mode = self.storage.get_user_god_mode(user_id)
            self._sessions[chat_id] = session
            handler = self._make_event_handler(session); session._event_handler = handler; self.event_bus.subscribe(str(chat_id), handler)
        
        # Оценка сложности — определяет пойдёт ли задача в фон (background pool)
        # Complex → background (10 workers, не блокирует чат)
        # Simple/Medium → interactive (5 workers, быстрый ответ)
        complexity = TaskComplexity.SIMPLE
        text_lower = text.lower()
        
        # Complex — исследования, анализ, долгие задачи
        # Эти задачи уводят в фон, пользователь может параллельно спрашивать другое
        complex_triggers = [
            "проанализируй", "изучи", "найди баги", "оптимизируй",
            "исследование", "исследуй", "расследование",
            "найди топ", "подбери топ", "топ 5", "топ-5", "топ 10", "топ-10",
            "подобрать", "подбери", "выбери лучшие", "найди лучшие",
            "собери данные", "собери информацию", "собери список",
            "сделай обзор", "сделай подборку", "собери подборку",
            "сравни", "сравнение",
            "найди сайты", "найди сервисы", "найди инструменты",
            "проверь все", "проверь каждый",
            "сделай отчёт", "сделай отчет", "подготовь отчёт",
            "deep dive", "разбери подробно",
        ]
        # Medium — средние задачи (поиск новостей, перевод)
        medium_triggers = [
            "найди новости", "сделай сводку", "отчёт", "переведи",
            "найди что нового", "найди информацию про",
        ]
        
        if any(m in text_lower for m in complex_triggers):
            complexity = TaskComplexity.COMPLEX
        elif any(m in text_lower for m in medium_triggers):
            complexity = TaskComplexity.MEDIUM
        
        # Если задача complex — сообщаем что уходит в фон
        if complexity == TaskComplexity.COMPLEX:
            await self._send_message(
                chat_id,
                f"🔬 Эта задача требует исследования — уводу в фон.\n"
                f"   Можешь задавать другие вопросы, я отвечу параллельно.\n"
                f"   Когда закончу — пришлю результат сюда.",
            )
        
        # Создаём задачу
        # Complex → background pool + NORMAL priority (не блокирует интерактив)
        # Simple/Medium → interactive pool + HIGH priority
        task_priority = TaskPriority.NORMAL if complexity == TaskComplexity.COMPLEX else TaskPriority.HIGH
        
        await self.queue.add_task(
            user_message=text,
            user_id=user_id,
            channel_id=channel_id,
            author_id=user_id,
            source="telegram",
            source_chat_id=str(chat_id),
            priority=task_priority,
            complexity=complexity,
        )
    
    async def _transcribe_voice(self, voice: dict, chat_id: int, user_tg_id: int) -> str | None:
        """Скачать voice .ogg из Telegram, распознать через faster-whisper.
        
        Telegram voice — это .ogg (Opus codec). faster-whisper требует wav,
        поэтому конвертируем через ffmpeg.
        """
        file_id = voice.get("file_id")
        if not file_id:
            return None
        
        # 1. Получаем file_path через getFile
        file_info = await self._api_call("getFile", {"file_id": file_id})
        if not file_info:
            self.log.error("TG getFile failed")
            return None
        
        file_path_tg = file_info.get("file_path", "")
        if not file_path_tg:
            return None
        
        # Размер файла (ограничение Bot API — 20 MB)
        file_size = file_info.get("file_size", 0)
        if file_size > 20 * 1024 * 1024:
            self.log.warning(f"Voice file too large: {file_size} bytes")
            return None
        
        # 2. Скачиваем файл
        download_url = f"{self.api_base}/file/bot{self.bot_token}/{file_path_tg}"
        tmp_ogg = None
        try:
            import tempfile, os
            tmp_fd, tmp_ogg = tempfile.mkstemp(suffix=".ogg", prefix="caesar_voice_")
            os.close(tmp_fd)
            
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(download_url)
                if resp.status_code != 200:
                    self.log.error(f"Voice download failed: HTTP {resp.status_code}")
                    return None
                with open(tmp_ogg, "wb") as f:
                    f.write(resp.content)
            
            # 3. Транскрипция через TranscribeAudioTool
            from caesar.tools.stt import TranscribeAudioTool
            
            tool = TranscribeAudioTool()
            # Параметры из конфига
            stt_model = getattr(self.config.stt, "model", "base") if hasattr(self.config, "stt") else "base"
            stt_language = getattr(self.config.stt, "language", None) if hasattr(self.config, "stt") else None
            
            # ОПРЕДЕЛЕНИЕ ЯЗЫКА ПОЛЬЗОВАТЕЛЯ:
            # Алгоритм (эффективный — как просил пользователь):
            # 1. Читаем сохранённый язык из БД (users.detected_language)
            # 2. Если язык ЕСТЬ → используем его (быстро, без сканирования)
            # 3. Если языка НЕТ → сканируем последние 5 текстовых сообщений,
            #    определяем язык, СОХРАНЯЕМ в БД, используем для этого голосового
            # 4. При следующем голосовом — язык уже сохранён, шаг 2
            #
            # Это значит: первое голосовое может сканировать историю,
            # все последующие — мгновенно берут сохранённый язык.
            if not stt_language:
                session = self._sessions.get(chat_id)
                if session and self.storage:
                    # Шаг 1: читаем сохранённый язык
                    saved_lang = self.storage.get_user_language(session.user_id)
                    if saved_lang:
                        # Шаг 2: язык есть — используем
                        stt_language = saved_lang
                        self.log.info(f"STT: using stored language='{stt_language}' for user={session.user_id}")
                    else:
                        # Шаг 3: языка нет — определяем по 5 последним сообщениям
                        stt_language = await self._detect_language_from_history(chat_id, session.user_id)
                        if stt_language:
                            # Сохраняем чтобы больше не определять
                            self.storage.set_user_language(session.user_id, stt_language)
                            self.log.info(f"STT: language='{stt_language}' detected and saved for user={session.user_id}")
                        else:
                            self.log.info(f"STT: no language detected, using Whisper auto-detect")
            
            result = await tool.execute(
                file_path=tmp_ogg,
                model=stt_model,
                language=stt_language,
            )
            
            if not result.success:
                self.log.error(f"STT failed: {result.error}")
                return None
            
            text = (result.data or {}).get("text", "")
            if not text.strip():
                return None
            
            # Логируем метрики
            duration = (result.data or {}).get("duration", 0)
            elapsed = (result.data or {}).get("elapsed", 0)
            self.log.info(
                f"STT: {duration:.1f}s audio → {elapsed:.1f}s process → {len(text)} chars"
            )
            
            return text.strip()
        
        except Exception as e:
            self.log.exception(f"Voice transcription error: {e}")
            return None
        finally:
            # Чистим tmp файл
            if tmp_ogg and os.path.exists(tmp_ogg):
                try:
                    os.unlink(tmp_ogg)
                except Exception:
                    pass
    
    async def _detect_language_from_history(self, chat_id: int, user_id: str) -> str | None:
        """Определить язык пользователя по последним 5 сообщениям.
        
        Вызывается ТОЛЬКО когда нет сохранённого языка в БД.
        Сканирует последние 5 текстовых сообщений, считает символы по скриптам,
        возвращает доминирующий язык ('ru', 'en', 'ar', 'zh') или None.
        
        После этого язык сохраняется в БД и больше не пересчитывается.
        """
        if not self.storage:
            return None
        
        try:
            session = self._sessions.get(chat_id)
            if not session:
                return None
            channel_id = session.channel_id
            
            # Берём последние 5 сообщений
            messages = self.storage.get_messages(channel_id, limit=5)
            if not messages:
                return None
            
            # Считаем символы по скриптам
            script_counts = {
                "cyrillic": 0,
                "latin": 0,
                "arabic": 0,
                "cjk": 0,
            }
            
            for msg in messages:
                content = msg.get("content", "") or ""
                for ch in content:
                    cp = ord(ch)
                    if 0x0400 <= cp <= 0x04FF:
                        script_counts["cyrillic"] += 1
                    elif 0x0600 <= cp <= 0x06FF:
                        script_counts["arabic"] += 1
                    elif (0x4E00 <= cp <= 0x9FFF) or (0x3040 <= cp <= 0x30FF):
                        script_counts["cjk"] += 1
                    elif 0x0041 <= cp <= 0x024F:
                        script_counts["latin"] += 1
            
            # Находим доминирующий скрипт
            max_script = max(script_counts.items(), key=lambda x: x[1])
            
            # Порог: минимум 10 символов чтобы быть уверенным
            if max_script[1] < 10:
                return None  # слишком мало данных, Whisper сделает auto-detect
            
            script_to_lang = {
                "cyrillic": "ru",
                "latin": "en",
                "arabic": "ar",
                "cjk": "zh",
            }
            
            detected = script_to_lang.get(max_script[0])
            if detected:
                self.log.info(
                    f"Language detected from history: user={user_id}, "
                    f"language='{detected}' (scripts={script_counts})"
                )
            return detected
        except Exception as e:
            self.log.warning(f"Language detection from history failed: {e}")
            return None
    
    async def _handle_document(self, document: dict, chat_id: int, user_tg_id: int, caption: str = "") -> str:
        """Скачать документ, извлечь текст, решить что с ним делать.
        
        Логика:
        - caption содержит "запомни"/"сохрани"/"запиши" → индексировать в L3
        - любой другой caption (или без caption) → передать агенту как контекст
          (пользователь хочет что-то сделать: проверка орфографии, перевод, и т.д.)
        
        Также сохраняем документ в _last_documents для сценария:
          файл (без caption) → следующее сообщение "запомни" → индексируем
        
        Возвращает:
        - "" если документ проиндексирован (агенту не передаём)
        - текст для агента (caption + содержимое документа) если НЕ индексировали
        """
        file_name = document.get("file_name", "document")
        file_id = document.get("file_id")
        file_size = document.get("file_size", 0)
        
        if not file_id:
            return ""
        
        # Проверяем размер (TG Bot API лимит 20MB)
        if file_size > 20 * 1024 * 1024:
            await self._send_message(chat_id, f"📄 Файл слишком большой ({file_size // 1024 // 1024} MB). Максимум 20 MB.")
            return ""
        
        # Проверяем расширение
        from pathlib import Path
        ext = Path(file_name).suffix.lower()
        supported = {".txt", ".md", ".markdown", ".pdf", ".docx"}
        if ext not in supported:
            await self._send_message(
                chat_id,
                f"📄 Файл '{file_name}' не поддерживается. "
                f"Поддерживаются: {', '.join(sorted(supported))}",
            )
            return ""
        
        # Индикатор загрузки
        progress_msg = await self._send_message(chat_id, f"Caesar: 📄 {file_name}")
        progress_msg_id = progress_msg.get("message_id") if progress_msg else None
        
        tmp_file = None
        try:
            # 1. Скачиваем файл
            file_info = await self._api_call("getFile", {"file_id": file_id})
            if not file_info:
                await self._send_message(chat_id, "❌ Не удалось получить файл")
                return ""
            
            file_path_tg = file_info.get("file_path", "")
            if not file_path_tg:
                return ""
            
            download_url = f"{self.api_base}/file/bot{self.bot_token}/{file_path_tg}"
            import tempfile, os, time as _time
            tmp_fd, tmp_file = tempfile.mkstemp(suffix=ext, prefix="caesar_doc_")
            os.close(tmp_fd)
            
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.get(download_url)
                if resp.status_code != 200:
                    await self._send_message(chat_id, f"❌ Ошибка скачивания: HTTP {resp.status_code}")
                    return ""
                with open(tmp_file, "wb") as f:
                    f.write(resp.content)
            
            # 2. Извлекаем текст
            content_text = await self._extract_document_text(tmp_file, ext)
            if not content_text:
                await self._send_message(chat_id, f"❌ Не удалось извлечь текст из {file_name}")
                return ""
            
            self.log.info(f"Document '{file_name}' extracted: {len(content_text)} chars")
            
            # Сохраняем как "последний документ" — на случай если пользователь
            # потом напишет "запомни" в следующем сообщении
            user_id = self._tg_to_user.get(user_tg_id, f"tg-{user_tg_id}")
            self._last_documents[chat_id] = {
                "file_name": file_name,
                "content_text": content_text,
                "user_id": user_id,
                "timestamp": _time.time(),
                "ext": ext,
                "file_size": file_size,
            }
            
            # 3. Решаем: индексировать или передать агенту?
            caption_lower = caption.lower().strip()
            remember_triggers = ["запомни", "сохрани", "запиши", "запомнить", 
                                 "сохрани это", "запомни это", "сохрани этот"]
            should_index = any(
                caption_lower.startswith(t) or caption_lower == t 
                for t in remember_triggers
            )
            
            if should_index:
                # Проверяем что L3 включён
                l3_enabled = getattr(self.config.l3, "enabled", False) if hasattr(self.config, "l3") else False
                if not l3_enabled:
                    await self._send_message(
                        chat_id,
                        "📄 Индексация документов требует L3 векторную память. "
                        "Включи: caesar enable l3",
                    )
                    return ""
                
                # Удаляем триггер из caption (если есть остаток — это тег/имя)
                for t in remember_triggers:
                    if caption_lower.startswith(t):
                        caption = caption[len(t):].strip()
                        break
                
                # Индексируем в L3
                chunk_ids = await self._index_document_in_l3(
                    user_id=user_id,
                    file_name=file_name,
                    content_text=content_text,
                    file_size=file_size,
                    ext=ext,
                    tag=caption,  # остаток caption как тег
                    chat_id=chat_id,
                )
                
                # Удаляем индикатор
                if progress_msg_id:
                    await self._api_call("deleteMessage", {
                        "chat_id": chat_id,
                        "message_id": progress_msg_id,
                    })
                
                # Сообщаем об успехе
                tag_info = f" (тег: {caption})" if caption else ""
                await self._send_message(
                    chat_id,
                    f"📄 Документ '{file_name}' загружен и проиндексирован{tag_info}.\n"
                    f"   Размер: {len(content_text):,} символов\n"
                    f"   Чанков в L3: {len(chunk_ids)}\n"
                    f"   Теперь я могу отвечать на вопросы по этому документу.",
                )
                
                # Очищаем последний документ (уже проиндексирован)
                self._last_documents.pop(chat_id, None)
                
                return ""  # агенту не передаём
            
            else:
                # НЕ индексируем — передаём агенту как контекст
                # (проверка орфографии, перевод, саммари, и т.д.)
                
                # Удаляем индикатор
                if progress_msg_id:
                    await self._api_call("deleteMessage", {
                        "chat_id": chat_id,
                        "message_id": progress_msg_id,
                    })
                
                # Формируем текст для агента
                if caption:
                    agent_text = (
                        f"{caption}\n\n"
                        f"--- Содержимое документа '{file_name}' ---\n"
                        f"{content_text}"
                    )
                else:
                    agent_text = (
                        f"Пользователь отправил документ '{file_name}' "
                        f"({len(content_text):,} символов). Содержимое:\n\n"
                        f"{content_text}"
                    )
                
                # Обрезаем если слишком длинное (LLM контекст)
                if len(agent_text) > 50000:
                    agent_text = agent_text[:50000] + f"\n\n... (truncated, total {len(content_text):,} chars)"
                
                return agent_text
        
        except Exception as e:
            self.log.exception(f"Document handling error: {e}")
            await self._send_message(chat_id, f"❌ Ошибка: {e}")
            return ""
        
        finally:
            if tmp_file and os.path.exists(tmp_file):
                try:
                    os.unlink(tmp_file)
                except Exception:
                    pass
    
    async def _index_document_in_l3(
        self, user_id: str, file_name: str, content_text: str,
        file_size: int, ext: str, tag: str = "",
        chat_id: int | None = None,
    ) -> list[str]:
        """Проиндексировать документ в L3 векторной памяти.
        
        Если chat_id передан — будет показывать прогресс в TG
        (включая скачивание модели при первой индексации).
        """
        from caesar.memory.l3 import L3Memory, set_progress_callback, _embedding_model
        from caesar.memory.storage import Storage as _Storage
        
        # Устанавливаем callback для прогресса скачивания модели
        # (сработает только при первой индексации, когда модель не загружена)
        progress_msg_id = None
        if chat_id is not None:
            # Проверяем нужно ли скачивать модель
            model_already_loaded = _embedding_model is not None
            
            async def send_progress(stage: str, message: str):
                nonlocal progress_msg_id
                if stage == "download_start":
                    # Меняем индикатор на 'скачиваю модель'
                    msg = await self._send_message(chat_id, f"📥 {message}")
                    if msg:
                        progress_msg_id = msg.get("message_id")
                elif stage == "download_done":
                    if progress_msg_id:
                        await self._api_call("editMessageText", {
                            "chat_id": chat_id,
                            "message_id": progress_msg_id,
                            "text": f"✅ Модель загружена",
                        })
                elif stage == "error":
                    if progress_msg_id:
                        await self._api_call("editMessageText", {
                            "chat_id": chat_id,
                            "message_id": progress_msg_id,
                            "text": f"⚠️ {message}",
                        })
            
            # sync wrapper — callback вызывается из sync кода (_get_embedding_model)
            def progress_sync(stage: str, message: str):
                try:
                    asyncio.create_task(send_progress(stage, message))
                except Exception:
                    pass
            
            set_progress_callback(progress_sync)
        
        storage = self.storage if self.storage else _Storage()
        l3_model_key = getattr(self.config.l3, "model", "multilingual-minilm") if hasattr(self.config, "l3") else "multilingual-minilm"
        l3 = L3Memory(storage, model_key=l3_model_key)
        
        metadata = {
            "source": "telegram_document",
            "file_name": file_name,
            "file_size": file_size,
            "file_type": ext,
        }
        if tag:
            metadata["tag"] = tag
        
        chunk_ids = await l3.add(
            user_id=user_id,
            channel="documents",
            content=content_text,
            author_id=user_id,
            metadata=metadata,
        )
        
        # Удаляем сообщение о модели если оно было
        if progress_msg_id:
            await self._api_call("deleteMessage", {
                "chat_id": chat_id,
                "message_id": progress_msg_id,
            })
        
        self.log.info(f"Document '{file_name}' indexed: {len(chunk_ids)} chunks in L3")
        return chunk_ids
    
    async def _index_last_document(self, chat_id: int, doc_info: dict) -> None:
        """Индексировать последний документ (когда пользователь написал 'запомни'
        в сообщении после отправки файла)."""
        await self._send_message(chat_id, f"📄 Индексирую '{doc_info['file_name']}'...")
        
        # Проверяем что L3 включён
        l3_enabled = getattr(self.config.l3, "enabled", False) if hasattr(self.config, "l3") else False
        if not l3_enabled:
            await self._send_message(
                chat_id,
                "📄 Индексация документов требует L3 векторную память. "
                "Включи: caesar enable l3",
            )
            return
        
        try:
            chunk_ids = await self._index_document_in_l3(
                user_id=doc_info["user_id"],
                file_name=doc_info["file_name"],
                content_text=doc_info["content_text"],
                file_size=doc_info["file_size"],
                ext=doc_info["ext"],
                chat_id=chat_id,
            )
            
            await self._send_message(
                chat_id,
                f"📄 Документ '{doc_info['file_name']}' проиндексирован.\n"
                f"   Размер: {len(doc_info['content_text']):,} символов\n"
                f"   Чанков в L3: {len(chunk_ids)}\n"
                f"   Теперь я могу отвечать на вопросы по этому документу.",
            )
            
            # Очищаем
            self._last_documents.pop(chat_id, None)
        except Exception as e:
            self.log.exception(f"Index last document failed: {e}")
            await self._send_message(chat_id, f"❌ Ошибка индексации: {e}")
    
    async def _extract_document_text(self, file_path: str, ext: str) -> str:
        """Извлечь текст из файла. БУЛЕТПРУФ определение кодировки.
        
        Стратегия: пробуем все кодировки, считаем гласные, выбираем лучшую.
        """
        if ext in (".txt", ".md", ".markdown"):
            try:
                with open(file_path, "rb") as f:
                    raw_bytes = f.read()
            except Exception as e:
                self.log.error(f"Failed to read file: {e}")
                return ""
            
            if not raw_bytes:
                return ""
            
            # BOM checks
            if raw_bytes.startswith(b"\xef\xbb\xbf"):
                try:
                    return raw_bytes[3:].decode("utf-8")
                except UnicodeDecodeError:
                    pass
            elif raw_bytes.startswith(b"\xff\xfe"):
                try:
                    return raw_bytes.decode("utf-16-le")
                except UnicodeDecodeError:
                    pass
            elif raw_bytes.startswith(b"\xfe\xff"):
                try:
                    return raw_bytes.decode("utf-16-be")
                except UnicodeDecodeError:
                    pass
            
            # UTF-8 — строгая проверка. Если decode прошёл без ошибок — это UTF-8.
            # Все остальные кодировки (cp1251, koi8-r) однобайтовые и МОГУТ декодировать
            # любые байты без ошибки, но результат будет кракозябрами.
            # Только UTF-8 имеет встроенную валидацию последовательностей.
            try:
                text = raw_bytes.decode("utf-8")
                self.log.info("Encoding: UTF-8 (strict decode succeeded)")
                return text
            except UnicodeDecodeError:
                pass
            
            # Не UTF-8 — используем chardet + проверку русскими словами
            chardet_result = None
            try:
                import chardet
                detected = chardet.detect(raw_bytes[:50000])
                chardet_result = detected
                self.log.info(f"chardet: {detected}")
            except ImportError:
                self.log.warning("chardet not installed")
            
            # Кандидаты
            candidates = ["koi8-r", "cp1251", "cp866", "iso-8859-5", "mac_cyrillic"]
            if chardet_result and chardet_result.get("encoding"):
                enc = chardet_result["encoding"]
                # Нормализуем: MacCyrillic → mac_cyrillic
                enc = enc.lower().replace("-", "_")
                if enc in candidates:
                    candidates.remove(enc)
                    candidates.insert(0, enc)
            
            # ПРОБОВАТЬ ВСЕ — выбираем по РУССКИМ СЛОВАМ
            # Это надёжнее гласных и биграмм: " и ", " в ", " не " и т.д.
            # есть в ЛЮБОМ русском тексте, но не в кракозябрах
            russian_words = [
                " и ", " в ", " не ", " на ", " что ", " это ", 
                " для ", " или ", " как ", " по ", " при ", " от ",
                " до ", " со ", " то ", " он ", " она ", " они ",
                " мы ", " вы ", " ты ", " бы ", " же ", " ли ",
            ]
            
            best_text = None
            best_score = -1
            best_enc = ""
            
            for enc in candidates:
                try:
                    text = raw_bytes.decode(enc)
                    sample = text[:5000].lower()
                    
                    # Считаем русские слова
                    word_count = sum(1 for w in russian_words if w in sample)
                    
                    self.log.info(f"Encoding {enc}: {word_count} russian words")
                    
                    if word_count > best_score:
                        best_score = word_count
                        best_text = text
                        best_enc = enc
                except (UnicodeDecodeError, LookupError):
                    continue
            
            if best_text and best_score > 0:
                self.log.info(f"Best encoding: {best_enc} ({best_score} words)")
                return best_text
            
            # Last resort
            return raw_bytes.decode("utf-8", errors="replace")
        
        elif ext == ".pdf":
            from caesar.tools.documents import ParsePdfTool
            tool = ParsePdfTool()
            result = await tool.execute(path=file_path, ocr=False, max_chars=500000)
            if result.success and result.data:
                return result.data.get("text", "") or result.data.get("content", "")
            return ""
        
        elif ext == ".docx":
            from caesar.tools.documents import ParseDocxTool
            tool = ParseDocxTool()
            result = await tool.execute(path=file_path, max_chars=500000)
            if result.success and result.data:
                return result.data.get("text", "") or result.data.get("content", "")
            return ""
        
        return ""
    
    def _is_text_clean(self, text: str, sample_size: int = 1000) -> bool:
        """Проверить что текст выглядит осмысленным (не кракозябры).
        
        Многоуровневая проверка:
        1. Control chars и replacement chars
        2. Наличие гласных (аеиоуыэюя) — в кракозябрах их почти нет
        3. Наличие пробелов — нормальный текст имеет пробелы
        4. Ratio печатных символов
        5. Box drawing chars (кракозябры из cp866)
        """
        if not text:
            return False
        
        sample = text[:sample_size]
        if not sample or len(sample) < 10:
            return False
        
        total = len(sample)
        
        # 1. Считаем категории
        printable = 0
        suspicious = 0
        replacement_chars = 0
        vowels = 0
        spaces = 0
        
        russian_vowels = set("аеиоуыэюяАЕИОУЫЭЮЯёЁ")
        
        for ch in sample:
            cp = ord(ch)
            if ch == "\ufffd":
                replacement_chars += 1
            elif cp < 32 and ch not in "\n\t\r":
                suspicious += 1
            elif 0x80 <= cp <= 0x9F:  # C1 control
                suspicious += 1
            elif 0x2500 <= cp <= 0x257F:  # Box drawing — кракозябры из cp866
                suspicious += 1
            elif 0x0180 <= cp <= 0x024F:  # Latin Extended-B
                suspicious += 1
            else:
                printable += 1
                if ch in russian_vowels:
                    vowels += 1
                if ch == " ":
                    spaces += 1
        
        # 2. Replacement chars
        if replacement_chars > total * 0.05:
            return False
        
        # 3. Suspicious chars
        if suspicious > total * 0.05:
            return False
        
        # 4. Printable ratio
        if printable < total * 0.7:
            return False
        
        # 5. Гласные — КЛЮЧЕВАЯ проверка!
        # В нормальном русском тексте гласные = 15-25% от букв
        # В кракозябрах (koi8-r→utf-8, cp1251→utf-8) — почти 0%
        # т.к. гласные заменяются на другие символы
        if total > 50:
            if vowels < total * 0.03:  # < 3% гласных — кракозябры
                return False
            if spaces < total * 0.05:  # < 5% пробелов — кракозябры
                return False
        
        return True
    
    async def _handle_callback(self, callback: dict) -> None:
        """Обработать inline button callback."""
        callback_id = callback.get("id")
        data = callback.get("data", "")
        message = callback.get("message", {})
        chat_id = message.get("chat", {}).get("id")
        
        # Acknowledge
        await self._api_call("answerCallbackQuery", {"callback_query_id": callback_id})
        
        # Парсим data: "answer:yes" | "answer:no" | "allow:..." | "deny:..." | "model:smart:..." | "model:cheap:..."
        if data.startswith("answer:"):
            answer = data.split(":", 1)[1]
            # TODO: передать ответ в оркестратор
            await self._send_message(chat_id, f"✅ Принято: {answer}")
        elif data.startswith("model:"):
            await self._handle_model_callback(callback)
    
    def _make_event_handler(self, session: TgSession):
        """Создать обработчик events для сессии TG."""
        async def handler(event: Event) -> None:
            await self._render_event(session, event)
        return handler
    
    async def _render_event(self, session: TgSession, event: Event) -> None:
        """Отрендерить event в Telegram.
        
        Roadmap раздел 4: иконки накапливаются в длину, без текста.
        Но в TG одиночная эмодзи показывается огромной — поэтому
        используем префикс 'Caesar: ' чтобы иконки всегда были мелкими.
        """
        if event.type == EVENT_PROGRESS_START:
            # Создаём сообщение-карточку с префиксом (чтобы TG не увеличивал иконку)
            text = "Caesar: 🧠"
            msg = await self._send_message(session.chat_id, text)
            if msg:
                session.progress_message_id = msg.get("message_id")
                session.last_icon = "🧠"
                session.icons_sequence = ["🧠"]
                session.last_progress_text = text
        
        elif event.type == EVENT_PROGRESS_UPDATE:
            icon = event.data.get("icon", "")
            # Не дублируем подряд одинаковые
            if icon == session.last_icon:
                return
            session.last_icon = icon
            session.icons_sequence.append(icon)
            
            # Throttle
            now = time.time()
            if now - session.last_edit_at < TG_EDIT_THROTTLE_SEC:
                await asyncio.sleep(TG_EDIT_THROTTLE_SEC - (now - session.last_edit_at))
            
            # Префикс "Caesar: " + иконки — чтобы TG не увеличивал одиночную эмодзи
            text = "Caesar: " + " ".join(session.icons_sequence)
            if len(text) > 200:
                # Схлопываем если слишком длинно
                text = "Caesar: " + " ".join(session.icons_sequence[-20:])
            
            if session.progress_message_id:
                await self._api_call("editMessageText", {
                    "chat_id": session.chat_id,
                    "message_id": session.progress_message_id,
                    "text": text,
                })
                session.last_progress_text = text
                session.last_edit_at = time.time()
        
        elif event.type == EVENT_ANSWER_READY:
            content = event.data.get("content", "")
            
            # Удаляем карточку прогресса
            if session.progress_message_id:
                del_result = await self._api_call("deleteMessage", {
                    "chat_id": session.chat_id,
                    "message_id": session.progress_message_id,
                })
                if not del_result:
                    self.log.warning(f"Failed to delete progress message {session.progress_message_id}")
                session.progress_message_id = None
                session.icons_sequence = []
            
            # Отправляем ответ
            msg = await self._send_message(session.chat_id, content)
            if msg:
                session.last_answer_message_id = msg.get("message_id")
        
        elif event.type == EVENT_QUESTION_ASKED:
            question = event.data.get("question", "")
            options = event.data.get("options", [])
            
            # Inline keyboard
            keyboard = None
            if options:
                keyboard = {
                    "inline_keyboard": [
                        [{
                            "text": opt.get("label", ""),
                            "callback_data": f"answer:{opt.get('value', '')}",
                        }]
                        for opt in options
                    ]
                }
            
            await self._send_message(session.chat_id, question, reply_markup=keyboard)
        
        elif event.type == EVENT_INFO_NOTIFICATION:
            message = event.data.get("message", "")
            await self._send_message(session.chat_id, f"ℹ️ {message}")
        
        elif event.type == EVENT_WARNING_NOTIFICATION:
            message = event.data.get("message", "")
            await self._send_message(session.chat_id, f"⚠️ {message}")
        
        elif event.type == EVENT_ERROR_OCCURRED:
            message = event.data.get("message", "")
            # Удаляем карточку
            if session.progress_message_id:
                await self._api_call("deleteMessage", {
                    "chat_id": session.chat_id,
                    "message_id": session.progress_message_id,
                })
                session.progress_message_id = None
            await self._send_message(session.chat_id, f"❌ Ошибка: {message}")
    
    async def _send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict | None = None,
        parse_mode: str | None = None,
    ) -> dict | None:
        """Отправить сообщение.
        
        Всегда используем MarkdownV2 через telegramify-markdown если доступен.
        Если нет — plain text.
        """
        # Пытаемся конвертировать markdown → MarkdownV2
        final_text = text
        final_parse_mode = parse_mode  # по умолчанию = caller's parse_mode
        
        # Markdownify только если caller НЕ запросил HTML
        if parse_mode != "HTML":
            try:
                from telegramify_markdown import markdownify
                final_text = markdownify(text)
                final_parse_mode = "MarkdownV2"
            except ImportError:
                pass
        
        data: dict = {
            "chat_id": chat_id,
            "text": final_text,
        }
        if final_parse_mode:
            data["parse_mode"] = final_parse_mode
        if reply_markup:
            data["reply_markup"] = reply_markup
        
        # Если текст длиннее лимита — разбиваем
        if len(text) > TG_MAX_MESSAGE_LENGTH:
            parts = self._split_message(text)
            last_msg = None
            for i, part in enumerate(parts):
                # Для каждой части: используем тот же parse_mode что у caller
                # Не пытаемся markdownify — caller уже позаботился о форматировании
                part_data = {
                    "chat_id": chat_id,
                    "text": part,
                }
                if final_parse_mode:
                    part_data["parse_mode"] = final_parse_mode
                elif parse_mode:
                    part_data["parse_mode"] = parse_mode
                # reply_markup только для последней части
                if reply_markup and i == len(parts) - 1:
                    part_data["reply_markup"] = reply_markup
                last_msg = await self._api_call("sendMessage", part_data)
            return last_msg
        
        return await self._api_call("sendMessage", data)
    
    def _split_message(self, text: str) -> list[str]:
        """Разбить длинный текст на части ≤ 4096 символов."""
        parts = []
        while text:
            if len(text) <= TG_MAX_MESSAGE_LENGTH:
                parts.append(text)
                break
            # Ищем перенос строки
            chunk = text[:TG_MAX_MESSAGE_LENGTH]
            last_nl = chunk.rfind("\n")
            if last_nl > 1000:
                parts.append(text[:last_nl])
                text = text[last_nl + 1:]
            else:
                parts.append(chunk)
                text = text[TG_MAX_MESSAGE_LENGTH:]
        return parts
    
    async def _toggle_setting(self, chat_id: int, setting: str, value: bool) -> None:
        """Включить/выключить настройку из Telegram."""
        import yaml as _yaml
        from caesar.config import CONFIG_PATH
        
        # Читаем config
        data = {}
        if CONFIG_PATH.exists():
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    data = _yaml.safe_load(f) or {}
            except Exception:
                data = {}
        
        # Обновляем
        if "cron" not in data:
            data["cron"] = {}
        
        setting_names = {
            "morning_briefing": "Утренний брифинг",
            "auto_cleanup": "Авто-очистка",
        }
        
        if setting == "morning_briefing":
            data["cron"]["morning_briefing_enabled"] = value
        elif setting == "auto_cleanup":
            data["cron"]["auto_cleanup_enabled"] = value
        
        # Сохраняем
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                _yaml.dump(data, f, allow_unicode=True, default_flow_style=False)
            
            status = "✅ вкл" if value else "❌ выкл"
            name = setting_names.get(setting, setting)
            await self._send_message(chat_id, f"⚙️ {name}: {status}")
            
            # Перезапуск daemon чтобы подхватил
            import subprocess as _sp
            _sp.run(["systemctl", "--user", "restart", "caesar-daemon"],
                     capture_output=True, timeout=10)
        except Exception as e:
            await self._send_message(chat_id, f"❌ Ошибка: {e}")
    
    async def _handle_settings(self, chat_id: int) -> None:
        """Показать текущие настройки из Telegram."""
        import yaml as _yaml
        from caesar.config import CONFIG_PATH, Config
        
        config = Config.load()
        
        lines = ["⚙️ Настройки Caesar\n"]
        
        # LLM
        lines.append("🤖 Модели:")
        lines.append(f"  Smart: {config.llm.smart_provider} / {config.llm.smart_model}")
        lines.append(f"  Cheap: {config.llm.cheap_provider} / {config.llm.cheap_model}")
        
        # Features
        lines.append("\n🔌 Функции:")
        lines.append(f"  STT (голосовые): {'✅' if getattr(config.stt, 'enabled', False) else '❌'}")
        lines.append(f"  L3 (память): {'✅' if getattr(config.l3, 'enabled', False) else '❌'}")
        lines.append(f"  Cron: {'✅' if getattr(config.cron, 'enabled', False) else '❌'}")
        
        # Cron settings
        if getattr(config.cron, "enabled", False):
            lines.append("\n⏰ Расписание:")
            
            # Читаем дополнительные настройки из YAML
            data = {}
            if CONFIG_PATH.exists():
                try:
                    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                        data = _yaml.safe_load(f) or {}
                except Exception:
                    pass
            
            cron_data = data.get("cron", {})
            briefing_enabled = cron_data.get("morning_briefing_enabled", True)
            cleanup_enabled = cron_data.get("auto_cleanup_enabled", True)
            
            lines.append(f"  Dream cycle: {getattr(config.cron, 'dream_cycle_time', '02:00')} (всегда вкл)")
            lines.append(f"  Утренний брифинг: {getattr(config.cron, 'morning_briefing_time', '09:00')} {'✅' if briefing_enabled else '❌'}")
            lines.append(f"  Авто-очистка: воскресенье 03:00 {'✅' if cleanup_enabled else '❌'}")
            lines.append(f"  Тихие часы: {getattr(config.cron, 'quiet_hours_start', '23:00')}-{getattr(config.cron, 'quiet_hours_end', '08:00')}")
        
        lines.append("\n💬 Команды:")
        lines.append("  брифинг вкл/выкл — утренний дайджест")
        lines.append("  чистка вкл/выкл — авто-очистка")
        lines.append("  проиндексируй память — консолидация по темам")
        lines.append("  статус — статус системы")
        lines.append("  обновись — обновить Caesar")
        
        await self._send_message(chat_id, "\n".join(lines))
    
    def _get_help_text(self) -> str:
        """Полный список команд для /help и 'помощь'.
        
        Использует HTML форматирование (TG поддерживает нативно, без библиотек):
        <b>жирный</b> — для команд
        обычный текст — для объяснений
        """
        return (
            "<b>🤖 Caesar — команды</b>\n\n"
            
            "<b>Управление:</b>\n"
            "  <b>/status</b> — состояние системы, память, токены, модели\n"
            "  <b>/clear</b> — очистить контекст диалога (начать заново)\n"
            "  <b>/stop</b> — остановить текущую задачу\n"
            "  <b>/update</b> — обновить код + перезапуск daemon\n"
            "  <b>/help</b> — эта справка\n\n"
            
            "<b>Память:</b>\n"
            "  <b>запомни это</b> — сохранить диалог в долговременную память (L3)\n"
            "  Сохраняет весь контекст: вопрос, ответ, историю\n"
            "  Используй для важных обсуждений, решений, идей\n\n"
            "  <b>запиши это</b> — сохранить конкретный факт (L2)\n"
            "  Пример: «запиши что мой телефон 123-456»\n"
            "  Бот вызовет инструмент memory_add_fact автоматически\n\n"
            "  <b>удали информацию про X</b> — удалить из памяти\n"
            "  <b>проиндексируй память</b> — сгруппировать чанки по темам\n\n"
            
            "<b>Настройки:</b>\n"
            "  <b>модели</b> — выбрать smart и cheap модели (кнопки)\n"
            "  <b>настройки</b> — расписание, брифинг, авто-очистка\n"
            "  <b>брифинг вкл</b> / <b>брифинг выкл</b> — утренний дайджест\n"
            "  <b>чистка вкл</b> / <b>чистка выкл</b> — авто-очистка БД\n\n"
            
            "<b>Режимы:</b>\n"
            "  <b>газ в пол</b> — GOD MODE: все блокировки сняты\n"
            "  Можно выполнять systemctl, sudo, rm — что угодно\n"
            "  <b>god mode off</b> — выключить GOD MODE\n"
            "  <b>перезапусти демона</b> — перезапуск daemon без обновления\n\n"
            
            "<b>Команды терминала (через бота):</b>\n"
            "  <b>выполни команду X</b> — выполнить shell команду\n"
            "  <b>покажи логи</b> — journalctl caesar-daemon\n"
            "  <b>что в файле X</b> — cat X\n\n"
            
            "Остальное — просто пиши задачу, я выполню."
        )
    
    async def _get_status_text(self, chat_id: int = 0) -> str:
        """Текст статуса для /status команды.
        
        ВАЖНО: вызываем generate_status_report НАПРЯМУЮ через self.storage,
        БЕЗ socket roundtrip. Раньше ходили через socket к daemon — но
        если daemon занят (dream cycle, L3 save) socket таймаутил.
        TG adapter уже имеет storage и queue — нет смысла ходить через socket.
        """
        if not self.storage:
            return "⚠️ Storage недоступен"
        
        try:
            from caesar.core.status import generate_status_report, format_status_text
            
            # Version
            try:
                from caesar import __version__
                version = __version__
            except Exception:
                version = "unknown"
            
            # Вызываем напрямую — без socket, без таймаута
            report = generate_status_report(
                storage=self.storage,
                queue=self.queue,
                version=version,
                uptime_seconds=None,  # uptime неизвестен без daemon
                user_id="",  # все пользователи
            )
            
            # Добавляем workers info + TG sessions
            if self.queue:
                report.setdefault("daemon", {})["workers"] = {
                    "interactive_active": self.queue.get_active_count("interactive"),
                    "interactive_max": 5,
                    "background_active": self.queue.get_active_count("background"),
                    "background_max": 10,
                    "interactive_pending": self.queue.get_pending_count("interactive"),
                    "background_pending": self.queue.get_pending_count("background"),
                }
            # TG sessions — сколько активных чатов
            report["daemon"]["tg_sessions"] = len(self._sessions)
            # Модели
            report["daemon"]["smart_model"] = self.config.llm.smart_model
            report["daemon"]["cheap_model"] = self.config.llm.cheap_model or "авто"
            # Контекст текущей сессии
            if chat_id in self._sessions:
                session = self._sessions[chat_id]
                try:
                    msgs = self.storage.get_messages(session.channel_id, limit=20)
                    total_chars = sum(len(m.get("content", "") or "") for m in msgs)
                    # Грубая оценка: русский ~2 символа/токен, английский ~4
                    # Берём среднее ~3 символа/токен + system prompt ~3000 + tools ~4000
                    history_tokens = total_chars // 3
                    system_tokens = 3000
                    tools_tokens = 4000
                    total_est = history_tokens + system_tokens + tools_tokens
                    report["daemon"]["context"] = {
                        "messages": len(msgs),
                        "history_tokens": history_tokens,
                        "system_tokens": system_tokens,
                        "tools_tokens": tools_tokens,
                        "total_tokens": total_est,
                    }
                except Exception:
                    pass
            
            text = format_status_text(report)
            # TG лимит 4096 — обрезаем если длинно
            if len(text) > 3500:
                text = text[:3500] + "\n\n... (обрезано)"
            return text
        except Exception as e:
            self.log.exception(f"Status generation failed: {e}")
            # Fallback на простой формат
            return (
                f"⚠️ Не удалось сгенерировать статус: {e}\n"
                f"Активных сессий: {len(self._sessions)}\n"
                f"Interactive workers: {self.queue.get_active_count('interactive')}/5"
            )
    
    async def _handle_tg_models(self, chat_id: int) -> None:
        """Команда 'модели' — выбор smart и cheap моделей через inline кнопки.
        
        Показывает ВСЕ модели (по 20 за раз, с пагинацией).
        Кнопка 'оставить текущую' — пропустить выбор.
        """
        from caesar.config import Config as Cfg
        config = Cfg.load()
        
        if not config.llm.smart_api_key:
            await self._send_message(
                chat_id,
                "❌ Smart API key не настроен.\n"
                "Выполни: caesar setup"
            )
            return
        
        await self._send_message(chat_id, "🔄 Загружаю список доступных моделей...")
        
        # Получаем список моделей
        from caesar.core.llm import OpenAICompatibleProvider
        provider = OpenAICompatibleProvider(config.llm, "smart")
        models = await provider.list_models()
        
        if not models:
            await self._send_message(
                chat_id,
                "⚠️ Не удалось получить список моделей (endpoint /v1/models недоступен).\n"
                "Используй: caesar models (в терминале)"
            )
            return
        
        # Сохраняем полный список в сессии для пагинации
        if chat_id in self._sessions:
            self._sessions[chat_id]._model_list = models
            self._sessions[chat_id]._model_role = "smart"
        
        await self._show_models_page(chat_id, models, "smart", 0)
    
    async def _show_models_page(self, chat_id: int, models: list[str], role: str, page: int) -> None:
        """Показать страницу моделей (по 20, с пагинацией).
        
        Args:
            models: полный список моделей
            role: 'smart' или 'cheap'
            page: номер страницы (0-based)
        """
        from caesar.config import Config as Cfg
        config = Cfg.load()
        
        per_page = 20
        total = len(models)
        total_pages = (total + per_page - 1) // per_page
        start = page * per_page
        end = min(start + per_page, total)
        page_models = models[start:end]
        
        current_model = config.llm.smart_model if role == "smart" else (config.llm.cheap_model or "")
        role_icon = "🧠" if role == "smart" else "💰"
        role_name = "SMART (основная)" if role == "smart" else "CHEAP (экономия)"
        
        # Строим inline keyboard
        buttons = []
        
        # Кнопка "оставить текущую" — первая
        if current_model:
            buttons.append([{
                "text": f"✅ Оставить текущую: {current_model}",
                "callback_data": f"model:keep:{role}",
            }])
        
        # Модели
        for m in page_models:
            marker = " ←" if m == current_model else ""
            buttons.append([{
                "text": f"{role_icon} {m}{marker}",
                "callback_data": f"model:{role}:{m[:52]}",
            }])
        
        # Навигация: назад / страница / вперёд
        nav_buttons = []
        if page > 0:
            nav_buttons.append({
                "text": "⬅️ Назад",
                "callback_data": f"model:page:{role}:{page - 1}",
            })
        nav_buttons.append({
            "text": f"{page + 1}/{total_pages}",
            "callback_data": "model:nop",
        })
        if page < total_pages - 1:
            nav_buttons.append({
                "text": "Вперёд ➡️",
                "callback_data": f"model:page:{role}:{page + 1}",
            })
        if nav_buttons:
            buttons.append(nav_buttons)
        
        keyboard = {"inline_keyboard": buttons}
        
        await self._send_message(
            chat_id,
            f"{role_icon} Выбор {role_name} модели:\n"
            f"Текущая: {current_model or 'не настроена'}\n"
            f"Всего: {total} моделей (страница {page + 1} из {total_pages})\n\n"
            f"Нажми модель или 'оставить текущую':",
            reply_markup=keyboard,
        )
    
    async def _handle_model_callback(self, callback: dict) -> None:
        """Обработка нажатия на кнопку выбора модели."""
        callback_id = callback.get("id")
        data = callback.get("data", "")
        message = callback.get("message", {})
        chat_id = message.get("chat", {}).get("id")
        
        # Acknowledge
        await self._api_call("answerCallbackQuery", {"callback_query_id": callback_id})
        
        # model:nop — нажали на номер страницы, ничего не делаем
        if data == "model:nop":
            return
        
        # model:page:smart:2 — пагинация
        if data.startswith("model:page:"):
            parts = data.split(":")
            if len(parts) == 4:
                role = parts[2]
                page = int(parts[3])
                # Берём сохранённый список моделей из сессии
                if chat_id in self._sessions:
                    models = getattr(self._sessions[chat_id], "_model_list", [])
                    if models:
                        await self._show_models_page(chat_id, models, role, page)
            return
        
        # model:keep:smart или model:keep:cheap — оставить текущую
        if data.startswith("model:keep:"):
            role = data.split(":")[2]
            from caesar.config import Config as Cfg
            config = Cfg.load()
            current = config.llm.smart_model if role == "smart" else (config.llm.cheap_model or "")
            await self._send_message(chat_id, f"✅ {role.upper()} модель осталась: {current}")
            
            # Если smart — предлагаем выбрать cheap
            if role == "smart":
                await self._offer_cheap_selection(chat_id)
            return
        
        # model:smart:model_name или model:cheap:model_name
        parts = data.split(":", 2)
        if len(parts) != 3:
            return
        
        _, role, model_name = parts
        
        from caesar.config import Config as Cfg
        config = Cfg.load()
        
        if role == "smart":
            config.llm.smart_model = model_name
            await self._send_message(chat_id, f"✅ Smart модель: {model_name}")
        elif role == "cheap":
            config.llm.cheap_model = model_name
            config.llm.cheap_api_key = config.llm.smart_api_key
            config.llm.cheap_provider = config.llm.smart_provider
            config.llm.cheap_base_url = config.llm.smart_base_url
            await self._send_message(chat_id, f"✅ Cheap модель: {model_name}")
        
        config.save()
        
        # Если выбрали smart — предлагаем выбрать cheap
        if role == "smart":
            await self._offer_cheap_selection(chat_id)
    
    async def _offer_cheap_selection(self, chat_id: int) -> None:
        """После выбора smart — предлагаем выбрать cheap модель."""
        from caesar.config import Config as Cfg
        from caesar.core.llm import OpenAICompatibleProvider
        config = Cfg.load()
        
        provider = OpenAICompatibleProvider(config.llm, "smart")
        models = await provider.list_models()
        
        if not models:
            await self._send_message(chat_id, "⚠️ Не удалось загрузить список для cheap модели.")
            return
        
        # Сохраняем список для пагинации
        if chat_id in self._sessions:
            self._sessions[chat_id]._model_list = models
            self._sessions[chat_id]._model_role = "cheap"
        
        await self._show_models_page(chat_id, models, "cheap", 0)
    
    async def _handle_stop_task(self, chat_id: int) -> None:
        """Экстренная остановка текущей задачи для этого чата.
        
        Отменяет все активные задачи в очереди для этого channel_id.
        Используется когда агент зациклился и не отвечает.
        """
        await self._send_message(chat_id, "⏹️ Останавливаю текущую задачу...")
        
        # Получаем session чтобы знать channel_id
        session = self._sessions.get(chat_id)
        if not session:
            await self._send_message(chat_id, "ℹ️ Нет активной сессии — нечего останавливать.")
            return
        
        channel_id = session.channel_id
        
        # Удаляем карточку прогресса если есть
        if session.progress_message_id:
            await self._api_call("deleteMessage", {
                "chat_id": chat_id,
                "message_id": session.progress_message_id,
            })
            session.progress_message_id = None
            session.icons_sequence = []
        
        # Находим и отменяем активные задачи для этого channel_id
        stopped_count = 0
        if self.queue:
            # Помечаем все активные задачи этого канала как FAILED
            for task in list(self.queue._tasks.values()):
                if task.channel_id == channel_id and task.status.value in ("running", "pending", "waiting_for_user"):
                    try:
                        from caesar.core.queue import TaskStatus
                        task.status = TaskStatus.FAILED
                        task.cancelled = True
                        task.error = "Остановлено пользователем (/stop)"
                        stopped_count += 1
                        self.log.info(f"Stopped task {task.id}: '{task.user_message[:50]}...'")
                    except Exception as e:
                        self.log.warning(f"Failed to stop task {task.id}: {e}")
        
        if stopped_count > 0:
            await self._send_message(
                chat_id,
                f"✅ Остановлено задач: {stopped_count}\n"
                f"Можешь продолжить — я готов к новым вопросам.",
            )
        else:
            await self._send_message(
                chat_id,
                "ℹ️ Активных задач для этого чата не найдено.\n"
                "Возможно задача уже завершилась или идёт в другом чате.",
            )
    
    async def _handle_tg_update(self, chat_id: int) -> None:
        """Обновить Caesar через Telegram.
        
        Запускает caesar update -y как subprocess.
        НЕ перезапускает daemon (это убило бы TG-бот).
        Вместо этого после обновления шлёт systemctl restart.
        
        Graceful: перед restart ждём завершения активных задач
        (через socket API к daemon). Незавершённые сохранятся в БД
        и подхватятся после рестарта.
        """
        await self._send_message(chat_id, "🔄 Обновляю Caesar...")
        
        # Находим caesar binary — через venv python
        from pathlib import Path
        import os as _os
        venv_python = str(Path.home() / ".local/share/caesar/venv/bin/python")
        if not Path(venv_python).exists():
            venv_python = "python3"  # fallback
        
        # Передаём chat_id через env var — management.py пишет его в
        # /tmp/caesar-restart-chat-id, а новый daemon при старте отправит
        # "✅ готово" через _notify_restart_complete.
        # Также передаём XDG_RUNTIME_DIR чтобы systemctl --user работал.
        daemon_uid = _os.getuid()
        xdg_runtime_dir = f"/run/user/{daemon_uid}"
        update_env = {
            **_os.environ,
            "CAESAR_TG_CHAT_ID": str(chat_id),
            "XDG_RUNTIME_DIR": xdg_runtime_dir,
            "DBUS_SESSION_BUS_ADDRESS": f"unix:path={xdg_runtime_dir}/bus",
        }
        
        # Запускаем update БЕЗ перезапуска daemon (--no-restart).
        # Рестарт делает сам subprocess (в cmd_update) через setsid + bash —
        # это отвязанный процесс который переживает смерть daemon.
        try:
            proc = await asyncio.create_subprocess_exec(
                venv_python, "-m", "caesar.management", "update", "-y", "--no-restart",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=update_env,
            )
            
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=180)
            output = stdout.decode("utf-8", errors="replace")[-1000:]
            
            if proc.returncode == 0:
                # НЕ добавляем свой заголовок — subprocess уже печатает
                # '✅ Код обновлён!' и '📋 Что нового'. Просто отправляем его вывод.
                await self._send_message(chat_id, output[-1500:].strip())
            else:
                error = stderr.decode("utf-8", errors="replace")[:300]
                await self._send_message(chat_id, f"⚠️ Ошибка:\n\n{error}")
                return
        except asyncio.TimeoutError:
            await self._send_message(chat_id, "❌ Таймаут (180 сек)")
            return
        except Exception as e:
            await self._send_message(chat_id, f"❌ {e}")
            return
        
        # Graceful: ждём завершения активных задач перед restart.
        # Рестарт делает сам subprocess cmd_update (через setsid+bash) —
        # тут только ждём активных задач.
        active_count = await self._get_active_tasks_count()
        if active_count > 0:
            await self._send_message(
                chat_id,
                f"⏳ {active_count} активных задач — жду завершения перед рестартом..."
            )
            # Ждём до 3 минут
            start = time.time()
            while time.time() - start < 180:
                await asyncio.sleep(5)
                count = await self._get_active_tasks_count()
                if count == 0:
                    await self._send_message(chat_id, "✅ Все задачи завершены.")
                    break
            else:
                await self._send_message(
                    chat_id,
                    "⚠️ Не дождались — сохраняю задачи в БД и перезапускаю. "
                    "Они подхватятся после рестарта."
                )
        
        # Рестарт уже запланирован самим subprocess cmd_update через setsid+bash.
        # Не дублируем сообщение — оно уже в выводе cmd_update выше.
        self.log.info(f"Update completed for chat_id={chat_id}, restart scheduled by subprocess")
    
    async def _handle_tg_show_update_log(self, chat_id: int) -> None:
        """Показать лог рестарта для дебага обновлений.
        
        Читает /tmp/caesar-tg-ready.log и /tmp/caesar-update.log
        (если существуют) и отправляет пользователю.
        """
        import subprocess
        
        log_parts = []
        
        # 1. Лог bash-скрипта рестарта (/tmp/caesar-tg-ready.log)
        try:
            result = subprocess.run(
                ["cat", "/tmp/caesar-tg-ready.log"],
                capture_output=True, text=True, timeout=3,
            )
            if result.stdout.strip():
                log_parts.append(("📋 Лог рестарта (/tmp/caesar-tg-ready.log)", result.stdout[-2000:]))
        except Exception as e:
            log_parts.append(("📋 /tmp/caesar-tg-ready.log", f"(не удалось прочитать: {e})"))
        
        # 2. Последние строки из journalctl для caesar-daemon
        try:
            result = subprocess.run(
                ["journalctl", "--user", "-u", "caesar-daemon",
                 "--since", "30 min ago", "-n", "30", "--no-pager"],
                capture_output=True, text=True, timeout=5,
            )
            if result.stdout.strip():
                log_parts.append(("📋 journalctl caesar-daemon (30 min, last 30)", result.stdout[-2000:]))
        except Exception as e:
            log_parts.append(("📋 journalctl", f"(не удалось прочитать: {e})"))
        
        if not log_parts:
            await self._send_message(chat_id, "ℹ️ Логи пусты — обновлений не было.")
            return
        
        # Отправляем по частям (TG лимит 4096 символов)
        for title, content in log_parts:
            text = f"{title}:\n\n{content}"
            # Обрезаем если слишком длинно
            if len(text) > 3500:
                text = text[:3500] + "\n\n... (обрезано)"
            await self._send_message(chat_id, text)
    
    async def _handle_tg_index_memory(self, chat_id: int) -> None:
        """Команда 'проиндексируй память' — запустить topic consolidation.
        
        Через socket API просим daemon запустить DreamCycle с параметром
        force_topic_consolidation=True. Это сгруппирует L3 чанки по темам
        и создаст consolidated summaries.
        """
        from caesar.config import SOCKET_PATH
        import json as json_mod
        import socket as socket_mod
        
        # Проверяем — вдруг уже идёт индексация (файл-флаг)
        # TODO: можно через socket get_status, но пока просто шлём запрос.
        
        if not SOCKET_PATH.exists():
            await self._send_message(
                chat_id,
                "❌ Daemon не запущен (socket не найден). "
                "Запусти: systemctl --user start caesar-daemon",
            )
            return
        
        await self._send_message(
            chat_id,
            "🧠 Запускаю индексацию памяти...\n"
            "⏱️ Это может занять 1-3 минуты (зависит от количества чанков). "
            "Я напишу когда закончу.",
        )
        
        # Запрос к daemon через socket
        try:
            sock = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
            sock.settimeout(300)  # 5 минут максимум — индексация может быть долгой
            sock.connect(str(SOCKET_PATH))
            
            request = {
                "action": "index_memory",
                "force_all": True,
                "topic_only": True,
            }
            sock.sendall((json_mod.dumps(request) + "\n").encode())
            
            # Читаем ответ (может быть длинным — отчёт со статистикой)
            response_data = b""
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                response_data += chunk
                # Если уже прочитали newline — это конец ответа
                if b"\n" in chunk:
                    break
            
            sock.close()
            
            data = json_mod.loads(response_data.decode())
        except socket_mod.timeout:
            await self._send_message(
                chat_id,
                "⏰ Индексация занимает слишком долго (>5 мин). "
                "Проверь статус через /status — возможно всё ещё работает.",
            )
            return
        except Exception as e:
            await self._send_message(chat_id, f"❌ Ошибка: {e}")
            return
        
        if "error" in data:
            await self._send_message(
                chat_id,
                f"❌ Ошибка индексации: {data.get('error')}\n"
                f"{data.get('message', '')}",
            )
            return
        
        # Форматируем отчёт
        topics = data.get("topics_consolidated", 0)
        chunks_created = data.get("chunks_created", 0)
        chunks_processed = data.get("chunks_processed", 0)
        duration = data.get("duration_sec", 0)
        
        if topics == 0 and chunks_processed == 0:
            await self._send_message(
                chat_id,
                "ℹ️ Индексировать нечего — нет новых чанков для консолидации.\n"
                "Возможно, всё уже было консолидировано ранее.",
            )
            return
        
        msg = (
            "✅ Индексация памяти завершена.\n\n"
            f"📊 Результат:\n"
            f"  • Тем консолидировано: {topics}\n"
            f"  • Consolidated чанков создано: {chunks_created}\n"
            f"  • Исходных чанков обработано: {chunks_processed}\n"
            f"  • Время: {duration:.1f} сек\n\n"
            f"Теперь при вопросах по этим темам бот будет находить "
            f"укрупнённые саммари вместо множества мелких чанков."
        )
        await self._send_message(chat_id, msg)
    
    async def _get_active_tasks_count(self) -> int:
        """Запросить у daemon количество активных задач через socket."""
        from caesar.config import SOCKET_PATH
        import json as json_mod
        import socket as socket_mod
        
        if not SOCKET_PATH.exists():
            return 0
        try:
            sock = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
            sock.settimeout(2)
            sock.connect(str(SOCKET_PATH))
            sock.sendall((json_mod.dumps({"action": "list_tasks"}) + "\n").encode())
            response = sock.recv(65536).decode()
            sock.close()
            data = json_mod.loads(response)
            return len(data.get("active", []))
        except Exception:
            return 0
