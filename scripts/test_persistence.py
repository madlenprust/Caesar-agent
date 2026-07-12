"""Тест: persistence задач при graceful restart.

Сценарий:
1. Создаём TaskQueue с mock storage
2. Добавляем задачу (она остаётся pending/running)
3. Вызываем persist_unfinished_tasks — задача сохраняется в БД
4. Создаём НОВЫЙ TaskQueue (имитация рестарта)
5. При start() вызывается _restore_persisted_tasks — задача подхватывается
6. Проверяем что задача в _tasks и в очереди
"""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "/home/z/my-project")

from unittest.mock import MagicMock
from caesar.core.queue import TaskQueue, TaskStatus, TaskPriority, TaskComplexity
from caesar.config import Config
from caesar.memory.storage import Storage


async def main():
    print("=" * 60)
    print("PERSISTENCE TEST: Task survives daemon restart")
    print("=" * 60)
    
    # Создаём реальную SQLite БД во временной папке
    tmp_dir = tempfile.mkdtemp(prefix="caesar_persist_test_")
    db_path = Path(tmp_dir) / "test.db"
    
    # Подменяем DB_PATH
    import caesar.memory.storage as storage_module
    storage_module.DB_PATH = db_path
    
    storage = Storage()
    
    config = Config()
    config.queue.max_interactive_workers = 2
    config.queue.max_background_workers = 1
    
    # === Фаза 1: создаём очередь, добавляем задачу, "падаем" ===
    print("\n--- Phase 1: Create queue, add task, simulate crash ---")
    
    queue1 = TaskQueue(config)
    queue1.set_storage(storage)
    # НЕ запускаем workers — мы только тестируем persistence
    
    task_id = None
    async def fake_handler(task):
        # Имитируем что задача "в работе" — не завершаем
        await asyncio.sleep(100)
    
    queue1.set_task_handler(fake_handler)
    
    # Добавляем задачу
    task = await queue1.add_task(
        user_message="Найди новости про Hermes agent",
        user_id="test-user",
        channel_id="channel:test-user:main",
        author_id="test-user",
        source="cli",
        source_chat_id="cli-session",
    )
    task_id = task.id
    
    # Помечаем как running (имитируем что worker взял)
    task.status = TaskStatus.RUNNING
    task.current_step = 5
    task.tokens_used = 1500
    
    print(f"  Created task: {task_id}")
    print(f"  Status: {task.status.value}")
    print(f"  Step: {task.current_step}, tokens: {task.tokens_used}")
    
    # Persist
    saved = queue1.persist_unfinished_tasks()
    print(f"  Persisted: {saved} task(s)")
    assert saved == 1, f"Expected 1 persisted, got {saved}"
    
    # Проверяем что в БД есть запись
    unfinished = storage.get_unfinished_tasks()
    print(f"  DB has {len(unfinished)} unfinished task(s)")
    assert len(unfinished) == 1
    assert unfinished[0]["id"] == task_id
    assert unfinished[0]["status"] == "running"
    assert unfinished[0]["current_step"] == 5
    print("  ✓ Task saved to DB with correct state")
    
    # === Фаза 2: "рестарт" — новый TaskQueue, должен подхватить ===
    print("\n--- Phase 2: New queue (restart), should restore task ---")
    
    queue2 = TaskQueue(config)
    queue2.set_storage(storage)
    queue2.set_task_handler(fake_handler)
    
    # При start() вызовется _restore_persisted_tasks
    # НЕ запускаем workers чтобы задача не начала выполняться
    queue2._running = True
    await queue2._restore_persisted_tasks()
    
    # Проверяем что задача в _tasks
    restored = queue2._tasks.get(task_id)
    print(f"  Restored task: {restored.id if restored else None}")
    assert restored is not None, "Task was not restored!"
    assert restored.id == task_id
    assert restored.user_message == "Найди новости про Hermes agent"
    assert restored.user_id == "test-user"
    assert restored.channel_id == "channel:test-user:main"
    print(f"  Status: {restored.status.value}")
    print(f"  Message: {restored.user_message}")
    print("  ✓ Task restored with correct data")
    
    # Проверяем что задача в очереди (pending)
    assert restored.status == TaskStatus.PENDING, \
        f"Expected PENDING after restore, got {restored.status}"
    print("  ✓ Task is PENDING (ready to re-execute)")
    
    # Проверяем что БД очищена (задачи теперь в RAM)
    remaining_in_db = storage.get_unfinished_tasks()
    print(f"  DB now has {len(remaining_in_db)} unfinished (should be 0)")
    assert len(remaining_in_db) == 0
    print("  ✓ DB cleared (tasks now in RAM)")
    
    # Cleanup
    import shutil
    shutil.rmtree(tmp_dir, ignore_errors=True)
    
    print("\n=== ALL TESTS PASSED ===")
    print("\nSummary:")
    print("  ✓ persist_unfinished_tasks() saves running/pending tasks to DB")
    print("  ✓ _restore_persisted_tasks() loads them on startup")
    print("  ✓ Restored tasks are PENDING (ready to re-execute)")
    print("  ✓ DB cleared after restore (no duplicates)")


if __name__ == "__main__":
    asyncio.run(main())
