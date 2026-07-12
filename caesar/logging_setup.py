"""Логирование — гибрид journald + файлы (см. roadmap раздел 14.6)."""

import logging
import logging.handlers
import sys
from pathlib import Path

from caesar.config import LOG_DIR


def setup_logging(level: str = "INFO") -> logging.Logger:
    """Настроить логирование агента.
    
    Гибрид:
    - stderr → journald (через systemd-сервис)
    - файлы в LOG_DIR/agent.log, tasks.log, errors.log
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    
    root = logging.getLogger("caesar")
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    
    # Также настраиваем корневой логгер для перехвата всего
    logging.getLogger().setLevel(logging.WARNING)
    
    # Очищаем существующие хендлеры (чтобы не дублировать при повторном вызове)
    root.handlers.clear()
    
    # Формат
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    
    # 1. stderr → journald подхватит
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(fmt)
    root.addHandler(stderr_handler)
    
    # Также добавляем на корневой логгер чтобы TG adapter (который пишет напрямую в stderr) тоже попадал
    logging.getLogger().addHandler(stderr_handler)
    
    # 2. Основной лог-файл с ротацией
    file_handler = logging.handlers.RotatingFileHandler(
        LOG_DIR / "caesar.log",
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)
    
    # 3. Отдельный лог ошибок
    error_handler = logging.handlers.RotatingFileHandler(
        LOG_DIR / "errors.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(fmt)
    root.addHandler(error_handler)
    
    return root


def get_logger(name: str) -> logging.Logger:
    """Получить логгер для модуля."""
    return logging.getLogger(f"caesar.{name}")
