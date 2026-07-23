"""god mode = полная мощность: обходит МЯГКИЕ гейты (requires_permission,
references_secret_path), но НЕ exact_deny — снос локальной системы всегда включён.
Диск-format (mkfs/dd) разрешён; rm -rf /etc блок даже в god/full.
remote_exec в sandboxed запрещён, в god — выполняет. Subprocess мокается.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from caesar.tools.shell_files import RemoteExecTool, ShellExecTool


def _fake_proc(rc=0, out=b"", err=b""):
    p = MagicMock()
    p.communicate = AsyncMock(return_value=(out, err))
    p.returncode = rc
    return p


async def test_sandboxed_blocks_rm_rf():
    t = ShellExecTool()
    t.access_mode = "sandboxed"
    t.god_mode = False
    r = await t.execute(command="rm -rf /etc")
    assert not r.success
    assert "exact_deny" in r.error


async def test_god_mode_does_not_bypass_system_wipe():
    """exact_deny (снос системы) — всегда включён, god НЕ обходит."""
    t = ShellExecTool()
    t.access_mode = "sandboxed"
    t.god_mode = True
    r = await t.execute(command="rm -rf /etc")
    assert not r.success
    assert "exact_deny" in r.error


async def test_full_mode_does_not_bypass_system_wipe():
    """full mode тоже не обходит снос системы."""
    t = ShellExecTool()
    t.access_mode = "full"
    t.god_mode = False
    r = await t.execute(command="rm -rf /etc")
    assert not r.success
    assert "exact_deny" in r.error


async def test_god_mode_allows_disk_format():
    """Форматирование диска (mkfs) разрешено в god — subprocess мокнут."""
    t = ShellExecTool()
    t.access_mode = "sandboxed"
    t.god_mode = True
    with patch("caesar.tools.shell_files.asyncio.create_subprocess_shell",
               AsyncMock(return_value=_fake_proc(0))):
        r = await t.execute(command="mkfs.ext4 /dev/sda")
    assert r.success


async def test_remote_exec_sandboxed_blocked():
    t = RemoteExecTool()
    t.access_mode = "sandboxed"
    t.god_mode = False
    r = await t.execute(host="localhost", command="echo hi")
    assert not r.success
    assert "god" in r.error.lower() or "full" in r.error.lower()


async def test_remote_exec_god_bypasses_gate():
    t = RemoteExecTool()
    t.access_mode = "sandboxed"
    t.god_mode = True
    with patch("caesar.tools.shell_files.asyncio.create_subprocess_exec",
               AsyncMock(return_value=_fake_proc(0, out=b"hi\n"))):
        r = await t.execute(host="neighbor", command="echo hi", timeout=5)
    assert r.success
    assert r.data["stdout"] == "hi\n"


async def test_remote_exec_password_path_uses_paramiko():
    """password → paramiko (мок _paramiko_run); пароль не доходит до subprocess."""
    t = RemoteExecTool()
    t.access_mode = "sandboxed"
    t.god_mode = True
    with patch("caesar.tools.shell_files._paramiko_run", return_value=(0, "hello\n", "")) as mock_run:
        r = await t.execute(host="neighbor", command="echo hello", password="s3cret", timeout=5)
    assert r.success
    assert r.data["stdout"] == "hello\n"
    mock_run.assert_called_once()  # paramiko path, not subprocess ssh
    # пароль не утёк в результат
    assert "s3cret" not in str(r.data) and "s3cret" not in (r.error or "")


async def test_remote_exec_password_failure_does_not_leak_password():
    t = RemoteExecTool()
    t.access_mode = "sandboxed"
    t.god_mode = True
    def _raise(*a, **k):
        raise RuntimeError("auth failed for s3cret")
    with patch("caesar.tools.shell_files._paramiko_run", side_effect=_raise):
        r = await t.execute(host="n", command="x", password="s3cret", timeout=5)
    assert not r.success
    assert "s3cret" not in (r.error or "")  # пароль замаскирован
    assert "***" in r.error  # и сообщение об ошибке осталось информативным
