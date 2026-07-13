"""Конфигурация агента."""

import os
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional
import yaml


# === Пути ===
# Caesar ставится в user-space (как Ari):
# - код: ~/caesar (git clone)
# - venv: ~/.local/share/caesar/venv
# - данные: ~/.local/share/caesar/data
# - конфиг: ~/.config/caesar/
# - сокет: ~/.local/share/caesar/caesar.sock (или /tmp если первый не работает)
# - логи: ~/.local/share/caesar/log
# - skills: ~/.local/share/caesar/skills
# - self-knowledge: ~/.local/share/caesar/self
#
# Но если есть env-переменные CAESAR_CONFIG_DIR / CAESAR_DATA_DIR — используем их
# (systemd-сервисы их прокидывают).
# Это позволяет переопределить для dev-режима или multi-user.

HOME = Path.home()

# Код: parent от этого файла (caesar/config.py → caesar/ → корень проекта)
CODE_DIR = Path(__file__).resolve().parent.parent

# Директории — с переопределением через env
DATA_DIR = Path(os.environ.get(
    "CAESAR_DATA_DIR",
    str(HOME / ".local" / "share" / "caesar" / "data"),
))
CONFIG_DIR = Path(os.environ.get(
    "CAESAR_CONFIG_DIR",
    str(HOME / ".config" / "caesar"),
))
LOG_DIR = Path(os.environ.get(
    "CAESAR_LOG_DIR",
    str(HOME / ".local" / "share" / "caesar" / "log"),
))
RUN_DIR = Path(os.environ.get(
    "CAESAR_RUN_DIR",
    str(HOME / ".local" / "share" / "caesar"),
))

# Сокет — в RUN_DIR
SOCKET_PATH = RUN_DIR / "caesar.sock"

# Файлы
CONFIG_PATH = CONFIG_DIR / "config.yaml"
SECRETS_PATH = CONFIG_DIR / "secrets.yaml"
PERMISSIONS_PATH = CONFIG_DIR / "permissions.yaml"
DB_PATH = DATA_DIR / "caesar.db"
L3_VECTORS_PATH = DATA_DIR / "l3_vectors.db"
SKILLS_DIR = DATA_DIR / "skills"
SELF_DIR = DATA_DIR / "self"

# IS_DEV — только для логов, не влияет на пути
IS_DEV = (CODE_DIR / "pyproject.toml").exists()


@dataclass
class LLMConfig:
    """Конфигурация LLM-провайдера."""
    smart_provider: str = "openai"
    smart_model: str = "gpt-4o"
    smart_api_key: str = ""
    smart_base_url: Optional[str] = None
    
    cheap_provider: str = "openai"
    cheap_model: str = "gpt-4o-mini"
    cheap_api_key: str = ""
    cheap_base_url: Optional[str] = None


@dataclass
class TelegramConfig:
    """Конфигурация Telegram-бота."""
    bot_token: str = ""
    read_mode: str = "web"  # web | mtproto
    mtproto_api_id: str = ""
    mtproto_api_hash: str = ""
    # Авторизация: список chat_id, которым разрешено пользоваться ботом.
    # Заполняется при привязке (caesar pair) — владелец шлёт код боту, бот
    # записывает chat_id сюда. Пустой = бот не привязан (работает открыто +
    # ворнит «запусти caesar pair», без abrupt-локаута).
    allowed_chat_ids: list[int] = field(default_factory=list)
    # user_id владельца (TG user, не chat_id) — для распознавания владельца
    # внутри групп (где chat_id — отрицательный id группы).
    owner_user_id: int = 0
    # Группы: False (дефолт) — группы игнорируются; True — бот отвечает в группе.
    allow_group_chats: bool = False
    # Кто может командовать в группе (при allow_group_chats=True):
    #   "owner" — только владелец (по user_id), без повторной авторизации
    #   "all"   — любой участник группы (открытый супер-чат)
    group_access: str = "owner"


@dataclass
class STTConfig:
    """Конфигурация Speech-to-Text (распознавание голосовых)."""
    enabled: bool = False
    model: str = "base"
    language: str | None = None


@dataclass
class L3Config:
    """Конфигурация L3 векторной памяти (семантический поиск по прошлым диалогам)."""
    enabled: bool = False
    # Модель: multilingual-minilm (470MB, default), bge-m3 (2.2GB, best), minilm (80MB, en)
    model: str = "multilingual-minilm"


@dataclass
class CronConfig:
    """Конфигурация cron планировщика."""
    enabled: bool = False
    # Quiet hours — cron уведомления холдятся в это время
    quiet_hours_start: str = "23:00"  # 23:00
    quiet_hours_end: str = "08:00"    # 08:00
    # Dream cycle — ночной цикл обработки памяти
    dream_cycle_time: str = "02:00"   # 2:00 ночи
    # Morning briefing — утренний дайджест
    morning_briefing_time: str = "09:00"


@dataclass
class QueueConfig:
    """Конфигурация очереди задач."""
    max_interactive_workers: int = 5
    max_background_workers: int = 10
    reserved_cron_workers: int = 1
    waiting_for_user_timeout_hours: int = 24


@dataclass
class OrchestratorConfig:
    """Конфигурация оркестратора."""
    reflection_mode: str = "adaptive"  # adaptive | always | never | on_error_only
    reflection_interval_steps: int = 5
    reflection_on_subtask_switch: bool = True
    reflection_on_error: bool = True
    reflection_allow_explicit_call: bool = True
    
    max_steps_simple: int = 25
    max_steps_medium: int = 50
    max_steps_complex: int = 100
    
    max_tokens_simple: int = 500_000
    max_tokens_medium: int = 1_000_000
    max_tokens_complex: int = 2_000_000
    
    max_time_simple_min: int = 10
    max_time_medium_min: int = 60
    max_time_complex_min: int = 240
    
    max_retries_per_step: int = 3
    action_dedup_threshold: int = 3


@dataclass
class Config:
    """Полная конфигурация агента."""
    mode: str = "auto"  # auto | sandboxed | full
    timezone: str = "Europe/Moscow"
    language: str = "ru"
    
    llm: LLMConfig = field(default_factory=LLMConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    stt: STTConfig = field(default_factory=STTConfig)
    l3: L3Config = field(default_factory=L3Config)
    cron: CronConfig = field(default_factory=CronConfig)
    queue: QueueConfig = field(default_factory=QueueConfig)
    orchestrator: OrchestratorConfig = field(default_factory=OrchestratorConfig)
    
    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> "Config":
        """Загрузить конфиг из YAML файла."""
        config = cls()
        if not path.exists():
            return config
        
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        
        # Заполняем поля из YAML
        if "mode" in data:
            config.mode = data["mode"]
        if "timezone" in data:
            config.timezone = data["timezone"]
        if "language" in data:
            config.language = data["language"]
        
        if "llm" in data:
            for k, v in data["llm"].items():
                if hasattr(config.llm, k):
                    setattr(config.llm, k, v)
        
        if "telegram" in data:
            for k, v in data["telegram"].items():
                if hasattr(config.telegram, k):
                    setattr(config.telegram, k, v)
        
        if "stt" in data:
            for k, v in data["stt"].items():
                if hasattr(config.stt, k):
                    setattr(config.stt, k, v)
        
        if "l3" in data:
            for k, v in data["l3"].items():
                if hasattr(config.l3, k):
                    setattr(config.l3, k, v)
        
        if "cron" in data:
            for k, v in data["cron"].items():
                if hasattr(config.cron, k):
                    setattr(config.cron, k, v)
        
        if "queue" in data:
            for k, v in data["queue"].items():
                if hasattr(config.queue, k):
                    setattr(config.queue, k, v)
        
        if "orchestrator" in data:
            for k, v in data["orchestrator"].items():
                if hasattr(config.orchestrator, k):
                    setattr(config.orchestrator, k, v)
        
        return config
    
    def save(self, path: Path = CONFIG_PATH) -> None:
        """Сохранить конфиг в YAML.
        
        ВАЖНО: orchestrator настройки НЕ пишем в файл — используем дефолты из кода.
        Иначе старые значения (max_tokens_simple=50000) переопределяют новые дефолты
        после обновления Caesar. Если пользователь хочет переопределить — пусть
        редактирует config.yaml вручную.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "mode": self.mode,
            "timezone": self.timezone,
            "language": self.language,
            "llm": self.llm.__dict__,
            "telegram": self.telegram.__dict__,
            "stt": self.stt.__dict__,
            "l3": self.l3.__dict__,
            "cron": self.cron.__dict__,
            "queue": self.queue.__dict__,
            # orchestrator НЕ пишем — используем дефолты из кода
            # (иначе старые max_tokens переопределяют новые после update)
        }
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, allow_unicode=True, default_flow_style=False)
