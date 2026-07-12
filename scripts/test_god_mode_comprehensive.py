"""Комплексные тесты для god_mode и direct_command detector.

Покрываем ВСЕ пути:
1. _detect_direct_command с user_id параметром
2. god_mode активирован → dangerous_patterns не блокируются
3. god_mode не активирован → dangerous_patterns блокируются
4. full mode → то же что god_mode
5. sandboxed mode без god_mode → блокировки работают
6. Все триггеры: 'рестартни daemon', 'выполни команду X', etc.
"""

import asyncio
import sys
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

sys.path.insert(0, str(Path(__file__).parent.parent))


def make_orchestrator(storage=None, access_mode="sandboxed"):
    """Создать Orchestrator без __init__."""
    from caesar.core.orchestrator import Orchestrator
    orch = Orchestrator.__new__(Orchestrator)
    orch.log = MagicMock()
    orch.storage = storage
    orch.tools = MagicMock()
    orch.tools.access_mode = access_mode
    return orch


def make_storage_with_god_mode(god_mode=False):
    """Создать mock storage с god_mode."""
    storage = MagicMock()
    storage.get_user_god_mode = MagicMock(return_value=god_mode)
    return storage


# ============================================================
# TEST 1: _detect_direct_command принимает user_id параметр
# ============================================================
def test_user_id_parameter():
    """Метод должен принимать user_id как второй параметр."""
    orch = make_orchestrator()
    
    # Без user_id — не должно падать
    cmd = orch._detect_direct_command("выполни команду ls")
    assert cmd == "ls", f"Expected 'ls', got {cmd!r}"
    
    # С user_id — не должно падать
    cmd = orch._detect_direct_command("выполни команду ls", "user-123")
    assert cmd == "ls", f"Expected 'ls', got {cmd!r}"
    
    print("  ✅ user_id parameter accepted")


# ============================================================
# TEST 2: god_mode=True → sudo не блокируется
# ============================================================
def test_god_mode_allows_sudo():
    """В god mode 'выполни sudo X' должно детектиться (не возвращать None)."""
    storage = make_storage_with_god_mode(god_mode=True)
    orch = make_orchestrator(storage=storage, access_mode="sandboxed")
    
    cmd = orch._detect_direct_command("выполни команду sudo apt update", "user-123")
    assert cmd == "sudo apt update", f"god_mode should allow sudo, got {cmd!r}"
    
    print("  ✅ god_mode allows sudo")


# ============================================================
# TEST 3: god_mode=False → sudo блокируется
# ============================================================
def test_no_god_mode_blocks_sudo():
    """Без god mode 'выполни sudo X' должно вернуть None."""
    storage = make_storage_with_god_mode(god_mode=False)
    orch = make_orchestrator(storage=storage, access_mode="sandboxed")
    
    cmd = orch._detect_direct_command("выполни команду sudo apt update", "user-123")
    assert cmd is None, f"Without god_mode, sudo should be blocked, got {cmd!r}"
    
    print("  ✅ no god_mode blocks sudo")


# ============================================================
# TEST 4: full mode → sudo не блокируется (даже без god_mode)
# ============================================================
def test_full_mode_allows_sudo():
    """В full mode 'выполни sudo X' должно детектиться."""
    storage = make_storage_with_god_mode(god_mode=False)
    orch = make_orchestrator(storage=storage, access_mode="full")
    
    cmd = orch._detect_direct_command("выполни команду sudo apt update", "user-123")
    assert cmd == "sudo apt update", f"full mode should allow sudo, got {cmd!r}"
    
    print("  ✅ full_mode allows sudo")


# ============================================================
# TEST 5: god_mode=True → rm -rf не блокируется
# ============================================================
def test_god_mode_allows_rm_rf():
    """В god mode 'выполни rm -rf X' должно детектиться."""
    storage = make_storage_with_god_mode(god_mode=True)
    orch = make_orchestrator(storage=storage, access_mode="sandboxed")
    
    cmd = orch._detect_direct_command("выполни команду rm -rf /tmp/test", "user-123")
    assert cmd == "rm -rf /tmp/test", f"god_mode should allow rm -rf, got {cmd!r}"
    
    print("  ✅ god_mode allows rm -rf")


# ============================================================
# TEST 6: god_mode=False → rm -rf блокируется
# ============================================================
def test_no_god_mode_blocks_rm_rf():
    """Без god mode 'выполни rm -rf X' должно вернуть None."""
    storage = make_storage_with_god_mode(god_mode=False)
    orch = make_orchestrator(storage=storage, access_mode="sandboxed")
    
    cmd = orch._detect_direct_command("выполни команду rm -rf /tmp/test", "user-123")
    assert cmd is None, f"Without god_mode, rm -rf should be blocked, got {cmd!r}"
    
    print("  ✅ no god_mode blocks rm -rf")


# ============================================================
# TEST 7: 'рестартни daemon' → systemctl restart
# ============================================================
def test_restart_daemon_trigger():
    """'рестартни daemon' возвращает systemctl restart."""
    orch = make_orchestrator()
    
    for trigger in ("рестартни daemon", "рестартни демона", "рестартни caesar",
                     "перезапусти daemon", "перезапусти демона", "рестартни бота"):
        cmd = orch._detect_direct_command(trigger, "user-123")
        assert cmd == "systemctl --user restart caesar-daemon", (
            f"For {trigger!r}: expected systemctl restart, got {cmd!r}"
        )
    
    print("  ✅ restart triggers work")


# ============================================================
# TEST 8: 'systemctl --user restart caesar-daemon' напрямую
# ============================================================
def test_direct_systemctl_restart():
    """Прямой ввод 'systemctl --user restart caesar-daemon' — теперь детектится
    как прямая команда (через 'systemctl ' префикс) и перехватывается is_daemon_restart."""
    storage = make_storage_with_god_mode(god_mode=True)
    orch = make_orchestrator(storage=storage, access_mode="sandboxed")
    
    # Теперь 'systemctl ' в direct_command_starts → детектится напрямую
    cmd = orch._detect_direct_command("systemctl --user restart caesar-daemon", "user-123")
    assert cmd == "systemctl --user restart caesar-daemon", (
        f"Direct systemctl restart should be detected, got {cmd!r}"
    )
    
    print("  ✅ direct systemctl restart detected (no LLM needed)")


# ============================================================
# TEST 9: 'выполни команду systemctl --user restart caesar-daemon'
# ============================================================
def test_vypolni_systemctl_restart():
    """'выполни команду systemctl --user restart caesar-daemon' — должно детектиться."""
    storage = make_storage_with_god_mode(god_mode=True)
    orch = make_orchestrator(storage=storage, access_mode="sandboxed")
    
    cmd = orch._detect_direct_command("выполни команду systemctl --user restart caesar-daemon", "user-123")
    assert cmd == "systemctl --user restart caesar-daemon", (
        f"Expected systemctl restart, got {cmd!r}"
    )
    
    print("  ✅ 'выполни команду systemctl restart' detected")


# ============================================================
# TEST 10: Safe commands work without god_mode
# ============================================================
def test_safe_commands_no_god_mode():
    """Безопасные команды работают без god_mode."""
    storage = make_storage_with_god_mode(god_mode=False)
    orch = make_orchestrator(storage=storage, access_mode="sandboxed")
    
    # cat, ls, echo — безопасные
    assert orch._detect_direct_command("выполни команду cat /etc/hosts", "user-123") == "cat /etc/hosts"
    assert orch._detect_direct_command("выполни команду ls -la", "user-123") == "ls -la"
    assert orch._detect_direct_command("выполни команду echo hello", "user-123") == "echo hello"
    
    print("  ✅ safe commands work without god_mode")


# ============================================================
# TEST 11: Non-commands return None
# ============================================================
def test_non_commands():
    """Обычные вопросы не детектятся как команды."""
    orch = make_orchestrator()
    
    for msg in ("привет", "что такое Python?", "найди новости", "расскажи про AI"):
        cmd = orch._detect_direct_command(msg, "user-123")
        assert cmd is None, f"{msg!r} should not be detected as command, got {cmd!r}"
    
    print("  ✅ non-commands correctly ignored")


# ============================================================
# TEST 12: Storage None → god_mode=False (no crash)
# ============================================================
def test_storage_none():
    """Если storage=None, god_mode проверка не должна падать."""
    orch = make_orchestrator(storage=None, access_mode="sandboxed")
    
    # Не должно падать с AttributeError
    cmd = orch._detect_direct_command("выполни команду ls", "user-123")
    assert cmd == "ls"
    
    print("  ✅ storage=None handled gracefully")


# ============================================================
# TEST 13: user_id empty → god_mode=False (no crash)
# ============================================================
def test_empty_user_id():
    """Если user_id пустой, god_mode проверка не должна падать."""
    storage = make_storage_with_god_mode(god_mode=True)
    orch = make_orchestrator(storage=storage, access_mode="sandboxed")
    
    # user_id="" → god_mode не проверяется → sudo блокируется
    cmd = orch._detect_direct_command("выполни команду sudo apt update", "")
    assert cmd is None, f"Empty user_id should not trigger god_mode, got {cmd!r}"
    
    print("  ✅ empty user_id handled gracefully")


# ============================================================
# RUN ALL TESTS
# ============================================================
def main():
    print("=" * 60)
    print("TEST 1: user_id parameter")
    print("=" * 60)
    test_user_id_parameter()
    
    print()
    print("=" * 60)
    print("TEST 2: god_mode allows sudo")
    print("=" * 60)
    test_god_mode_allows_sudo()
    
    print()
    print("=" * 60)
    print("TEST 3: no god_mode blocks sudo")
    print("=" * 60)
    test_no_god_mode_blocks_sudo()
    
    print()
    print("=" * 60)
    print("TEST 4: full_mode allows sudo")
    print("=" * 60)
    test_full_mode_allows_sudo()
    
    print()
    print("=" * 60)
    print("TEST 5: god_mode allows rm -rf")
    print("=" * 60)
    test_god_mode_allows_rm_rf()
    
    print()
    print("=" * 60)
    print("TEST 6: no god_mode blocks rm -rf")
    print("=" * 60)
    test_no_god_mode_blocks_rm_rf()
    
    print()
    print("=" * 60)
    print("TEST 7: restart daemon triggers")
    print("=" * 60)
    test_restart_daemon_trigger()
    
    print()
    print("=" * 60)
    print("TEST 8: direct systemctl restart → LLM")
    print("=" * 60)
    test_direct_systemctl_restart()
    
    print()
    print("=" * 60)
    print("TEST 9: 'выполни команду systemctl restart'")
    print("=" * 60)
    test_vypolni_systemctl_restart()
    
    print()
    print("=" * 60)
    print("TEST 10: safe commands without god_mode")
    print("=" * 60)
    test_safe_commands_no_god_mode()
    
    print()
    print("=" * 60)
    print("TEST 11: non-commands")
    print("=" * 60)
    test_non_commands()
    
    print()
    print("=" * 60)
    print("TEST 12: storage=None")
    print("=" * 60)
    test_storage_none()
    
    print()
    print("=" * 60)
    print("TEST 13: empty user_id")
    print("=" * 60)
    test_empty_user_id()
    
    print()
    print("🎉 ALL 13 TESTS PASSED!")


if __name__ == "__main__":
    main()
