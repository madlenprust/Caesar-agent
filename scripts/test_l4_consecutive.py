"""L4 skills: consecutive_failures — 3 подряд → broken; success сбрасывает.

Регресс на баг из аудита: record_success падал на несуществующей колонке
consecutive_failures (её не было в l4_skills), а record_failure считал lifetime,
не подряд. Плюс version не должен inflate'иться на рестарте без смены YAML.
"""
import tempfile
from pathlib import Path

from caesar.memory.l4 import L4Skills
from caesar.memory.storage import Storage


def _make_l4():
    tmp = tempfile.mkdtemp(prefix="caesar_l4_test_")
    storage = Storage(db_path=Path(tmp) / "test.db")
    skills_dir = Path(tmp) / "skills"
    skills_dir.mkdir()
    return L4Skills(storage, skills_dir=skills_dir), storage


def test_record_success_resets_consecutive():
    l4, storage = _make_l4()
    storage.upsert_skill({"name": "s1", "trigger": "test trigger keyword"})
    l4.record_failure("s1", "e1")
    l4.record_failure("s1", "e2")
    assert storage.get_skill("s1")["consecutive_failures"] == 2
    l4.record_success("s1")  # сбрасывает
    assert storage.get_skill("s1")["consecutive_failures"] == 0
    assert storage.get_skill("s1")["success_count"] == 1


def test_three_consecutive_failures_breaks():
    l4, storage = _make_l4()
    storage.upsert_skill({"name": "s2", "trigger": "t"})
    for _ in range(3):
        l4.record_failure("s2", "e")
    assert storage.get_skill("s2")["broken"] == 1


def test_two_then_success_then_two_not_broken():
    """3 подряд нужно; успех между провалами обнуляет счётчик."""
    l4, storage = _make_l4()
    storage.upsert_skill({"name": "s3", "trigger": "t"})
    l4.record_failure("s3", "e")
    l4.record_failure("s3", "e")
    l4.record_success("s3")  # обнулили
    l4.record_failure("s3", "e")
    l4.record_failure("s3", "e")  # всего 2 подряд после сброса
    assert storage.get_skill("s3")["broken"] == 0


def test_sync_no_version_inflate_on_restart():
    """Два __init__ без смены YAML не поднимают version."""
    tmp = tempfile.mkdtemp(prefix="caesar_l4_test_")
    storage = Storage(db_path=Path(tmp) / "test.db")
    skills_dir = Path(tmp) / "skills"
    skills_dir.mkdir()
    (skills_dir / "s.yaml").write_text("name: s\ntrigger: t\n", encoding="utf-8")
    L4Skills(storage, skills_dir=skills_dir)
    v1 = storage.get_skill("s")["version"]
    L4Skills(storage, skills_dir=skills_dir)  # имитация рестарта
    v2 = storage.get_skill("s")["version"]
    assert v1 == v2, f"version inflated on restart: {v1} -> {v2}"
