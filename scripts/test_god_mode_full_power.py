"""god mode = полная мощность: обходит exact_deny; remote_exec в sandboxed
запрещён, в god — выполняет. Subprocess мокается, чтобы rm -rf /etc не выполнялся
по-настоящему.
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


async def test_god_mode_bypasses_exact_deny_gate():
    """god mode — exact_deny пропускается; subprocess мокнут, до реального rm не доходит."""
    t = ShellExecTool()
    t.access_mode = "sandboxed"
    t.god_mode = True
    with patch("caesar.tools.shell_files.asyncio.create_subprocess_shell",
               AsyncMock(return_value=_fake_proc(0))):
        r = await t.execute(command="rm -rf /etc")
    assert r.success  # gate bypassed → (mocked) subprocess ran


async def test_full_mode_bypasses_exact_deny_gate():
    t = ShellExecTool()
    t.access_mode = "full"
    t.god_mode = False
    with patch("caesar.tools.shell_files.asyncio.create_subprocess_shell",
               AsyncMock(return_value=_fake_proc(0))):
        r = await t.execute(command="rm -rf /etc")
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
