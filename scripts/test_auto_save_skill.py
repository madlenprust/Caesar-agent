"""Тесты для Auto-save Skills V2.

Проверяем:
1. Recipe extraction — извлекаются только recipe-worthy tool calls
2. Anti-patterns — собираются из failed tool calls
3. Skip если нет recipe-worthy tools (только read-only)
4. Skip если < 3 шагов
5. Skip если skill уже существует (дубликат)
6. Уведомление отправляется через event_bus
7. Skill сохраняется с правильной структурой
"""

import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))


def make_task(user_message: str = "настрой nginx для проекта"):
    """Создать mock Task."""
    task = MagicMock()
    task.user_message = user_message
    task.id = "test-task-id"
    task.source_chat_id = "test-chat"
    task.author_id = "user-1"
    task.complexity = MagicMock()
    task.complexity.value = "complex"
    return task


def make_orchestrator():
    """Создать Orchestrator без __init__."""
    from caesar.core.orchestrator import Orchestrator
    orch = Orchestrator.__new__(Orchestrator)
    orch.log = MagicMock()
    orch._l4 = MagicMock()
    orch._l4.get_skill = MagicMock(return_value=None)  # no existing skill
    orch._l4.save_skill = MagicMock()
    orch.event_bus = MagicMock()
    orch.event_bus.emit = AsyncMock()
    return orch


# ============================================================
# TEST 1: Recipe extraction — только recipe-worthy tools
# ============================================================
async def test_recipe_extraction():
    """Recipe extraction — извлекаются shell_exec/write_file, не web_search."""
    orch = make_orchestrator()
    task = make_task("настрой nginx для проекта")
    
    full_tool_history = [
        {"step": 1, "tool": "web_search", "args": {"query": "nginx setup"}, "success": True},
        {"step": 2, "tool": "shell_exec", "args": {"command": "apt install nginx"}, "success": True},
        {"step": 3, "tool": "shell_exec", "args": {"command": "systemctl start nginx"}, "success": True},
        {"step": 4, "tool": "write_file", "args": {"path": "/etc/nginx/sites-enabled/default", "content": "..."}, "success": True},
        {"step": 5, "tool": "shell_exec", "args": {"command": "nginx -t"}, "success": True},
    ]
    
    await orch._maybe_auto_save_skill(
        task, 
        used_tools={"web_search", "shell_exec", "write_file"},
        steps=5,
        full_tool_history=full_tool_history,
        emit_key="test-chat",
    )
    
    # Проверяем что save_skill был вызван
    assert orch._l4.save_skill.called, "save_skill should be called"
    
    saved_skill = orch._l4.save_skill.call_args[0][0]
    
    # Recipe должен содержать 4 шага (3 shell_exec + 1 write_file)
    # web_search не должен попасть
    assert len(saved_skill.exact_recipe) == 4, (
        f"Expected 4 recipe steps, got {len(saved_skill.exact_recipe)}"
    )
    
    recipe_tools = [step["tool"] for step in saved_skill.exact_recipe]
    assert "web_search" not in recipe_tools
    assert recipe_tools.count("shell_exec") == 3
    assert recipe_tools.count("write_file") == 1
    
    # anti_patterns должен быть пустой (все success)
    assert len(saved_skill.anti_patterns) == 0
    
    # needs_validation должен быть True
    assert saved_skill.needs_validation is True
    
    # Имя должно начинаться с auto_
    assert saved_skill.name.startswith("auto_")
    assert "nginx" in saved_skill.name or "настрой" in saved_skill.name or "проект" in saved_skill.name
    
    print(f"  ✅ Recipe extracted: {len(saved_skill.exact_recipe)} steps")
    print(f"    Tools: {recipe_tools}")
    print(f"    Skill name: {saved_skill.name}")


# ============================================================
# TEST 2: Anti-patterns из failed tool calls
# ============================================================
async def test_anti_patterns_from_errors():
    """Anti-patterns собираются из failed tool calls."""
    orch = make_orchestrator()
    task = make_task("настрой docker")
    
    full_tool_history = [
        {"step": 1, "tool": "shell_exec", "args": {"command": "apt install docker"}, "success": False, "error": "E: Unable to locate package docker"},
        {"step": 2, "tool": "shell_exec", "args": {"command": "apt update"}, "success": True},
        {"step": 3, "tool": "shell_exec", "args": {"command": "apt install docker.io"}, "success": True},
        {"step": 4, "tool": "shell_exec", "args": {"command": "systemctl start docker"}, "success": True},
    ]
    
    await orch._maybe_auto_save_skill(
        task,
        used_tools={"shell_exec"},
        steps=4,
        full_tool_history=full_tool_history,
        emit_key="test-chat",
    )
    
    saved_skill = orch._l4.save_skill.call_args[0][0]
    
    # Должен быть 1 anti_pattern (первая попытка упала)
    assert len(saved_skill.anti_patterns) == 1, (
        f"Expected 1 anti_pattern, got {len(saved_skill.anti_patterns)}"
    )
    
    ap = saved_skill.anti_patterns[0]
    assert "Unable to locate package docker" in ap["error"]
    assert ap["tool"] == "shell_exec"
    
    print(f"  ✅ Anti-patterns: {len(saved_skill.anti_patterns)}")
    print(f"    Error: {ap['error'][:60]}...")


# ============================================================
# TEST 3: Skip если только read-only tools
# ============================================================
async def test_skip_read_only_only():
    """Skip если только read-only tools (web_search, read_file)."""
    orch = make_orchestrator()
    task = make_task("найди информацию про python")
    
    full_tool_history = [
        {"step": 1, "tool": "web_search", "args": {"query": "python"}, "success": True},
        {"step": 2, "tool": "read_file", "args": {"path": "/tmp/x.py"}, "success": True},
        {"step": 3, "tool": "web_fetch", "args": {"url": "https://python.org"}, "success": True},
    ]
    
    await orch._maybe_auto_save_skill(
        task,
        used_tools={"web_search", "read_file", "web_fetch"},
        steps=3,
        full_tool_history=full_tool_history,
        emit_key="test-chat",
    )
    
    # save_skill НЕ должен быть вызван
    assert not orch._l4.save_skill.called, (
        "save_skill should NOT be called when only read-only tools used"
    )
    
    print(f"  ✅ Skipped: only read-only tools")


# ============================================================
# TEST 4: Skip если < 3 шагов
# ============================================================
async def test_skip_too_few_steps():
    """Skip если < 3 шагов."""
    orch = make_orchestrator()
    task = make_task("сделай что-то")
    
    full_tool_history = [
        {"step": 1, "tool": "shell_exec", "args": {"command": "ls"}, "success": True},
        {"step": 2, "tool": "shell_exec", "args": {"command": "pwd"}, "success": True},
    ]
    
    await orch._maybe_auto_save_skill(
        task,
        used_tools={"shell_exec"},
        steps=2,
        full_tool_history=full_tool_history,
        emit_key="test-chat",
    )
    
    assert not orch._l4.save_skill.called, "save_skill should NOT be called for < 3 steps"
    
    print(f"  ✅ Skipped: only 2 steps")


# ============================================================
# TEST 5: Skip если skill уже существует
# ============================================================
async def test_skip_duplicate():
    """Skip если skill с таким именем уже существует."""
    orch = make_orchestrator()
    
    # Mock get_skill чтобы вернуть существующий скилл
    from caesar.memory.l4 import Skill
    existing_skill = Skill(
        name="auto_настрой_nginx_проект",
        trigger="старый триггер",
        version=1,
    )
    orch._l4.get_skill = MagicMock(return_value=existing_skill)
    
    task = make_task("настрой nginx для проекта")
    
    full_tool_history = [
        {"step": 1, "tool": "shell_exec", "args": {"command": "x"}, "success": True},
        {"step": 2, "tool": "shell_exec", "args": {"command": "y"}, "success": True},
        {"step": 3, "tool": "shell_exec", "args": {"command": "z"}, "success": True},
    ]
    
    await orch._maybe_auto_save_skill(
        task,
        used_tools={"shell_exec"},
        steps=3,
        full_tool_history=full_tool_history,
        emit_key="test-chat",
    )
    
    # save_skill НЕ должен быть вызван (дубликат)
    assert not orch._l4.save_skill.called, "save_skill should NOT be called for duplicate"
    
    print(f"  ✅ Skipped: skill already exists")


# ============================================================
# TEST 6: Уведомление через event_bus
# ============================================================
async def test_notification_sent():
    """После сохранения отправляется INFO_NOTIFICATION."""
    orch = make_orchestrator()
    task = make_task("настрой nginx")
    
    full_tool_history = [
        {"step": 1, "tool": "shell_exec", "args": {"command": "apt install nginx"}, "success": True},
        {"step": 2, "tool": "shell_exec", "args": {"command": "systemctl start nginx"}, "success": True},
        {"step": 3, "tool": "shell_exec", "args": {"command": "nginx -t"}, "success": True},
    ]
    
    await orch._maybe_auto_save_skill(
        task,
        used_tools={"shell_exec"},
        steps=3,
        full_tool_history=full_tool_history,
        emit_key="test-chat-123",
    )
    
    # event_bus.emit должен быть вызван с правильным ключом
    assert orch.event_bus.emit.called, "event_bus.emit should be called"
    
    call_args = orch.event_bus.emit.call_args
    emit_key_used = call_args[0][0]
    event = call_args[0][1]
    
    assert emit_key_used == "test-chat-123"
    assert event.type == "info_notification"
    assert "Автоматически сохранил скилл" in event.data["message"]
    assert "caesar skill remove" in event.data["message"]
    
    print(f"  ✅ Notification sent to emit_key='{emit_key_used}'")
    print(f"    Message preview: {event.data['message'][:80]}...")


# ============================================================
# TEST 7: Skill структура корректна
# ============================================================
async def test_skill_structure():
    """Проверяем структуру сохранённого skill."""
    orch = make_orchestrator()
    task = make_task("настрой postgresql")
    
    full_tool_history = [
        {"step": 1, "tool": "shell_exec", "args": {"command": "apt install postgresql"}, "success": True},
        {"step": 2, "tool": "shell_exec", "args": {"command": "systemctl enable postgresql"}, "success": True},
    ]
    
    await orch._maybe_auto_save_skill(
        task,
        used_tools={"shell_exec"},
        steps=3,
        full_tool_history=full_tool_history,
        emit_key="test-chat",
    )
    
    saved_skill = orch._l4.save_skill.call_args[0][0]
    
    # Проверяем все поля
    assert saved_skill.name.startswith("auto_")
    assert saved_skill.trigger == task.user_message[:200]
    assert saved_skill.version == 1
    assert saved_skill.created_at  # непустой ISO timestamp
    assert "Auto-saved" in saved_skill.notes
    assert "shell_exec" in saved_skill.notes
    assert len(saved_skill.exact_recipe) == 2
    assert saved_skill.needs_validation is True
    assert len(saved_skill.pitfalls) >= 1
    assert "Auto-saved" in saved_skill.pitfalls[0]
    
    # Каждый recipe step должен иметь tool, args, step
    for rstep in saved_skill.exact_recipe:
        assert "tool" in rstep
        assert "args" in rstep
        assert "step" in rstep
    
    print(f"  ✅ Skill structure valid:")
    print(f"    name: {saved_skill.name}")
    print(f"    version: {saved_skill.version}")
    print(f"    needs_validation: {saved_skill.needs_validation}")
    print(f"    pitfalls: {len(saved_skill.pitfalls)}")


# ============================================================
# TEST 8: Пустой full_tool_history — skip
# ============================================================
async def test_skip_empty_history():
    """Skip если full_tool_history пустой."""
    orch = make_orchestrator()
    task = make_task()
    
    await orch._maybe_auto_save_skill(
        task,
        used_tools={"shell_exec"},
        steps=5,
        full_tool_history=None,
        emit_key="test-chat",
    )
    
    assert not orch._l4.save_skill.called
    print(f"  ✅ Skipped: empty tool history")


# ============================================================
# RUN ALL TESTS
# ============================================================
async def main():
    print("=" * 60)
    print("TEST 1: Recipe extraction")
    print("=" * 60)
    await test_recipe_extraction()
    
    print()
    print("=" * 60)
    print("TEST 2: Anti-patterns from errors")
    print("=" * 60)
    await test_anti_patterns_from_errors()
    
    print()
    print("=" * 60)
    print("TEST 3: Skip — read-only only")
    print("=" * 60)
    await test_skip_read_only_only()
    
    print()
    print("=" * 60)
    print("TEST 4: Skip — too few steps")
    print("=" * 60)
    await test_skip_too_few_steps()
    
    print()
    print("=" * 60)
    print("TEST 5: Skip — duplicate skill")
    print("=" * 60)
    await test_skip_duplicate()
    
    print()
    print("=" * 60)
    print("TEST 6: Notification sent via event_bus")
    print("=" * 60)
    await test_notification_sent()
    
    print()
    print("=" * 60)
    print("TEST 7: Skill structure")
    print("=" * 60)
    await test_skill_structure()
    
    print()
    print("=" * 60)
    print("TEST 8: Skip — empty tool history")
    print("=" * 60)
    await test_skip_empty_history()
    
    print()
    print("🎉 All tests passed!")


if __name__ == "__main__":
    asyncio.run(main())
