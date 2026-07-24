"""Тесты T2 — Mind Mirror (Memory Transparency).

Покрытие:
- export(): структура auto/ (README, facts.md, entities/<name>.md, decisions/wins/incidents),
  счётчики; KG-сущности + relations как wikilinks.
- export() идемпотентен (повторный → без дублей, stable counts).
- load_manual_context(): читает manual/*.md с заголовками; пусто → ''; бюджет max_chars.
- Orchestrator._build_system_prompt инжектит manual overlay (wiring T2.4).
"""
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from caesar.core.orchestrator import Orchestrator
from caesar.core.queue import Task
from caesar.memory.mind_mirror import MindMirror, _safe_filename
from caesar.memory.storage import Storage


def _storage_with_data() -> tuple[Storage, Path]:
    s = Storage(db_path=Path(tempfile.mkdtemp()) / "t.db")
    # L2 facts — разные категории
    s.add_fact(user_id="u1", channel="c", entity="Postgres", attribute="choice",
               value="use as main db", category="decision")
    s.add_fact(user_id="u1", channel="c", entity="Release", attribute="status",
               value="shipped v1", category="win")
    s.add_fact(user_id="u1", channel="c", entity="API", attribute="outage",
               value="2h on Tue", category="incident")
    s.add_fact(user_id="u1", channel="c", entity="Postgres", attribute="version",
               value="16", category="fact")
    # KG entity + relation
    with s._conn() as c:
        c.execute("INSERT INTO kg_entities (id, user_id, name, entity_type, mention_count) "
                  "VALUES (?,?,?,?,?)", ("e1", "u1", "Postgres", "technology", 3))
        c.execute("INSERT INTO kg_relations (id, user_id, from_entity, to_entity, relation_type) "
                  "VALUES (?,?,?,?,?)", ("r1", "u1", "Postgres", "API", "powers"))
        c.commit()
    return s, s.db_path.parent


# --- export structure ---

def test_export_creates_auto_structure():
    s, _ = _storage_with_data()
    mirror = MindMirror(s)
    counts = mirror.export()
    assert (mirror.auto / "README.md").exists()
    assert (mirror.auto / "facts.md").exists()
    assert (mirror.auto / "decisions.md").exists()
    assert (mirror.auto / "wins.md").exists()
    assert (mirror.auto / "incidents.md").exists()
    assert (mirror.auto / "entities").is_dir()
    assert counts["facts"] == 4
    assert counts["entities"] >= 2  # Postgres, Release, API
    assert counts["relations"] == 1


def test_export_entity_page_has_facts_and_wikilinks():
    s, _ = _storage_with_data()
    mirror = MindMirror(s)
    mirror.export()
    pg = mirror.auto / "entities" / f"{_safe_filename('Postgres')}.md"
    assert pg.exists()
    text = pg.read_text(encoding="utf-8")
    assert "use as main db" in text            # факт (decision)
    assert "version: 16" in text               # факт (fact)
    assert "[[API|API]]" in text or "[[API" in text  # relation wikilink


def test_export_idempotent():
    """Повторный export не дублирует (auto/ очищается)."""
    s, _ = _storage_with_data()
    mirror = MindMirror(s)
    c1 = mirror.export()
    c2 = mirror.export()
    assert c1 == c2
    # нет дублей файлов сущностей
    ents = list((mirror.auto / "entities").glob("*.md"))
    names = {f.name for f in ents}
    assert len(names) == len(ents)  # без дублей


def test_export_does_not_touch_manual():
    s, data_dir = _storage_with_data()
    mirror = MindMirror(s)
    manual_file = mirror.manual / "note.md"
    manual_file.parent.mkdir(parents=True, exist_ok=True)
    manual_file.write_text("do not delete me", encoding="utf-8")
    mirror.export()
    assert manual_file.exists()
    assert manual_file.read_text() == "do not delete me"


# --- load_manual_context ---

def test_load_manual_context_reads_files():
    s, _ = _storage_with_data()
    mirror = MindMirror(s)
    mirror.manual.mkdir(parents=True, exist_ok=True)
    (mirror.manual / "preferences.md").write_text("Юзер предпочитает Postgres", encoding="utf-8")
    (mirror.manual / "focus.md").write_text("Текущая задача: миграция на v2", encoding="utf-8")
    ctx = mirror.load_manual_context()
    assert "preferences (curated)" in ctx
    assert "focus (curated)" in ctx
    assert "Postgres" in ctx
    assert "миграция на v2" in ctx


def test_load_manual_context_empty():
    s, _ = _storage_with_data()
    mirror = MindMirror(s)
    assert mirror.load_manual_context() == ""


def test_load_manual_context_budget():
    s, _ = _storage_with_data()
    mirror = MindMirror(s)
    mirror.manual.mkdir(parents=True, exist_ok=True)
    (mirror.manual / "big.md").write_text("X" * 5000, encoding="utf-8")
    ctx = mirror.load_manual_context(max_chars=200)
    assert len(ctx) <= 210  # обрезан по бюджету (+хвост)
    assert "обрезано" in ctx


# --- orchestrator wiring (T2.4) ---

def test_orchestrator_injects_manual_overlay():
    """_build_system_prompt подхватывает manual/ overlay (high-priority блок)."""
    s, _ = _storage_with_data()
    mirror = MindMirror(s)
    mirror.manual.mkdir(parents=True, exist_ok=True)
    (mirror.manual / "always.md").write_text("Юзера зовут Alex", encoding="utf-8")
    stub = SimpleNamespace(storage=s, kg=None, log=MagicMock(), _mind_mirror=None)
    prompt = Orchestrator._build_system_prompt(
        stub, Task(), memory_context="", history_count=0, l3_context="")
    assert "ЗНАНИЯ ОТ ПОЛЬЗОВАТЕЛЯ" in prompt
    assert "Alex" in prompt


def test_orchestrator_no_manual_no_block():
    """Без manual/ — блока curated-знаний нет (prompt без 'ЗНАНИЯ ОТ ПОЛЬЗОВАТЕЛЯ')."""
    s, _ = _storage_with_data()
    stub = SimpleNamespace(storage=s, kg=None, log=MagicMock(), _mind_mirror=None)
    prompt = Orchestrator._build_system_prompt(
        stub, Task(), memory_context="", history_count=0, l3_context="")
    assert "ЗНАНИЯ ОТ ПОЛЬЗОВАТЕЛЯ" not in prompt
