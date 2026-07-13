"""sandboxed hardening: exfil-denylist (shell_exec + read_file). god обходит.

Защищает sandboxed-режим (дефолт для хэндоффа): секреты (~/.ssh, config, /etc/shadow,
.env) не читаются. god/full — обходит (владелец).
"""
from unittest.mock import AsyncMock, MagicMock, patch

from caesar.tools.shell_files import ReadFileTool, ShellExecTool


def _fake_proc(rc=0, out=b"", err=b""):
    p = MagicMock()
    p.communicate = AsyncMock(return_value=(out, err))
    p.returncode = rc
    return p


async def test_sandboxed_shell_blocks_secret_path():
    t = ShellExecTool()
    t.access_mode = "sandboxed"
    t.god_mode = False
    r = await t.execute(command="cat ~/.ssh/id_rsa")
    assert not r.success
    assert "секрет" in r.error.lower()


async def test_god_shell_allows_secret_path():
    t = ShellExecTool()
    t.access_mode = "sandboxed"
    t.god_mode = True
    with patch("caesar.tools.shell_files.asyncio.create_subprocess_shell",
               AsyncMock(return_value=_fake_proc(0, out=b"key"))):
        r = await t.execute(command="cat ~/.ssh/id_rsa")
    assert r.success  # god bypasses exfil gate


async def test_sandboxed_shell_allows_normal_cat():
    t = ShellExecTool()
    t.access_mode = "sandboxed"
    t.god_mode = False
    with patch("caesar.tools.shell_files.asyncio.create_subprocess_shell",
               AsyncMock(return_value=_fake_proc(0, out=b"x"))):
        r = await t.execute(command="cat /etc/hostname")
    assert r.success  # не секретный путь — ОК


async def test_sandboxed_read_file_blocks_secret():
    t = ReadFileTool()
    t.access_mode = "sandboxed"
    t.god_mode = False
    r = await t.execute(path="~/.ssh/id_rsa")
    assert not r.success
    assert "секрет" in r.error.lower()


async def test_sandboxed_read_file_allows_normal():
    import os
    import tempfile

    fd, path = tempfile.mkstemp(suffix=".txt")
    os.write(fd, b"hello")
    os.close(fd)
    try:
        t = ReadFileTool()
        t.access_mode = "sandboxed"
        t.god_mode = False
        r = await t.execute(path=path)
        assert r.success
        assert r.data["content"] == "hello"
    finally:
        os.unlink(path)
