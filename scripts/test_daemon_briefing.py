"""Тесты daemon._run_morning_briefing (audit medium-fix: routing каждому TG-юзеру).

Fix (0.10.1): раньше briefing шёл первому попавшемуся TG-каналу (LIMIT 1 без ORDER BY)
и генерировался для CLI-юзера → уходил не тому. Теперь — каждому юзеру с активным
TG-каналом, со своим user_id. Покрытие:
- 2 TG-канала → generate_and_send зовётся 2 раза, каждому со своим (user_id, channel_id).
- 0 TG-каналов → skip (generate_and_send не звался).
"""
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from caesar.config import Config
from caesar.daemon import AgentDaemon
from caesar.memory.storage import Storage


def _stub_with_channels(channels: list[tuple[str, str]]):
    """temp Storage с TG-каналами + stub daemon (config/storage/event_bus/log)."""
    db = Path(tempfile.mkdtemp()) / "t.db"
    storage = Storage(db_path=db)
    for user_id, chat_id in channels:
        storage.upsert_channel(
            channel_id=f"ch-{user_id}", user_id=user_id, source="telegram",
            source_chat_id=chat_id, display_name="main",
        )
    return SimpleNamespace(config=Config(), storage=storage,
                           event_bus=MagicMock(), log=MagicMock())


async def test_briefing_sent_to_each_tg_user():
    """2 активных TG-канала → briefing каждому, со своим user_id + channel_id."""
    stub = _stub_with_channels([("u-a", "111"), ("u-b", "222")])
    with patch("caesar.core.briefing.MorningBriefing") as MB:
        MB.return_value.generate_and_send = AsyncMock(return_value="briefing text")
        await AgentDaemon._run_morning_briefing(stub)
    calls = MB.return_value.generate_and_send.call_args_list
    assert len(calls) == 2
    pairs = sorted((c.kwargs["user_id"], c.kwargs["channel_id"]) for c in calls)
    assert pairs == [("u-a", "111"), ("u-b", "222")]


async def test_no_tg_channels_skips_briefing():
    """Нет активных TG-каналов → skip, generate_and_send не звался."""
    stub = _stub_with_channels([])  # 0 каналов
    with patch("caesar.core.briefing.MorningBriefing") as MB:
        MB.return_value.generate_and_send = AsyncMock()
        await AgentDaemon._run_morning_briefing(stub)
    MB.return_value.generate_and_send.assert_not_called()


async def test_inactive_tg_channel_skipped():
    """Не-active TG-канал не получает briefing (фильтр status='active')."""
    stub = _stub_with_channels([("u-a", "111")])
    # пометим канал закрытым
    with stub.storage._conn() as c:
        c.execute("UPDATE channels SET status='closed' WHERE id='ch-u-a'")
    with patch("caesar.core.briefing.MorningBriefing") as MB:
        MB.return_value.generate_and_send = AsyncMock()
        await AgentDaemon._run_morning_briefing(stub)
    MB.return_value.generate_and_send.assert_not_called()
