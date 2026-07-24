"""Тесты cron quiet-hours (audit H3) — defer, не терять работу.

H3-fix (0.x): cron в quiet hours раньше hold'ил и терял задачу. Теперь —
DEFER: переносит firing на конец quiet hours (reschedule), и в конце quiet
задача выполнится. Покрытие:
- _quiet_window_contains: чистая логика (sameday/overnight/invalid) — детерминированно.
- _fire_cron_impl: quiet → defer (scheduler.add_job с deferred id, без выполнения);
  non-quiet → выполняется (queue.add_task), БЕЗ defer.
"""
from datetime import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from caesar.config import Config
from caesar.core.cron import CronScheduler
from caesar.memory.storage import Storage


# --- pure-логика quiet-window ---

def test_quiet_window_sameday():
    """08:00-17:00 — обычное окно в течение дня."""
    f = CronScheduler._quiet_window_contains
    assert f(time(12, 0), "08:00", "17:00") is True
    assert f(time(7, 59), "08:00", "17:00") is False
    assert f(time(18, 0), "08:00", "17:00") is False
    assert f(time(8, 0), "08:00", "17:00") is True   # граница start
    assert f(time(17, 0), "08:00", "17:00") is True   # граница end


def test_quiet_window_overnight():
    """23:00-08:00 — окно через полночь."""
    f = CronScheduler._quiet_window_contains
    assert f(time(23, 30), "23:00", "08:00") is True   # поздно вечером
    assert f(time(2, 0), "23:00", "08:00") is True     # рано утром
    assert f(time(12, 0), "23:00", "08:00") is False   # днём
    assert f(time(8, 0), "23:00", "08:00") is True     # граница end
    assert f(time(23, 0), "23:00", "08:00") is True    # граница start


def test_quiet_window_invalid_config_returns_false():
    """Битый конфиг → False (не падает)."""
    f = CronScheduler._quiet_window_contains
    assert f(time(12, 0), "bad", "08:00") is False
    assert f(time(12, 0), "25:00", "08:00") is False
    assert f(time(12, 0), "23:00", "xx") is False


# --- behavioral: defer vs execute ---

def _make_cron_with_task(enabled: bool = True):
    """temp Storage + cron_task + channel (numeric source_chat_id)."""
    import tempfile
    from pathlib import Path
    db = Path(tempfile.mkdtemp()) / "t.db"
    storage = Storage(db_path=db)
    cron_id = storage.add_cron_task({
        "user_id": "u1", "channel_id": "ch1", "schedule": "0 9 * * *",
        "schedule_human": "09:00", "task_to_execute": "do morning thing",
    })
    # деактивируем если надо
    if not enabled:
        with storage._conn() as c:
            c.execute("UPDATE cron_tasks SET enabled=0 WHERE id=?", (cron_id,))
    storage.upsert_channel(channel_id="ch1", user_id="u1", source="telegram",
                           source_chat_id="123", display_name="main")
    return storage, cron_id


def _build_cron(storage):
    cron = CronScheduler(Config(), storage, queue=MagicMock())
    cron._scheduler = MagicMock()  # без start() — _scheduler None; даём mock для defer
    cron.queue = AsyncMock()       # queue.add_task — async
    return cron


async def _fire(cron, cron_id, quiet: bool):
    with patch.object(CronScheduler, "_is_quiet_hours", return_value=quiet):
        await cron._fire_cron_impl(cron_id)


def _deferred_addjob_called(cron, cron_id) -> bool:
    target_id = f"{cron_id}:deferred"
    for call in cron._scheduler.add_job.call_args_list:
        if call.kwargs.get("id") == target_id:
            return True
    return False


async def test_quiet_hours_defers_not_loses():
    """quiet → задача ПЕРЕНОСИТСЯ (deferred add_job), НЕ выполняется (queue.add_task не звался)."""
    storage, cron_id = _make_cron_with_task(enabled=True)
    cron = _build_cron(storage)
    await _fire(cron, cron_id, quiet=True)
    assert _deferred_addjob_called(cron, cron_id)  # reschedule на конец quiet
    cron.queue.add_task.assert_not_called()        # не выполняли — отложили


async def test_non_quiet_executes_no_defer():
    """non-quiet → задача ВЫПОЛНЯЕТСЯ (queue.add_task), БЕЗ defer."""
    storage, cron_id = _make_cron_with_task(enabled=True)
    cron = _build_cron(storage)
    await _fire(cron, cron_id, quiet=False)
    assert not _deferred_addjob_called(cron, cron_id)  # не откладывали
    cron.queue.add_task.assert_called_once()           # выполняли


async def test_disabled_cron_skipped_even_in_quiet():
    """disabled cron → skip сразу (не defer, не execute)."""
    storage, cron_id = _make_cron_with_task(enabled=False)
    cron = _build_cron(storage)
    await _fire(cron, cron_id, quiet=True)
    assert not _deferred_addjob_called(cron, cron_id)  # disabled → return до quiet-чек
    cron.queue.add_task.assert_not_called()
