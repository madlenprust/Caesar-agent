"""Тесты для расширенного status report.

Покрываем:
1. generate_status_report — структура dict корректна
2. _collect_memory_stats — L3/L4/KG counts
3. _collect_cron_stats — задачи с расписанием
4. _collect_token_stats — today/week с разбивкой по ролям
5. _collect_recent_dialogs — последние 5 диалогов с временем
6. format_status_text — текст содержит все секции
7. _format_uptime — человекочитаемое время
8. _format_relative_time — прошлое/будущее
9. Edge cases: пустая БД, нет token_usage, нет cron tasks
"""

import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))


def make_storage_with_data(tmpdir: Path):
    """Создать Storage с тестовыми данными."""
    from caesar.memory.storage import Storage
    storage = Storage(db_path=tmpdir / "test.db")
    
    user_id = "test-user"
    storage.upsert_user(user_id=user_id, unix_uid=os.getuid(), display_name="Test")
    storage.upsert_channel(
        channel_id=f"channel:{user_id}:main",
        user_id=user_id, source="cli", source_chat_id="t", display_name="main",
    )
    
    # L3 chunks (5 обычных + 2 consolidated)
    with storage._conn() as conn:
        for i in range(5):
            conn.execute(
                """INSERT INTO l3_chunks (id, user_id, channel, content, chunk_metadata)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    f"chunk-{i}", user_id, "main",
                    f"Content {i} about Python asyncio",
                    json.dumps({"auto_indexed": True}),
                ),
            )
        for i in range(2):
            conn.execute(
                """INSERT INTO l3_chunks (id, user_id, channel, content, chunk_metadata)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    f"chunk-cons-{i}", user_id, "main",
                    f"Consolidated {i}",
                    json.dumps({"type": "consolidated", "topic": f"topic-{i}"}),
                ),
            )
        
        # L4 skills (3 обычных + 1 needs_validation)
        for i in range(3):
            conn.execute(
                """INSERT INTO l4_skills (name, trigger, version, broken, needs_validation)
                   VALUES (?, ?, ?, ?, ?)""",
                (f"skill-{i}", f"trigger {i}", 1, 0, 0),
            )
        conn.execute(
            """INSERT INTO l4_skills (name, trigger, version, broken, needs_validation)
               VALUES (?, ?, ?, ?, ?)""",
            ("skill-needs-val", "trigger", 1, 0, 1),
        )
        
        # KG entities & relations
        for i in range(5):
            conn.execute(
                """INSERT INTO kg_entities (id, user_id, name, entity_type, first_seen, last_seen, mention_count, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    f"ent-{i}", user_id, f"Entity{i}", "concept",
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    i + 1,
                    json.dumps({}),
                ),
            )
        # 1 stale entity
        conn.execute(
            """INSERT INTO kg_entities (id, user_id, name, entity_type, first_seen, last_seen, mention_count, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "ent-stale", user_id, "StaleEntity", "concept",
                (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d %H:%M:%S"),
                (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d %H:%M:%S"),
                1,
                json.dumps({"stale": True}),
            ),
        )
        
        # 2 relations
        for i in range(2):
            conn.execute(
                """INSERT INTO kg_relations (id, user_id, from_entity, to_entity, relation_type)
                   VALUES (?, ?, ?, ?, ?)""",
                (f"rel-{i}", user_id, f"Entity{i}", "Entity{i+1}", "related_to"),
            )
        
        # Cron tasks (2 active, 1 disabled)
        for i in range(2):
            conn.execute(
                """INSERT INTO cron_tasks 
                   (id, user_id, schedule, schedule_human, task_to_execute, enabled,
                    last_run_at, next_run_at, total_runs, successful_runs, failed_runs,
                    consecutive_failures)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    f"cron-{i}", user_id, "0 9 * * *", "Каждый день в 9:00",
                    f"найди новости {i}", 1,
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"),
                    10, 9, 1, 0,
                ),
            )
        conn.execute(
            """INSERT INTO cron_tasks 
               (id, user_id, schedule, schedule_human, task_to_execute, enabled)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("cron-disabled", user_id, "0 0 * * 0", "Раз в неделю", "старая задача", 0),
        )
        
        # Token usage (5 calls today, 3 last week)
        # Используем datetime.now().replace(hour=12) — середина дня,
        # чтобы избежать edge cases на границе UTC/local при сравнении с
        # datetime('now', 'start of day') в SQLite.
        today_noon = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
        for i in range(5):
            conn.execute(
                """INSERT INTO token_usage 
                   (id, task_id, timestamp, llm_role, llm_model, 
                    prompt_tokens, completion_tokens, total_tokens, cost_rub, reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    f"tu-today-{i}", f"task-{i}",
                    today_noon.strftime("%Y-%m-%d %H:%M:%S"),
                    "smart" if i % 2 == 0 else "cheap",
                    "test-model",
                    100 * (i + 1), 50 * (i + 1), 150 * (i + 1),
                    0.001 * (i + 1),
                    "main_answer",
                ),
            )
        # Old tokens (14 days ago — точно за пределами недели)
        for i in range(3):
            old_dt = datetime.now() - timedelta(days=14)
            conn.execute(
                """INSERT INTO token_usage 
                   (id, task_id, timestamp, llm_role, llm_model, 
                    prompt_tokens, completion_tokens, total_tokens, cost_rub, reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    f"tu-old-{i}", f"task-old-{i}",
                    old_dt.strftime("%Y-%m-%d %H:%M:%S"),
                    "cheap", "test-model",
                    200, 100, 300, 0.002,
                    "analysis",
                ),
            )
        
        # Week tokens (3 days ago — попадают в недельный фильтр)
        week_dt = datetime.now() - timedelta(days=3, hours=1)
        conn.execute(
            """INSERT INTO token_usage 
               (id, task_id, timestamp, llm_role, llm_model, 
                prompt_tokens, completion_tokens, total_tokens, cost_rub, reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "tu-week-0", "task-week-0",
                week_dt.strftime("%Y-%m-%d %H:%M:%S"),
                "smart", "test-model",
                500, 200, 700, 0.005,
                "main_answer",
            ),
        )
        
        # Tasks (5 диалогов)
        for i in range(5):
            dt = datetime.now() - timedelta(minutes=i * 30)
            conn.execute(
                """INSERT INTO tasks 
                   (id, user_id, channel_id, user_message, status, complexity,
                    created_at, completed_at, current_step, tokens_used, cost_rub)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    f"task-{i}", user_id, f"channel:{user_id}:main",
                    f"настрой nginx {i}" if i % 2 == 0 else f"найди новости {i}",
                    "completed" if i < 4 else "running",
                    "complex" if i == 0 else "simple",
                    dt.strftime("%Y-%m-%d %H:%M:%S"),
                    (dt + timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S") if i < 4 else None,
                    i + 1, 1000 * (i + 1), 0.01 * (i + 1),
                ),
            )
        
        conn.commit()
    
    return storage, user_id


# ============================================================
# TEST 1: generate_status_report — структура
# ============================================================
async def test_status_report_structure():
    """Полный отчёт содержит все секции."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        storage, user_id = make_storage_with_data(tmpdir)
        
        from caesar.core.status import generate_status_report
        report = generate_status_report(storage, user_id=user_id)
        
        # Должны быть все секции
        assert "daemon" in report
        assert "memory" in report
        assert "cron" in report
        assert "tokens" in report
        assert "recent" in report
        
        # daemon содержит version и uptime_seconds
        assert "version" in report["daemon"]
        assert "uptime_seconds" in report["daemon"]
        
        print(f"  ✅ Report structure: {list(report.keys())}")


# ============================================================
# TEST 2: memory stats
# ============================================================
async def test_memory_stats():
    """L3/L4/KG статистика корректна."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        storage, user_id = make_storage_with_data(tmpdir)
        
        from caesar.core.status import _collect_memory_stats
        mem = _collect_memory_stats(storage, user_id)
        
        # L3: 5 + 2 consolidated = 7
        assert mem["l3"]["total_chunks"] == 7, f"Expected 7, got {mem['l3']['total_chunks']}"
        assert mem["l3"]["consolidated_chunks"] == 2
        
        # L4: 3 + 1 needs_validation = 4
        assert mem["l4"]["total_skills"] == 4
        assert mem["l4"]["needs_validation"] == 1
        
        # KG: 6 entities (5 + 1 stale), 2 relations
        assert mem["kg"]["entities"] == 6
        assert mem["kg"]["relations"] == 2
        assert mem["kg"]["stale_entities"] == 1
        
        print(f"  ✅ Memory stats: L3={mem['l3']['total_chunks']}, L4={mem['l4']['total_skills']}, KG={mem['kg']['entities']}")


# ============================================================
# TEST 3: cron stats
# ============================================================
async def test_cron_stats():
    """Cron: 2 active задачи (не 3, 1 disabled)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        storage, user_id = make_storage_with_data(tmpdir)
        
        from caesar.core.status import _collect_cron_stats
        cron = _collect_cron_stats(storage)
        
        assert cron["active"] == 2, f"Expected 2 active, got {cron['active']}"
        assert len(cron["tasks"]) == 2
        
        # Проверяем структуру task
        task = cron["tasks"][0]
        assert "schedule" in task
        assert "task_preview" in task
        assert "last_run" in task
        assert "next_run" in task
        assert "total_runs" in task
        
        # last_run должен быть relative ('X мин назад')
        assert "назад" in task["last_run"] or "только что" in task["last_run"]
        
        # next_run должен быть future ('через X')
        assert "через" in task["next_run"] or "сейчас" in task["next_run"]
        
        print(f"  ✅ Cron: {cron['active']} active, first task: '{task['task_preview'][:30]}...'")


# ============================================================
# TEST 4: token stats
# ============================================================
async def test_token_stats():
    """Tokens: today vs week, разбивка smart/cheap."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        storage, user_id = make_storage_with_data(tmpdir)
        
        from caesar.core.status import _collect_token_stats
        tokens = _collect_token_stats(storage)
        
        # Today: 5 calls, total = 150*(1+2+3+4+5) = 150*15 = 2250
        # smart (i=0,2,4): 150*(1+3+5) = 150*9 = 1350
        # cheap (i=1,3): 150*(2+4) = 150*6 = 900
        today = tokens["today"]
        assert today["calls"] == 5
        assert today["total"] == 2250, f"Expected 2250, got {today['total']}"
        assert today["smart"] == 1350
        assert today["cheap"] == 900
        
        # Week: 5 today + 1 (3 days ago, smart, 700 tokens) = 6 calls, 2950 tokens
        week = tokens["week"]
        assert week["calls"] == 6, f"Expected 6, got {week['calls']}"
        assert week["total"] == 2950, f"Expected 2950, got {week['total']}"
        # smart: 1350 (today) + 700 (week-0) = 2050
        assert week["smart"] == 2050
        
        print(f"  ✅ Tokens today: {today['total']} ({today['calls']} calls)")
        print(f"     Tokens week: {week['total']} ({week['calls']} calls)")


# ============================================================
# TEST 5: recent dialogs
# ============================================================
async def test_recent_dialogs():
    """Последние 5 диалогов отсортированы по времени (новые вперёд)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        storage, user_id = make_storage_with_data(tmpdir)
        
        from caesar.core.status import _collect_recent_dialogs
        dialogs = _collect_recent_dialogs(storage, user_id, limit=5)
        
        assert len(dialogs) == 5
        
        # Первый должен быть самый свежий (task-0, 0 минут назад)
        first = dialogs[0]
        assert "настрой nginx 0" in first["message_preview"] or "найди новости 0" in first["message_preview"]
        assert first["status"] in ("completed", "running")
        
        # Должны быть поля
        assert "time" in first
        assert "status_icon" in first
        assert "steps" in first
        assert "complexity" in first
        
        print(f"  ✅ Recent dialogs: {len(dialogs)}, first: '{first['message_preview'][:40]}...'")


# ============================================================
# TEST 6: format_status_text — рендеринг
# ============================================================
async def test_format_status_text():
    """Текст содержит все секции."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        storage, user_id = make_storage_with_data(tmpdir)
        
        from caesar.core.status import generate_status_report, format_status_text
        report = generate_status_report(storage, version="0.7.0", uptime_seconds=90061)
        
        text = format_status_text(report)
        
        # Должны быть все секции
        assert "Caesar v0.7.0" in text
        assert "1d 1h 1m" in text  # 90061 sec = 1d 1h 1m
        assert "📊 Память:" in text
        assert "L3: 7 чанков" in text
        assert "consolidated" in text
        assert "L4: 4 скиллов" in text
        assert "KG: 6 entities" in text
        assert "⏰ Cron: 2 активных задач" in text
        assert "💰 Токены:" in text
        assert "Сегодня:" in text
        assert "За неделю:" in text
        assert "💬 Последние диалоги:" in text
        
        print(f"  ✅ Status text rendered ({len(text)} chars)")
        # Print first 200 chars for visual inspection
        preview = text[:200].replace("\n", " | ")
        print(f"     Preview: {preview}...")


# ============================================================
# TEST 7: _format_uptime
# ============================================================
async def test_format_uptime():
    """Uptime formatting корректен."""
    from caesar.core.status import _format_uptime
    
    assert _format_uptime(0) == "0m"
    assert _format_uptime(59) == "0m"
    assert _format_uptime(60) == "1m"
    assert _format_uptime(3600) == "1h 0m"
    assert _format_uptime(3661) == "1h 1m"
    # 86400 = exactly 1 day → "1d 0m" (hours=0 пропускается)
    assert _format_uptime(86400) == "1d 0m"
    assert _format_uptime(90061) == "1d 1h 1m"
    assert _format_uptime(None) == "unknown"
    assert _format_uptime(-1) == "unknown"
    
    print(f"  ✅ Uptime format: 0m, 1m, 1h 0m, 1d 0m, 1d 1h 1m, unknown")


# ============================================================
# TEST 8: _format_relative_time
# ============================================================
async def test_format_relative_time():
    """Relative time formatting для прошлого и будущего."""
    from caesar.core.status import _format_relative_time
    
    now = datetime.now()
    
    # Past
    assert "только что" in _format_relative_time(now)
    assert "мин назад" in _format_relative_time(now - timedelta(minutes=5))
    # 2h ago → "2ч 0м назад"
    h_result = _format_relative_time(now - timedelta(hours=2))
    assert "ч" in h_result and "назад" in h_result, f"Expected hours+назад, got {h_result}"
    d_result = _format_relative_time(now - timedelta(days=3))
    assert "дн назад" in d_result, f"Expected 'дн назад', got {d_result}"
    
    # Future
    assert "сейчас" in _format_relative_time(now + timedelta(seconds=10), future=True)
    assert "через" in _format_relative_time(now + timedelta(minutes=5), future=True)
    assert "через" in _format_relative_time(now + timedelta(days=2), future=True)
    
    print(f"  ✅ Relative time format: past + future")


# ============================================================
# TEST 9: Empty database
# ============================================================
async def test_empty_database():
    """Пустая БД — все счётчики 0, отчёт не падает."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        from caesar.memory.storage import Storage
        storage = Storage(db_path=tmpdir / "empty.db")
        
        from caesar.core.status import generate_status_report, format_status_text
        
        report = generate_status_report(storage, version="0.7.0")
        
        # Все счётчики 0
        assert report["memory"]["l3"]["total_chunks"] == 0
        assert report["memory"]["l4"]["total_skills"] == 0
        assert report["memory"]["kg"]["entities"] == 0
        assert report["cron"]["active"] == 0
        assert report["tokens"]["today"]["total"] == 0
        assert report["tokens"]["week"]["total"] == 0
        assert report["recent"] == []
        
        # format_status_text не должен падать
        text = format_status_text(report)
        assert "Caesar v0.7.0" in text
        
        print(f"  ✅ Empty database handled correctly")


# ============================================================
# RUN ALL TESTS
# ============================================================
async def main():
    print("=" * 60)
    print("TEST 1: Report structure")
    print("=" * 60)
    await test_status_report_structure()
    
    print()
    print("=" * 60)
    print("TEST 2: Memory stats")
    print("=" * 60)
    await test_memory_stats()
    
    print()
    print("=" * 60)
    print("TEST 3: Cron stats")
    print("=" * 60)
    await test_cron_stats()
    
    print()
    print("=" * 60)
    print("TEST 4: Token stats")
    print("=" * 60)
    await test_token_stats()
    
    print()
    print("=" * 60)
    print("TEST 5: Recent dialogs")
    print("=" * 60)
    await test_recent_dialogs()
    
    print()
    print("=" * 60)
    print("TEST 6: format_status_text rendering")
    print("=" * 60)
    await test_format_status_text()
    
    print()
    print("=" * 60)
    print("TEST 7: _format_uptime")
    print("=" * 60)
    await test_format_uptime()
    
    print()
    print("=" * 60)
    print("TEST 8: _format_relative_time")
    print("=" * 60)
    await test_format_relative_time()
    
    print()
    print("=" * 60)
    print("TEST 9: Empty database")
    print("=" * 60)
    await test_empty_database()
    
    print()
    print("🎉 All tests passed!")


if __name__ == "__main__":
    asyncio.run(main())
