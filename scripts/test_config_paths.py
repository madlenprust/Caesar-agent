"""Тест config.py path resolution — default'ы XDG (фикс stray-БД).

Корень бага: default DATA_DIR был CODE_DIR/data → CLI (без CAESAR_* env) смотрел в
папку проекта, а daemon (env-прокинут в XDG) — в ~/.local/share/caesar/data.
CLI плодил stray-БД + doctor говорил «DB не найдена». Теперь default = XDG.
"""
import importlib
import os
from pathlib import Path
from unittest.mock import patch

import caesar.config


def _reload_without_caesar_env():
    """Reload caesar.config с CAESAR_* env убранным (HOME и пр. сохранены)."""
    env_clean = {k: v for k, v in os.environ.items() if not k.startswith("CAESAR_")}
    with patch.dict(os.environ, env_clean, clear=True):
        importlib.reload(caesar.config)
        return (str(caesar.config.DATA_DIR), str(caesar.config.CONFIG_DIR),
                str(caesar.config.LOG_DIR), str(caesar.config.RUN_DIR))


def test_default_paths_are_xdg():
    """Без CAESAR_* env — default'ы XDG (совпадают с daemon'ом)."""
    try:
        data, cfg, log, run = _reload_without_caesar_env()
        home = str(Path.home())
        assert data == f"{home}/.local/share/caesar/data"
        assert cfg == f"{home}/.config/caesar"
        assert log == f"{home}/.local/share/caesar/log"
        assert run == f"{home}/.local/share/caesar/data"
    finally:
        importlib.reload(caesar.config)  # восстановить с реальным env


def test_env_override_still_works():
    """CAESAR_DATA_DIR env → переопределяет default (multi-user/dev)."""
    try:
        with patch.dict(os.environ, {"CAESAR_DATA_DIR": "/tmp/caesar-test-data"}):
            importlib.reload(caesar.config)
            assert str(caesar.config.DATA_DIR) == "/tmp/caesar-test-data"
    finally:
        importlib.reload(caesar.config)


def test_db_path_follows_data_dir():
    """DB_PATH = DATA_DIR/caesar.db — следует за DATA_DIR (env или default)."""
    try:
        with patch.dict(os.environ, {"CAESAR_DATA_DIR": "/tmp/caesar-test-data"}):
            importlib.reload(caesar.config)
            assert str(caesar.config.DB_PATH) == "/tmp/caesar-test-data/caesar.db"
    finally:
        importlib.reload(caesar.config)
