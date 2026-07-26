"""Тесты T4 — caesar mind query + forget (inspectability + correction).

Покрытие:
- query: возвращает факты + relations для сущности; «Ничего не знаю» если нет.
- forget: помечает L2 facts superseded (valid_until=now); возвращает count.
- forget с attribute: только конкретный атрибут.
"""
import tempfile
from pathlib import Path

import pytest

from caesar.memory.mind_mirror import MindMirror
from caesar.memory.storage import Storage


def _storage_with_data() -> tuple[Storage, Path]:
    s = Storage(db_path=Path(tempfile.mkdtemp()) / "t.db")
    s.add_fact(user_id="u1", channel="c", entity="Postgres", attribute="version",
               value="16", category="fact")
    s.add_fact(user_id="u1", channel="c", entity="Postgres", attribute="choice",
               value="use as main db", category="decision")
    s.add_fact(user_id="u1", channel="c", entity="API", attribute="outage",
               value="2h on Tue", category="incident")
    # KG entity + relation
    with s._conn() as c:
        c.execute("INSERT INTO kg_entities (id, user_id, name, entity_type, mention_count) "
                  "VALUES (?,?,?,?,?)", ("e1", "u1", "Postgres", "technology", 3))
        c.execute("INSERT INTO kg_relations (id, user_id, from_entity, to_entity, relation_type) "
                  "VALUES (?,?,?,?,?)", ("r1", "u1", "Postgres", "API", "powers"))
        c.commit()
    return s


# --- query ---

def test_query_returns_facts_and_relations():
    s = _storage_with_data()
    mirror = MindMirror(s)
    result = mirror.query("Postgres")
    assert "Что я знаю про «Postgres»" in result
    assert "version: 16" in result
    assert "use as main db" in result
    assert "[decision]" in result
    assert "→ powers → API" in result  # KG relation


def test_query_unknown_entity():
    s = _storage_with_data()
    mirror = MindMirror(s)
    result = mirror.query("NonExistent")
    assert "Ничего не знаю" in result


def test_query_with_user_id():
    s = _storage_with_data()
    mirror = MindMirror(s)
    result = mirror.query("Postgres", user_id="u1")
    assert "version: 16" in result
    # user_id filter — u2 has nothing
    result_u2 = mirror.query("Postgres", user_id="u2")
    assert "Ничего не знаю" in result_u2


# --- forget ---

def test_forget_all_facts_for_entity():
    s = _storage_with_data()
    mirror = MindMirror(s)
    count = mirror.forget("Postgres")
    assert count == 2  # version + choice
    # Verify: facts forgotten, but KG relations persist (forget doesn't touch KG)
    result = mirror.query("Postgres")
    assert "version: 16" not in result
    assert "use as main db" not in result
    assert "powers" in result  # KG relation still there
    # Other entity unaffected
    result_api = mirror.query("API")
    assert "2h on Tue" in result_api


def test_forget_specific_attribute():
    s = _storage_with_data()
    mirror = MindMirror(s)
    count = mirror.forget("Postgres", "version")
    assert count == 1
    # choice still there
    result = mirror.query("Postgres")
    assert "use as main db" in result
    assert "version: 16" not in result


def test_forget_unknown_entity():
    s = _storage_with_data()
    mirror = MindMirror(s)
    count = mirror.forget("NonExistent")
    assert count == 0


def test_forget_idempotent():
    """Повторный forget уже забытого → 0."""
    s = _storage_with_data()
    mirror = MindMirror(s)
    assert mirror.forget("Postgres") == 2
    assert mirror.forget("Postgres") == 0  # уже забыты


# --- natural-language detection (_detect_mind_query) ---

from caesar.core.orchestrator import Orchestrator


def test_detect_query_phrasings():
    """Natural-language «что ты знаешь про X?» → ('query', X)."""
    cases = [
        ("что ты знаешь про Postgres?", "Postgres"),
        ("что знаешь про API", "API"),
        ("что помнишь про Postgres", "Postgres"),
        ("что ты помнишь о Postgres", "Postgres"),
        ("Что ты знаешь про React?", "React"),
    ]
    for msg, entity in cases:
        result = Orchestrator._detect_mind_query(msg)
        assert result is not None, f"should detect: {msg}"
        assert result[0] == "query"
        assert result[1] == entity, f"entity={result[1]} expected={entity}"


def test_detect_forget_phrasings():
    """Natural-language «забудь X» / «забудь всё про X» → ('forget', X)."""
    cases = [
        ("забудь Postgres", "Postgres"),
        ("забудь всё про Postgres", "Postgres"),
        ("забудь про API", "API"),
        ("Забудь всё что знаешь про React", "React"),  # wait — this uses "про" detection
    ]
    for msg, entity in cases:
        result = Orchestrator._detect_mind_query(msg)
        assert result is not None, f"should detect: {msg}"
        assert result[0] == "forget", f"action={result[0]} for: {msg}"
        assert result[1] == entity, f"entity={result[1]} expected={entity} for: {msg}"


def test_detect_not_mind_query():
    """Не-mind-запросы → None."""
    for msg in ["расскажи про Postgres", "как дела", "найди новости",
                 "запомни что X = Y", "выполни ls", "обновись"]:
        assert Orchestrator._detect_mind_query(msg) is None, f"should NOT detect: {msg}"
