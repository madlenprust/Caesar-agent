"""Тест проверки requires_permission в ToolRegistry.execute.

ПРОБЛЕМА: LLM могла выполнить 'systemctl restart caesar-daemon' через
shell_exec — это убивало daemon. requires_permission() существовал, но
ToolRegistry.execute() его НЕ проверял.

ФИКС: ToolRegistry.execute() теперь проверяет requires_permission() и
возвращает ToolResult(success=False, error='BLOCKED...') если команда
опасная.
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))


async def test_dangerous_command_blocked():
    """systemctl restart блокируется ToolRegistry.execute."""
    from caesar.tools import ToolRegistry
    from caesar.tools.base import ToolResult
    
    registry = ToolRegistry(
        storage=MagicMock(),
        l3_memory=MagicMock(),
        l4_skills=MagicMock(),
        access_mode="sandboxed",
    )
    
    # Пытаемся выполнить опасную команду
    result = await registry.execute("shell_exec", command="systemctl --user restart caesar-daemon")
    
    assert isinstance(result, ToolResult), f"Expected ToolResult, got {type(result)}"
    assert not result.success, "Dangerous command should be blocked"
    assert "BLOCKED" in result.error or "permission" in result.error.lower(), (
        f"Expected BLOCKED in error, got: {result.error}"
    )
    
    print(f"  ✅ 'systemctl restart' blocked: {result.error[:80]}...")


async def test_rm_rf_blocked():
    """rm -rf блокируется."""
    from caesar.tools import ToolRegistry
    
    registry = ToolRegistry(
        storage=MagicMock(),
        l3_memory=MagicMock(),
        l4_skills=MagicMock(),
        access_mode="sandboxed",
    )
    
    result = await registry.execute("shell_exec", command="rm -rf /tmp/test")
    
    assert not result.success
    assert "BLOCKED" in result.error or "permission" in result.error.lower()
    
    print(f"  ✅ 'rm -rf' blocked")


async def test_sudo_blocked():
    """sudo блокируется."""
    from caesar.tools import ToolRegistry
    
    registry = ToolRegistry(
        storage=MagicMock(),
        l3_memory=MagicMock(),
        l4_skills=MagicMock(),
        access_mode="sandboxed",
    )
    
    result = await registry.execute("shell_exec", command="sudo apt update")
    
    assert not result.success
    assert "BLOCKED" in result.error or "permission" in result.error.lower()
    
    print(f"  ✅ 'sudo' blocked")


async def test_safe_command_allowed():
    """Безопасная команда НЕ блокируется (cat, ls, echo)."""
    from caesar.tools import ToolRegistry
    
    registry = ToolRegistry(
        storage=MagicMock(),
        l3_memory=MagicMock(),
        l4_skills=MagicMock(),
        access_mode="sandboxed",
    )
    
    # cat /etc/hosts — безопасная команда, должна выполниться
    result = await registry.execute("shell_exec", command="echo hello")
    
    # Не должна быть заблокирована (может упасть по другой причине, но не BLOCKED)
    if not result.success:
        assert "BLOCKED" not in result.error, (
            f"Safe command should not be BLOCKED, got: {result.error}"
        )
    
    print(f"  ✅ Safe command 'echo hello' allowed (success={result.success})")


async def test_unknown_tool():
    """Неизвестный инструмент возвращает ошибку."""
    from caesar.tools import ToolRegistry
    
    registry = ToolRegistry(
        storage=MagicMock(),
        l3_memory=MagicMock(),
        l4_skills=MagicMock(),
        access_mode="sandboxed",
    )
    
    result = await registry.execute("nonexistent_tool")
    
    assert not result.success
    assert "not found" in result.error.lower()
    
    print(f"  ✅ Unknown tool returns error")


async def main():
    print("=" * 60)
    print("TEST 1: 'systemctl restart' blocked")
    print("=" * 60)
    await test_dangerous_command_blocked()
    
    print()
    print("=" * 60)
    print("TEST 2: 'rm -rf' blocked")
    print("=" * 60)
    await test_rm_rf_blocked()
    
    print()
    print("=" * 60)
    print("TEST 3: 'sudo' blocked")
    print("=" * 60)
    await test_sudo_blocked()
    
    print()
    print("=" * 60)
    print("TEST 4: Safe command allowed")
    print("=" * 60)
    await test_safe_command_allowed()
    
    print()
    print("=" * 60)
    print("TEST 5: Unknown tool")
    print("=" * 60)
    await test_unknown_tool()
    
    print()
    print("🎉 All tests passed!")


if __name__ == "__main__":
    asyncio.run(main())
