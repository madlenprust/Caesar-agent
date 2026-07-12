"""TTL-чистка неактивных TG-сессий — регресс-тест на memory leak.

Раньше self._sessions и подписки event_bus росли безгранично для каждого
чата, который хоть раз написал боту; _session_ttl_seconds был настроен, но
никогда не использовался. _prune_expired_sessions() исправляет это.
"""
import time
from unittest.mock import MagicMock

from caesar.channels.telegram_adapter import TelegramAdapter, TgSession
from caesar.core.events import EventBus


def _make_adapter(ttl: int = 86400) -> TelegramAdapter:
    config = MagicMock()
    config.telegram.bot_token = ""  # TG выключен — start() бы вернулся
    adapter = TelegramAdapter(config, EventBus(), MagicMock(), storage=None)
    adapter._session_ttl_seconds = ttl
    return adapter


def _session(adapter, chat_id, age_seconds):
    s = TgSession(chat_id=chat_id, user_id_tg=chat_id * 100, channel_id=f"tg:{chat_id}")
    s.last_activity = time.time() - age_seconds
    s._event_handler = lambda *a, **k: None
    adapter._sessions[chat_id] = s
    return s


def test_prune_removes_expired_session():
    adapter = _make_adapter(ttl=60)
    _session(adapter, chat_id=1, age_seconds=120)
    adapter._last_documents[1] = {"file_name": "x"}

    n = adapter._prune_expired_sessions()

    assert n == 1
    assert 1 not in adapter._sessions
    assert 1 not in adapter._last_documents


def test_prune_keeps_active_session():
    adapter = _make_adapter(ttl=3600)
    _session(adapter, chat_id=2, age_seconds=10)  # свежая

    n = adapter._prune_expired_sessions()

    assert n == 0
    assert 2 in adapter._sessions


def test_prune_unsubscribes_event_handler():
    """Истёкшая сессия должна отписать свой handler от event_bus."""
    adapter = _make_adapter(ttl=60)
    s = _session(adapter, chat_id=3, age_seconds=120)
    adapter.event_bus.subscribe("3", s._event_handler)

    adapter._prune_expired_sessions()

    assert 3 not in adapter._sessions
    # второй прогон ничего не должен найти — значит отписка прошла
    assert adapter._prune_expired_sessions() == 0


def test_prune_empty_is_noop():
    adapter = _make_adapter(ttl=1)
    assert adapter._prune_expired_sessions() == 0
