"""Тесты S1 — видимость самообучения (self-update notifications).

Покрытие:
- note_self_update: эмиссия info_notification с «🧠 ...» через event_bus.
- без emit_key / без event_bus — не падает.
- memory_add_fact tool: принимает + передаёт category в storage (иначе перехват
  в оркестраторе никогда бы не сработал — category всегда был бы 'fact').
"""
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from caesar.core.events import EVENT_INFO_NOTIFICATION
from caesar.core.orchestrator import Orchestrator
from caesar.memory.storage import Storage
from caesar.tools.memory_tools import MemoryAddFactTool


def _storage() -> Storage:
    return Storage(db_path=Path(tempfile.mkdtemp()) / "t.db")


# --- note_self_update ---

async def test_note_self_update_emits_info_notification():
    bus = MagicMock(); bus.emit = AsyncMock()
    stub = SimpleNamespace(event_bus=bus, log=MagicMock())
    await Orchestrator.note_self_update(stub, "запомнил [decision]: db = postgres", emit_key="ch1")
    bus.emit.assert_called_once()
    args = bus.emit.call_args.args
    assert args[0] == "ch1"
    event = args[1]
    assert event.type == EVENT_INFO_NOTIFICATION
    assert "🧠" in event.data["message"]
    assert "decision" in event.data["message"]


async def test_note_self_update_no_emit_key_no_emit():
    bus = MagicMock(); bus.emit = AsyncMock()
    stub = SimpleNamespace(event_bus=bus, log=MagicMock())
    await Orchestrator.note_self_update(stub, "test", emit_key="")
    bus.emit.assert_not_called()


async def test_note_self_update_no_event_bus_no_crash():
    stub = SimpleNamespace(event_bus=None, log=MagicMock())
    await Orchestrator.note_self_update(stub, "test", emit_key="ch1")  # не падает


# --- memory_add_fact tool passes category ---

async def test_memory_add_fact_tool_passes_category():
    s = _storage()
    tool = MemoryAddFactTool(s, channel_id="channel:main", user_id="u", author_id="u")
    r = await tool.execute(
        entity="db", attribute="choice", value="postgres",
        source_quote="решили использовать postgres", category="decision",
    )
    assert r.success
    fact = s.get_facts("u", "main")[0]
    assert fact["category"] == "decision"


async def test_memory_add_fact_tool_default_category_fact():
    s = _storage()
    tool = MemoryAddFactTool(s, channel_id="channel:main", user_id="u", author_id="u")
    await tool.execute(entity="x", attribute="y", value="z", source_quote="...")
    assert s.get_facts("u", "main")[0]["category"] == "fact"
