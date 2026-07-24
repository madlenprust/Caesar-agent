"""Тесты T1 — L2 категоризация (Memory Transparency).

Покрытие:
- миграция: колонка category в l2_facts (DEFAULT 'fact').
- add_fact: дефолт 'fact', задаётся, invalid→'fact' (clamp), supersede несёт новую категорию.
- extract_facts: LLM-классификация + clamp неизвестных → 'fact' + фильтр low-confidence.
- briefing: секции Решения/Победы/Инциденты за неделю.
"""
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from caesar.config import Config
from caesar.core.briefing import MorningBriefing
from caesar.core.llm import LLMRouter
from caesar.memory.storage import Storage


def _storage() -> Storage:
    return Storage(db_path=Path(tempfile.mkdtemp()) / "t.db")


def _cols(storage, table: str) -> set:
    with storage._conn() as c:
        return {r["name"] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}


# --- миграция ---

def test_l2_facts_has_category_column():
    s = _storage()
    assert "category" in _cols(s, "l2_facts")


def test_existing_facts_default_to_fact_on_migration():
    """На свежей БД все факты — 'fact' (дефолт колонки)."""
    s = _storage()
    s.add_fact(user_id="u", channel="c", entity="e", attribute="a", value="v")
    with s._conn() as c:
        row = c.execute("SELECT category FROM l2_facts WHERE user_id='u'").fetchone()
    assert dict(row)["category"] == "fact"


# --- add_fact ---

def test_add_fact_default_category():
    s = _storage()
    s.add_fact(user_id="u", channel="c", entity="e", attribute="a", value="v")
    assert s.get_facts("u", "c")[0]["category"] == "fact"


def test_add_fact_with_category():
    s = _storage()
    s.add_fact(user_id="u", channel="c", entity="e", attribute="a", value="v", category="decision")
    assert s.get_facts("u", "c")[0]["category"] == "decision"


def test_add_fact_invalid_category_clamped():
    s = _storage()
    s.add_fact(user_id="u", channel="c", entity="e", attribute="a", value="v", category="nonsense")
    assert s.get_facts("u", "c")[0]["category"] == "fact"


def test_supersede_carries_new_category():
    """Supersede: новый факт получает свою категорию (не наследует старую)."""
    s = _storage()
    s.add_fact(user_id="u", channel="c", entity="e", attribute="a", value="v1", category="fact")
    s.add_fact(user_id="u", channel="c", entity="e", attribute="a", value="v2", category="decision")
    active = [f for f in s.get_facts("u", "c")]  # get_facts возвращает только active
    assert len(active) == 1
    assert active[0]["value"] == "v2"
    assert active[0]["category"] == "decision"


# --- extract_facts ---

def _router_with_chat(json_content: str):
    stub = SimpleNamespace()
    stub.cheap = MagicMock()
    stub.cheap.chat = AsyncMock(return_value=SimpleNamespace(content=json_content))
    stub.log = MagicMock()
    return stub


async def test_extract_facts_classifies_and_clamps():
    payload = json.dumps([
        {"entity": "db", "attribute": "choice", "value": "postgres",
         "category": "decision", "confidence": "high", "source_quote": "..."},
        {"entity": "rel", "attribute": "status", "value": "shipped",
         "category": "bogus", "confidence": "medium"},          # invalid → fact
        {"entity": "x", "attribute": "y", "value": "z",
         "category": "win", "confidence": "low"},                # low → filtered
        {"entity": "api", "attribute": "outage", "value": "2h",
         "confidence": "high"},                                   # no category → fact
    ])
    stub = _router_with_chat(payload)
    facts = await LLMRouter.extract_facts(stub, "dialog text")
    cats = sorted(f["category"] for f in facts)
    assert cats == ["decision", "fact", "fact"]  # bogus→fact, low-filtered, missing→fact
    assert len(facts) == 3


async def test_extract_facts_empty_array():
    stub = _router_with_chat("[]")
    assert await LLMRouter.extract_facts(stub, "nothing here") == []


# --- briefing recent events ---

async def test_briefing_recent_events_sections():
    s = _storage()
    s.add_fact(user_id="u1", channel="c", entity="db", attribute="choice", value="postgres", category="decision")
    s.add_fact(user_id="u1", channel="c", entity="rel", attribute="status", value="shipped", category="win")
    s.add_fact(user_id="u1", channel="c", entity="api", attribute="outage", value="2h", category="incident")
    bus = MagicMock(); bus.emit = AsyncMock()
    b = MorningBriefing(config=Config(), storage=s, event_bus=bus)
    text = await b.generate_and_send(user_id="u1", channel_id="111")
    assert "Решения (1)" in text
    assert "Победы (1)" in text
    assert "Инциденты (1)" in text
    assert "postgres" in text


async def test_briefing_no_events_no_section():
    s = _storage()
    s.add_fact(user_id="u1", channel="c", entity="e", attribute="a", value="v")  # category=fact (не событие)
    bus = MagicMock(); bus.emit = AsyncMock()
    b = MorningBriefing(config=Config(), storage=s, event_bus=bus)
    text = await b.generate_and_send(user_id="u1", channel_id="111")
    assert "Решения" not in text  # нет событий — секции нет
