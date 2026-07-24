"""Тесты watchdog._check_stuck_tasks (audit N1 + correctness).

Покрытие:
- задача в пределах лимита → не фложится.
- задача превышает max_time (complexity) → flag failed + notify.
- paused-задача → исключена (не убивается watchdog-ом) — N1/paused-exclusion.
- boot-race: БД БЕЗ колонки `paused` → запрос не падает, stuck-detect работает
  (PRAGMA-check собирает WHERE условно) — N1 fix.
"""
import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from caesar.config import Config
from caesar.watchdog import SmartWatchdog


def _make_db(rows: list[dict], with_paused_col: bool = True) -> Path:
    db = Path(tempfile.mkdtemp()) / "t.db"
    conn = sqlite3.connect(db)
    cols = (
        "id TEXT PRIMARY KEY, started_at TEXT, complexity TEXT, channel_id TEXT, "
        "user_id TEXT, user_message TEXT, source_chat_id TEXT, source TEXT, "
        "status TEXT, error TEXT"
    )
    if with_paused_col:
        cols += ", paused INTEGER DEFAULT 0"
    conn.execute(f"CREATE TABLE tasks ({cols})")
    for r in rows:
        r = dict(r)  # copy — не мутируем исходник
        if not with_paused_col:
            r.pop("paused", None)  # колонки нет — не вставляем
        cols_list = list(r.keys())
        placeholders = ",".join("?" * len(cols_list))
        conn.execute(
            f"INSERT INTO tasks ({','.join(cols_list)}) VALUES ({placeholders})",
            list(r.values()),
        )
    conn.commit()
    conn.close()
    return db


def _row(task_id: str, age_sec: int, complexity: str = "simple",
         source_chat_id: str = "123", paused: int = 0) -> dict:
    started = (datetime.now() - timedelta(seconds=age_sec)).strftime("%Y-%m-%d %H:%M:%S")
    r = {
        "id": task_id, "started_at": started, "complexity": complexity,
        "channel_id": "ch", "user_id": "u", "user_message": f"task {task_id}",
        "source_chat_id": source_chat_id, "source": "telegram", "status": "running",
    }
    if paused is not None:
        r["paused"] = paused
    return r


def _make_watchdog():
    with patch.object(SmartWatchdog, "_init_llm", lambda self: None):
        wd = SmartWatchdog(Config())
    wd._notify_user = AsyncMock()
    return wd


async def _run(wd, db_path):
    with patch("caesar.watchdog.DB_PATH", db_path), \
         patch("subprocess.run"):  # не рестартить реальный daemon
        await wd._check_stuck_tasks()


def _task_status(db_path, task_id) -> str:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
    conn.close()
    return dict(row)["status"] if row else None


async def test_within_limit_not_flagged():
    """Simple task 60s ago (< 660s threshold) → не зависла, не фложится."""
    wd = _make_watchdog()
    db = _make_db([_row("t1", age_sec=60)])
    await _run(wd, db)
    assert _task_status(db, "t1") == "running"
    wd._notify_user.assert_not_called()


async def test_stuck_flagged_and_notified():
    """Simple task 800s ago (> 660s) → flag failed + notify."""
    wd = _make_watchdog()
    db = _make_db([_row("t2", age_sec=800)])
    await _run(wd, db)
    assert _task_status(db, "t2") == "failed"
    wd._notify_user.assert_called_once()


async def test_paused_task_excluded():
    """Paused-задача (даже превысившая лимит) НЕ фложится — watchdog её не трогает."""
    wd = _make_watchdog()
    db = _make_db([_row("t3", age_sec=800, paused=1)])
    await _run(wd, db)
    assert _task_status(db, "t3") == "running"  # не стал failed
    wd._notify_user.assert_not_called()


async def test_missing_paused_column_no_crash():
    """Boot-race (N1): БД без колонки `paused` → запрос не падает, stuck-detect работает."""
    wd = _make_watchdog()
    # БЕЗ with_paused_col — симулируем, что daemon ещё не прогнал миграцию
    db = _make_db([_row("t4", age_sec=800)], with_paused_col=False)
    await _run(wd, db)
    # Колонки `paused` нет → PRAGMA-check опускает фильтр → задача видна и фложится
    assert _task_status(db, "t4") == "failed"
    wd._notify_user.assert_called_once()


async def test_medium_complexity_uses_higher_threshold():
    """Medium task 800s ago (< 3600+60 threshold) → не зависла (порог зависит от complexity)."""
    wd = _make_watchdog()
    db = _make_db([_row("t5", age_sec=800, complexity="medium")])
    await _run(wd, db)
    assert _task_status(db, "t5") == "running"  # 800 < 3660 — внутри лимита medium
    wd._notify_user.assert_not_called()
