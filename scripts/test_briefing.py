"""Тесты MorningBriefing.generate_and_send (audit medium-fix: routing).

Покрытие:
- нет данных → дефолтный «Доброе утро» + emit в event_bus.
- есть held + dream → секции в тексте + emit.
- emit уходит в ПЕРЕДАННЫЙ channel_id (routing) — основа fix-а «briefing не первому
  TG-юзеру, а нужному» (daemon 0.10.1 передаёт per-user channel_id).
"""
import json
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from caesar.config import Config
from caesar.core.briefing import MorningBriefing
from caesar.memory.storage import Storage


def _make_briefing() -> tuple[MorningBriefing, Path]:
    db = Path(tempfile.mkdtemp()) / "t.db"
    storage = Storage(db_path=db)
    bus = MagicMock()
    bus.emit = AsyncMock()
    b = MorningBriefing(config=Config(), storage=storage, event_bus=bus)
    return b, bus, db.parent


async def test_no_data_default_text_and_emit():
    """Пустое хранилище → дефолт «Доброе утро», event_bus.emit вызван."""
    b, bus, _ = _make_briefing()
    text = await b.generate_and_send(user_id="u1", channel_id="208118")
    assert "Доброе утро" in text
    bus.emit.assert_called_once()
    # emit в правильный channel_id (routing)
    args = bus.emit.call_args.args
    assert args[0] == "208118"


async def test_held_and_dream_sections_present():
    """Есть held_notifications + dream_report → обе секции в тексте + emit."""
    b, bus, data_dir = _make_briefing()
    (data_dir / "held_notifications.json").write_text(json.dumps([
        {"task": "morning news digest", "held_at": "2026-07-24T01:00:00"},
    ]), encoding="utf-8")
    (data_dir / "last_dream_report.json").write_text(json.dumps({
        "timestamp": datetime.now().isoformat(),
        "entities_extracted": 5,
        "entities_enriched": 2,
        "duplicates_merged": 1,
        "citations_fixed": 3,
        "duration_sec": 12.0,
    }), encoding="utf-8")

    text = await b.generate_and_send(user_id="u1", channel_id="208118")

    assert "Отложенные уведомления" in text
    assert "morning news digest" in text
    assert "Dream Cycle" in text
    assert "Новых сущностей: 5" in text
    bus.emit.assert_called_once()
    assert bus.emit.call_args.args[0] == "208118"


async def test_stale_dream_report_ignored():
    """Dream-отчёт старше 12ч → секция НЕ добавляется (не релевантно)."""
    b, bus, data_dir = _make_briefing()
    stale = (datetime.now().timestamp() - 13 * 3600)  # 13 часов назад
    from datetime import datetime as _dt
    stale_iso = _dt.fromtimestamp(stale).isoformat()
    (data_dir / "last_dream_report.json").write_text(json.dumps({
        "timestamp": stale_iso, "entities_extracted": 5,
    }), encoding="utf-8")
    text = await b.generate_and_send(user_id="u1", channel_id="208118")
    assert "Dream Cycle" not in text


async def test_no_event_bus_no_crash():
    """Без event_bus (None) — не падает, просто не emit'ит."""
    db = Path(tempfile.mkdtemp()) / "t.db"
    storage = Storage(db_path=db)
    b = MorningBriefing(config=Config(), storage=storage, event_bus=None)
    text = await b.generate_and_send(user_id="u1", channel_id="208118")
    assert "Доброе утро" in text
