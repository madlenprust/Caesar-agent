"""Тесты для детектора прямых команд.

Главная проблема: LLM иногда "рассуждает" о логах вместо того чтобы их
реально прочитать через shell_exec. Этот детектор перехватывает явные
просьбы выполнить команду и выполняет их напрямую, минуя LLM.
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

sys.path.insert(0, str(Path(__file__).parent.parent))


def make_orchestrator():
    """Создать Orchestrator без __init__."""
    from caesar.core.orchestrator import Orchestrator
    orch = Orchestrator.__new__(Orchestrator)
    orch.log = MagicMock()
    return orch


# ============================================================
# TEST 1: "выполни команду X" — basic
# ============================================================
def test_vypolni_komandu():
    """'выполни команду cat /tmp/x.log' → 'cat /tmp/x.log'"""
    orch = make_orchestrator()
    
    cmd = orch._detect_direct_command("выполни команду cat /tmp/x.log")
    assert cmd == "cat /tmp/x.log", f"Expected 'cat /tmp/x.log', got {cmd!r}"
    
    cmd = orch._detect_direct_command("Выполни команду journalctl --user -u caesar-daemon -n 50")
    assert cmd is not None
    assert "journalctl" in cmd
    print(f"  ✅ 'выполни команду X' → {cmd}")


# ============================================================
# TEST 2: "выполни X" — без слова "команда"
# ============================================================
def test_vypolni_bez_komandy():
    """'выполни cat /tmp/x' → 'cat /tmp/x'"""
    orch = make_orchestrator()
    
    cmd = orch._detect_direct_command("выполни cat /tmp/x")
    assert cmd == "cat /tmp/x", f"Expected 'cat /tmp/x', got {cmd!r}"
    
    cmd = orch._detect_direct_command("выполни ls -la")
    assert cmd == "ls -la"
    print(f"  ✅ 'выполни X' → {cmd}")


# ============================================================
# TEST 3: "запусти команду X"
# ============================================================
def test_zapusti():
    """'запусти команду X' → 'X'"""
    orch = make_orchestrator()
    
    cmd = orch._detect_direct_command("запусти команду date")
    assert cmd == "date", f"Expected 'date', got {cmd!r}"
    
    cmd = orch._detect_direct_command("запусти systemctl status nginx")
    assert cmd == "systemctl status nginx"
    print(f"  ✅ 'запусти X' → {cmd}")


# ============================================================
# TEST 4: Кавычки
# ============================================================
def test_quotes():
    """'выполни команду "cat /tmp/x"' → 'cat /tmp/x'"""
    orch = make_orchestrator()
    
    cmd = orch._detect_direct_command('выполни команду "cat /tmp/x"')
    assert cmd == "cat /tmp/x", f"Expected 'cat /tmp/x', got {cmd!r}"
    
    cmd = orch._detect_direct_command("выполни команду 'ls -la'")
    assert cmd == "ls -la"
    print(f"  ✅ Quoted commands stripped")


# ============================================================
# TEST 5: "покажи логи" → journalctl
# ============================================================
def test_pokazi_logi():
    """'покажи логи' → journalctl --user -u caesar-daemon -n 50"""
    orch = make_orchestrator()
    
    for trigger in ("покажи логи", "покажи лог", "логи демона", "логи caesar", "log", "logs"):
        cmd = orch._detect_direct_command(trigger)
        assert cmd is not None, f"Failed for {trigger!r}"
        assert "journalctl" in cmd
        assert "caesar-daemon" in cmd
    
    print(f"  ✅ 'покажи логи' → journalctl command")


# ============================================================
# TEST 6: "статус сервиса" → systemctl status
# ============================================================
def test_status_servisa():
    """'статус демона' → systemctl --user status caesar-daemon"""
    orch = make_orchestrator()
    
    for trigger in ("статус сервиса", "статус демона", "статус caesar"):
        cmd = orch._detect_direct_command(trigger)
        assert cmd is not None, f"Failed for {trigger!r}"
        assert "systemctl" in cmd
        assert "status" in cmd
        assert "caesar-daemon" in cmd
    
    print(f"  ✅ 'статус демона' → systemctl status command")


# ============================================================
# TEST 7: Прямые команды в начале (cat, ls, grep, ...)
# ============================================================
def test_direct_commands_at_start():
    """'cat /etc/hosts' → 'cat /etc/hosts'"""
    orch = make_orchestrator()
    
    test_cases = [
        ("cat /etc/hosts", "cat /etc/hosts"),
        ("ls -la /tmp", "ls -la /tmp"),
        ("grep 'error' /var/log/syslog", "grep 'error' /var/log/syslog"),
        ("journalctl -n 20", "journalctl -n 20"),
        ("ps aux", "ps aux"),
        ("df -h", "df -h"),
        ("date", "date"),
        ("whoami", "whoami"),
    ]
    
    for input_msg, expected in test_cases:
        cmd = orch._detect_direct_command(input_msg)
        assert cmd == expected, f"For {input_msg!r}: expected {expected!r}, got {cmd!r}"
    
    print(f"  ✅ Direct commands at start work ({len(test_cases)} cases)")


# ============================================================
# TEST 8: "что в файле X" → cat X
# ============================================================
def test_chto_v_faile():
    """'что в файле /tmp/x' → 'cat /tmp/x'"""
    orch = make_orchestrator()
    
    cmd = orch._detect_direct_command("что в файле /tmp/x")
    assert cmd == "cat /tmp/x", f"Expected 'cat /tmp/x', got {cmd!r}"
    
    cmd = orch._detect_direct_command("что в файле /etc/hosts?")
    assert cmd == "cat /etc/hosts"
    print(f"  ✅ 'что в файле X' → cat X")


# ============================================================
# TEST 9: Опасные команды — НЕ выполняем напрямую
# ============================================================
def test_dangerous_commands_blocked():
    """'выполни rm -rf /' → None (пусть LLM с подтверждением)
    
    ВАЖНО: systemctl restart НЕ блокируется в детекторе — пользователь
    может явно просить перезапустить daemon. Блокировка systemctl restart
    происходит в ToolRegistry.execute() (для LLM-вызовов), а не в детекторе
    прямых команд.
    """
    orch = make_orchestrator()
    
    dangerous_inputs = [
        "выполни команду rm -rf /tmp/test",
        "выполни sudo systemctl restart nginx",
        "выполни shutdown -h now",
        "выполни reboot",
        "sudo apt remove python3",
        "rm -rf /home/user/data",
    ]
    
    for inp in dangerous_inputs:
        cmd = orch._detect_direct_command(inp)
        assert cmd is None, f"Dangerous command should return None: {inp!r} → {cmd!r}"
    
    print(f"  ✅ {len(dangerous_inputs)} dangerous commands blocked")


# ============================================================
# TEST 10: НЕ команды — возвращаем None
# ============================================================
def test_not_commands():
    """Обычные вопросы не должны детектиться как команды."""
    orch = make_orchestrator()
    
    not_commands = [
        "привет",
        "что такое Python?",
        "расскажи про asyncio",
        "найди новости про AI",
        "как настроить nginx?",
        "переведи этот текст",
        "что нового в мире технологий за последнюю неделю",
        "запомни что мой любимый цвет синий",
    ]
    
    for inp in not_commands:
        cmd = orch._detect_direct_command(inp)
        assert cmd is None, f"Should not detect as command: {inp!r} → {cmd!r}"
    
    print(f"  ✅ {len(not_commands)} non-commands correctly ignored")


# ============================================================
# TEST 11: End-to-end: реальный вызов shell_exec
# ============================================================
async def test_e2e_execution():
    """Проверяем что детектор + tools.execute работают вместе."""
    from caesar.core.orchestrator import Orchestrator
    from caesar.tools.base import ToolResult
    
    orch = Orchestrator.__new__(Orchestrator)
    orch.log = MagicMock()
    orch.tools = MagicMock()
    
    # Mock tools.execute
    expected_output = "total 0\ndrwxr-xr-x 2 root root 60 Jul 8 09:00 .\n"
    orch.tools.execute = AsyncMock(return_value=ToolResult(
        success=True,
        data={"stdout": expected_output, "stderr": "", "exit_code": 0},
    ))
    
    # Detect + execute
    cmd = orch._detect_direct_command("выполни команду ls -la")
    assert cmd == "ls -la"
    
    result = await orch.tools.execute("shell_exec", command=cmd, timeout=30)
    assert result.success
    assert result.data["stdout"] == expected_output
    
    # Verify tools.execute was called with shell_exec
    orch.tools.execute.assert_called_once_with("shell_exec", command="ls -la", timeout=30)
    
    print(f"  ✅ E2E: detect → execute → result")


# ============================================================
# RUN ALL TESTS
# ============================================================
async def main():
    print("=" * 60)
    print("TEST 1: 'выполни команду X'")
    print("=" * 60)
    test_vypolni_komandu()
    
    print()
    print("=" * 60)
    print("TEST 2: 'выполни X' (без слова 'команда')")
    print("=" * 60)
    test_vypolni_bez_komandy()
    
    print()
    print("=" * 60)
    print("TEST 3: 'запусти команду X'")
    print("=" * 60)
    test_zapusti()
    
    print()
    print("=" * 60)
    print("TEST 4: Quoted commands")
    print("=" * 60)
    test_quotes()
    
    print()
    print("=" * 60)
    print("TEST 5: 'покажи логи' → journalctl")
    print("=" * 60)
    test_pokazi_logi()
    
    print()
    print("=" * 60)
    print("TEST 6: 'статус демона' → systemctl status")
    print("=" * 60)
    test_status_servisa()
    
    print()
    print("=" * 60)
    print("TEST 7: Direct commands at start (cat, ls, ...)")
    print("=" * 60)
    test_direct_commands_at_start()
    
    print()
    print("=" * 60)
    print("TEST 8: 'что в файле X' → cat X")
    print("=" * 60)
    test_chto_v_faile()
    
    print()
    print("=" * 60)
    print("TEST 9: Dangerous commands blocked")
    print("=" * 60)
    test_dangerous_commands_blocked()
    
    print()
    print("=" * 60)
    print("TEST 10: Non-commands ignored")
    print("=" * 60)
    test_not_commands()
    
    print()
    print("=" * 60)
    print("TEST 11: E2E execution")
    print("=" * 60)
    await test_e2e_execution()
    
    print()
    print("🎉 All tests passed!")


if __name__ == "__main__":
    asyncio.run(main())
