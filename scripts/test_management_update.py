"""Тесты caesar update — stash-safety (audit M1).

cmd_update раньше делал слепой `git reset --hard` и губил .env/локальные правки.
Теперь (0.11.2) перед reset — `git stash` (если есть изменения), после — `git stash pop`
(best-effort; при конфликте остаётся в stash, не теряется).

Покрытие:
- clean tree: stash НЕ вызывается, pop НЕ вызывается, reset --hard есть.
- dirty tree: stash push вызывается, pop вызывается.
- pop conflict (rc=1): warning, return 0 (reset уже прошёл), без крэша, правки в stash.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from caesar import management


def _result(rc=0, out="", err=""):
    return SimpleNamespace(returncode=rc, stdout=out, stderr=err)


def _make_run(dirty_output: str, pop_rc: int = 0, log_output: str = "abc1234 fix\n"):
    """Мок subprocess.run: git-симуляция update-flow. Возвращает fake CompletedProcess."""
    calls: list[list[str]] = []
    src_calls: list[list[str]] = calls  # захватим для ассертов

    def run(cmd, *a, **kw):
        calls.append(list(cmd))
        joined = " ".join(cmd)
        if "rev-parse" in joined and "--abbrev-ref" in joined:
            return _result(0, "main\n")
        if "rev-parse" in joined and "HEAD" in joined:
            return _result(0, "abc123\n")
        if "fetch" in joined:
            return _result(0)
        if "status" in joined and "--porcelain" in joined:
            return _result(0, dirty_output)
        if "stash" in joined and "push" in joined:
            # stash создаётся только если есть что прятать (dirty)
            if dirty_output.strip():
                return _result(0, "Saved working directory and index state WIP")
            return _result(0, "No local changes to save")
        if "reset" in joined and "--hard" in joined:
            return _result(0)
        if "stash" in joined and "pop" in joined:
            return _result(pop_rc, "" if pop_rc == 0 else "conflict", "merge conflict" if pop_rc else "")
        if "log" in joined and "--oneline" in joined:
            return _result(0, log_output)
        if "systemctl" in joined:
            return _result(0)
        # pip install и прочее
        return _result(0)

    mock = MagicMock()
    mock.run.side_effect = run
    return mock, calls


def _ran(calls, *prefixes) -> bool:
    """Был ли вызов subprocess.run с командой, начинающейся с одного из prefixes."""
    for cmd in calls:
        for p in prefixes:
            if cmd[: len(p)] == list(p):
                return True
    return False


async def _run_update(dirty_output: str, pop_rc: int = 0):
    args = SimpleNamespace(no_restart=True)  # skip stop/restart daemon
    mock, calls = _make_run(dirty_output, pop_rc)
    import tempfile
    from pathlib import Path
    tmp = Path(tempfile.mkdtemp())
    with patch("caesar.management.subprocess", mock), \
         patch("caesar.management.DATA_DIR", tmp), \
         patch("caesar.config.CONFIG_PATH", tmp / "nope-config.yaml"):
        rc = await management.cmd_update(args)
    return rc, calls


async def test_update_clean_tree_no_stash():
    """Чистое дерево → stash НЕ вызывается, pop НЕ вызывается, reset --hard есть."""
    rc, calls = await _run_update(dirty_output="")
    assert rc == 0
    assert _ran(calls, ("git", "reset", "--hard"))
    assert not _ran(calls, ("git", "stash",))
    assert not _ran(calls, ("git", "stash", "pop"))


async def test_update_dirty_tree_stashes_and_pops():
    """Грязное дерево → stash push + stash pop (правки не губятся)."""
    rc, calls = await _run_update(dirty_output=" M caesar/x.py\n?? y.txt\n")
    assert rc == 0
    assert _ran(calls, ("git", "status", "--porcelain"))
    assert _ran(calls, ("git", "stash", "push"))
    assert _ran(calls, ("git", "reset", "--hard"))
    assert _ran(calls, ("git", "stash", "pop"))


async def test_update_pop_conflict_keeps_stash_no_crash():
    """Конфликт pop (rc=1) → warning, return 0 (reset уже прошёл), без крэша."""
    rc, calls = await _run_update(dirty_output=" M x.py\n", pop_rc=1)
    assert rc == 0  # reset прошёл успешно — update не падает из-за конфликта pop
    assert _ran(calls, ("git", "stash", "push"))
    assert _ran(calls, ("git", "stash", "pop"))  # пробовал pop, не вышло — осталось в stash


async def test_update_reset_failure_returns_1():
    """Если сам reset --hard упал (rc!=0) — update возвращает 1 (не продолжаем)."""
    mock, calls = _make_run(dirty_output="", pop_rc=0)

    def run(cmd, *a, **kw):
        calls.append(list(cmd))
        joined = " ".join(cmd)
        if "rev-parse" in joined and "--abbrev-ref" in joined:
            return _result(0, "main\n")
        if "rev-parse" in joined and "HEAD" in joined:
            return _result(0, "abc123\n")
        if "reset" in joined and "--hard" in joined:
            return _result(1, "", "fatal: refusing to fetch")  # сбой reset
        return _result(0)

    mock.run.side_effect = run
    args = SimpleNamespace(no_restart=True)
    import tempfile
    from pathlib import Path
    tmp = Path(tempfile.mkdtemp())
    with patch("caesar.management.subprocess", mock), \
         patch("caesar.management.DATA_DIR", tmp), \
         patch("caesar.config.CONFIG_PATH", tmp / "nope.yaml"):
        rc = await management.cmd_update(args)
    assert rc == 1
