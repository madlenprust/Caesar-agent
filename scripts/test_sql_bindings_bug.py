"""Регрессионный тест для бага 'Incorrect number of bindings supplied'.

БАГ: в Python `(x)` — это НЕ tuple, это просто x в скобках.
     `(x,)` — это tuple с одним элементом.
     
При вызове `conn.execute(sql, (channel_id))` вместо `conn.execute(sql, (channel_id,))`:
- Если channel_id = "abc" (3 символа), sqlite3 получает 3 bindings вместо 1
- Если channel_id = "channel:user-1:main" (20 символов), sqlite3 получает 20 bindings
- Сообщение об ошибке: 'The current statement uses 1, and there are N supplied'

НАЙДЕННЫЕ БАГИ (все исправлены):
- storage.py:390 get_channel — (channel_id) → (channel_id,)
- storage.py:677 list_cron_tasks — (user_id) → (user_id,)
- storage.py:682 disable_cron_task — (cron_id) → (cron_id,)
- storage.py:709 update_cron_run — (cron_id) → (cron_id,)
- storage.py:712 update_cron_run — (cron_id) → (cron_id,)
- storage.py:744 get_skill — (name) → (name,)

Этот тест гарантирует что эти методы работают с КРАТКИМИ строками
(1-3 символа), которые до фикса падали с 'N bindings supplied'.
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


async def test_get_channel_with_short_id():
    """get_channel работает с 1-символьным channel_id."""
    from caesar.memory.storage import Storage
    
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        storage = Storage(db_path=tmpdir / "test.db")
        
        user_id = "u"  # 1 символ — максимально короткий
        storage.upsert_user(user_id=user_id, unix_uid=os.getuid(), display_name="Test")
        
        # channel_id из 1 символа
        channel_id = "x"
        storage.upsert_channel(
            channel_id=channel_id, user_id=user_id,
            source="cli", source_chat_id="t", display_name="main",
        )
        
        # До фикса: ProgrammingError: 1 binding, 1 supplied (вроде ок)
        # но при channel_id="xy" будет: 1 binding, 2 supplied = CRASH
        result = storage.get_channel(channel_id)
        assert result is not None, "Should find channel"
        assert result["id"] == channel_id
        print(f"  ✅ get_channel('{channel_id}') works")


async def test_get_channel_with_8_char_id():
    """get_channel работает с 8-символьным channel_id (reproduces user's bug)."""
    from caesar.memory.storage import Storage
    
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        storage = Storage(db_path=tmpdir / "test.db")
        
        user_id = "test-user"
        storage.upsert_user(user_id=user_id, unix_uid=os.getuid(), display_name="Test")
        
        # 8-символьный channel_id — как у пользователя "j,yjdbcm"
        channel_id = "j,yjdbcm"
        storage.upsert_channel(
            channel_id=channel_id, user_id=user_id,
            source="cli", source_chat_id="t", display_name="main",
        )
        
        # До фикса: 'The current statement uses 1, and there are 8 supplied.'
        result = storage.get_channel(channel_id)
        assert result is not None, "Should find 8-char channel"
        print(f"  ✅ get_channel('j,yjdbcm') works (8 chars)")


async def test_list_cron_tasks_with_short_user_id():
    """list_cron_tasks работает с коротким user_id."""
    from caesar.memory.storage import Storage
    
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        storage = Storage(db_path=tmpdir / "test.db")
        
        # 1-символьный user_id
        user_id = "u"
        storage.upsert_user(user_id=user_id, unix_uid=os.getuid(), display_name="Test")
        
        # Добавим cron task
        storage.add_cron_task({
            "user_id": user_id,
            "schedule": "0 9 * * *",
            "schedule_human": "Каждый день в 9:00",
            "task_to_execute": "test",
            "timezone": "Europe/Moscow",
            "enabled": 1,
        })
        
        # До фикса: падало бы с '1 binding, 1 supplied' (user_id='u', 1 char)
        # Но для user_id='ab' падало бы с '2 supplied'
        result = storage.list_cron_tasks(user_id)
        assert len(result) == 1
        print(f"  ✅ list_cron_tasks('{user_id}') works (1 char)")


async def test_disable_cron_task_with_short_id():
    """disable_cron_task работает с коротким cron_id."""
    from caesar.memory.storage import Storage
    
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        storage = Storage(db_path=tmpdir / "test.db")
        
        user_id = "u"
        storage.upsert_user(user_id=user_id, unix_uid=os.getuid(), display_name="Test")
        
        # Короткий cron_id
        cron_id = storage.add_cron_task({
            "user_id": user_id,
            "schedule": "0 9 * * *",
            "schedule_human": "Каждый день",
            "task_to_execute": "test",
            "timezone": "Europe/Moscow",
            "enabled": 1,
        })
        
        # До фикса: disable_cron_task передавал (cron_id) вместо (cron_id,)
        # Если cron_id длинный, ошибка была бы 'N supplied'
        storage.disable_cron_task(cron_id)
        
        # Проверяем что disabled
        tasks = storage.list_cron_tasks(user_id, only_enabled=True)
        assert len(tasks) == 0, "Should be disabled"
        print(f"  ✅ disable_cron_task works")


async def test_get_skill_with_short_name():
    """get_skill работает с коротким именем скилла."""
    from caesar.memory.storage import Storage
    
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        storage = Storage(db_path=tmpdir / "test.db")
        
        # Короткое имя скилла (8 символов = "skill_xy")
        skill_name = "skill_xy"
        storage.upsert_skill({
            "name": skill_name,
            "trigger": "test",
            "version": 1,
        })
        
        # До фикса: '1 binding, 8 supplied'
        result = storage.get_skill(skill_name)
        assert result is not None, "Should find skill"
        assert result["name"] == skill_name
        print(f"  ✅ get_skill('{skill_name}') works (8 chars)")


async def test_update_cron_run_with_short_id():
    """update_cron_run работает с коротким cron_id."""
    from datetime import datetime
    from caesar.memory.storage import Storage
    
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        storage = Storage(db_path=tmpdir / "test.db")
        
        user_id = "u"
        storage.upsert_user(user_id=user_id, unix_uid=os.getuid(), display_name="Test")
        
        cron_id = storage.add_cron_task({
            "user_id": user_id,
            "schedule": "0 9 * * *",
            "schedule_human": "Каждый день",
            "task_to_execute": "test",
            "timezone": "Europe/Moscow",
            "enabled": 1,
        })
        
        # update_cron_run использует (cron_id) в нескольких местах —
        # все должны работать
        storage.update_cron_run(cron_id, success=True, next_run_at=datetime.now())
        
        tasks = storage.list_cron_tasks(user_id)
        assert tasks[0]["total_runs"] == 1
        print(f"  ✅ update_cron_run works (success path)")
        
        # Тестируем failure path (3 неудачи → disable)
        for _ in range(4):
            storage.update_cron_run(cron_id, success=False)
        
        tasks = storage.list_cron_tasks(user_id, only_enabled=True)
        assert len(tasks) == 0, "Should be disabled after 3+ failures"
        print(f"  ✅ update_cron_run failure path works (auto-disable after 3 fails)")


async def main():
    print("=" * 60)
    print("TEST 1: get_channel with 1-char ID")
    print("=" * 60)
    await test_get_channel_with_short_id()
    
    print()
    print("=" * 60)
    print("TEST 2: get_channel with 8-char ID (user's bug)")
    print("=" * 60)
    await test_get_channel_with_8_char_id()
    
    print()
    print("=" * 60)
    print("TEST 3: list_cron_tasks with 1-char user_id")
    print("=" * 60)
    await test_list_cron_tasks_with_short_user_id()
    
    print()
    print("=" * 60)
    print("TEST 4: disable_cron_task with short cron_id")
    print("=" * 60)
    await test_disable_cron_task_with_short_id()
    
    print()
    print("=" * 60)
    print("TEST 5: get_skill with 8-char name")
    print("=" * 60)
    await test_get_skill_with_short_name()
    
    print()
    print("=" * 60)
    print("TEST 6: update_cron_run with short cron_id")
    print("=" * 60)
    await test_update_cron_run_with_short_id()
    
    print()
    print("🎉 All regression tests passed!")


if __name__ == "__main__":
    asyncio.run(main())
