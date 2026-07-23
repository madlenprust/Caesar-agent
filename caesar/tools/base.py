"""Инструменты агента.

См. roadmap раздел 11.

35 инструментов в V1, 7 категорий:
1. Shell + Files: shell_exec, read_file, write_file, edit_file, find_files, grep
2. Интернет: web_search, web_fetch, http_request
3. Источники: rss_read, tg_read_channel, hn_search, reddit_search, ...
4. Коммуникации: tg_post, email_send, tg_delete_message, webhook_call
5. Документы: parse_pdf, parse_docx, parse_xlsx, parse_csv
6. Память: memory_search, memory_add_fact, skill_find, skill_save, memory_compact
7. Self-knowledge: self_read, self_edit, self_install_package, self_scan, self_test
"""

import re
import shlex
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from caesar.logging_setup import get_logger


# exact_deny — чёрный список СНЯСА ЛОКАЛЬНОЙ СИСТЕМЫ (раздел 11.1).
# Срабатывает ВСЕГДА, до выполнения, и НЕ отключается ни god_mode, ни full
# (PRINCIPLES #9: «Не отключается через UI/чат»).
# Политика: блокируем только снос машины, где крутится агент — rm -rf на корень/
# системный каталог, fork bomb, chmod/chown -R на /, disable sshd/networking,
# self-uninstall pip/npm. Форматирование диска (mkfs/dd of=/dev/>/dev) и снос
# на СОСЕДНЕЙ машине (RemoteExecTool) — РАЗРЕШЕНЫ (владелец берёт ответственность;
# soft-gate requires_permission в sandboxed спросит подтверждение).
# Устойчив к обходам: chaining (&& ; | ||), command substitution ($(...) `...`),
# eval "...", и reorder флагов (rm -fr /, rm -r -f /).

# Защищённые от rm -rf пути: корень и системные каталоги верхнего уровня.
_RM_PROTECTED_PREFIXES = (
    "/", "/etc", "/var", "/home", "/usr", "/boot", "/bin", "/sbin",
    "/lib", "/lib64", "/root", "/opt", "/proc", "/sys", "/run", "/srv",
)
_RM_ALLOWED_TMP = ("/tmp", "/var/tmp")


def _split_subcommands(command: str) -> list[str]:
    """Разбить shell-команду на под-команды, раскрыв $(...), `...` и eval "...".

    Quote-aware: разделители (&& ; || |) режут только ВНЕ кавычек, иначе
    `python3 -c "import os; os.system(...)"` разрезалось бы по `;` внутри
    аргумента. Так опасная операция внутри любой ветки составной команды или
    подстановки попадает под проверку exact_deny.
    """
    # 1) Раскрываем command substitution, backticks и eval — их содержимое
    #    тоже проверяем как отдельные под-команды.
    expanded = re.sub(r"\$\(([^)]*)\)", r" \1 ", command)
    expanded = re.sub(r"`([^`]*)`", r" \1 ", expanded)
    expanded = re.sub(r'\beval\s+["\']([^"\']*)["\']', r" \1 ", expanded)

    parts: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i = 0
    n = len(expanded)
    while i < n:
        c = expanded[i]
        if quote:
            buf.append(c)
            if c == "\\" and quote == '"':  # escape внутри двойных кавычек
                if i + 1 < n:
                    buf.append(expanded[i + 1])
                    i += 1
            elif c == quote:
                quote = None
            i += 1
            continue
        if c in ('"', "'"):
            quote = c
            buf.append(c)
            i += 1
            continue
        two = expanded[i:i + 2]
        if two in ("&&", "||"):
            parts.append("".join(buf))
            buf = []
            i += 2
            continue
        if c in (";", "|"):
            parts.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(c)
        i += 1
    if buf:
        parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def _rm_recursive_force_targets(cmd: str) -> str | None:
    """Если cmd = rm с рекурсией И форсированием (любой порядок/группировка флагов)
    на КОРЕНЬ системы/дома — вернуть этот путь, иначе None.

    Политика roots-only: блокируем только снос системы — rm -rf на корень (/)
    или системный каталог верхнего уровня (/etc, /usr, /var, /home, ...), либо на
    корень дома (~, ~user). Подкаталоги (~/old_project, /var/log/old) разрешены —
    это легитимная зачистка, не снос системы.
    """
    try:
        argv = shlex.split(cmd, posix=True)
    except ValueError:
        argv = cmd.split()
    if not argv or argv[0] != "rm":
        return None
    flag_chars: set[str] = set()
    targets: list[str] = []
    for a in argv[1:]:
        if a.startswith("--"):
            if a in ("--recursive",):
                flag_chars.add("r")
            elif a in ("--force",):
                flag_chars.add("f")
            continue
        if a.startswith("-") and len(a) > 1:
            flag_chars.update(a[1:])  # буквы после '-', в любом порядке
            continue
        targets.append(a)
    if not ("r" in flag_chars and "f" in flag_chars):
        return None
    for t in targets:
        # /tmp, /var/tmp — всегда разрешены
        if any(t == p or t.startswith(p + "/") for p in _RM_ALLOWED_TMP):
            continue
        # Нормализуем хвостовой слэш: /etc/ → /etc; // → "" (корень).
        tn = t.rstrip("/")
        # Снос системы = rm -rf на КОРЕНЬ: /, системный каталог верхнего уровня
        # (/etc, /usr, /var, /home, ...) или корень дома (~, ~user).
        # Подкаталоги (~/old_project, /var/log/old) — НЕ блокируем.
        if tn == "" or tn == "~" or tn in _RM_PROTECTED_PREFIXES:
            return t
        # ~user (корень чужого дома). Подкаталог ~user/x — разрешён.
        if re.match(r"^~\w+$", tn):
            return t
    return None


# Паттерны (не-rm), проверяемые по каждой изолированной под-команде.
# Только СНЯС ЛОКАЛЬНОЙ СИСТЕМЫ. Форматирование диска (mkfs/dd of=/dev/>/dev)
# ЗДЕСЬ НЕТ — оно разрешено (владелец берёт ответственность; sandboxed спросит
# подтверждение через requires_permission).
_DENY_PER_SUBCMD: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^:\s*\(\s*\)\s*\{.*\}.*;.*:"), "fork bomb :(){ :|:& };:"),
    (re.compile(r"^chmod\s+-R\s+(?:000|777)\s+/(?!tmp/|var/tmp/)"), "chmod -R 000/777 /"),
    (re.compile(r"^chown\s+-R\s+\S+\s+/(?!tmp/|var/tmp/)"), "chown -R /"),
    (re.compile(r"^systemctl\s+(?:disable|mask)\s+(?:sshd|networking|systemd-network)\b"), "disable sshd/networking"),
    (re.compile(r"^pip\s+uninstall\s+-y\s+pip\b"), "uninstall pip"),
    (re.compile(r"^npm\s+uninstall\s+-g\s+npm\b"), "uninstall npm"),
    # sh -c как попытка обойти прямой запрет на снос системы (rm -r/chmod-R/chown-R).
    # mkfs/dd не ловим — форматирование диска разрешено.
    (re.compile(r"^(?:bash|sh|zsh|dash)\s+-c\b.*\b(?:rm\s+-r|chmod\s+-R|chown\s+-R)"), "sh -c со сносом системы"),
]


# Паттерны, проверяемые по ЦЕЛОЙ команде (не режутся сплиттером —
# их сигнатура размазана по составной команде, напр. fork bomb :(){ :|:& };:).
_WHOLE_DENY: list[tuple[re.Pattern, str]] = [
    (re.compile(r":\s*\(\s*\)\s*\{.*&.*;.*:"), "fork bomb :(){ :|:& };:"),
]


def is_dangerous_command(command: str) -> tuple[bool, str | None]:
    """Проверить команду на exact_deny (необратимые операции).

    Устойчива к обходам: chaining (&&;||;|), $(...)/backticks/eval, и reorder
    флагов (rm -fr /, rm -r -f /). Возвращает (is_dangerous, reason).
    """
    cmd = command.strip()
    # 0) whole-command сигнатуры (fork bomb и т.п.)
    for rx, reason in _WHOLE_DENY:
        if rx.search(cmd):
            return True, reason
    # 1) по каждой изолированной под-команде
    for sub in _split_subcommands(command):
        s = sub.strip()
        if not s:
            continue
        # rm -rf на защищённые пути (любой порядок флагов)
        tgt = _rm_recursive_force_targets(s)
        if tgt is not None:
            return True, f"rm -rf на защищённый путь: {tgt}"
        # остальные паттерны
        for rx, reason in _DENY_PER_SUBCMD:
            if rx.search(s):
                return True, reason
    return False, None


# Секретные пути/маркеры — в sandboxed чтение/отправка блокируется (god/full обходит).
# Узко: только реальные секреты, чтобы не ломать легальные cat/curl.
SECRET_PATH_MARKERS = (
    "~/.ssh", ".ssh/id_rsa", ".ssh/id_ed25519", ".ssh/id_ecdsa", ".ssh/id_dsa",
    "~/.gnupg", "/.gnupg/",
    "/etc/shadow", "/etc/gshadow", "/etc/ssh/",
    "~/.config/caesar",  # config.yaml (bot token + LLM keys) + secrets.yaml
    "secrets.yaml",
    "/.env", "~/.env",  # env-файлы с секретами
)


def references_secret_path(text: str) -> bool:
    """Содержит ли команда/путь ссылку на секретное расположение (~/.ssh, config
    с ключами, /etc/shadow, .env и т.п.). Только для sandboxed-режима.
    """
    for marker in SECRET_PATH_MARKERS:
        if marker in text:
            return True
    return False



@dataclass
class ToolResult:
    success: bool
    data: dict = field(default_factory=dict)
    error: str | None = None
    requires_permission: bool = False  # если True — нужно подтверждение пользователя


class Tool(ABC):
    """Базовый класс инструмента."""
    
    name: str = ""
    description: str = ""
    category: str = ""
    
    # JSON Schema для tool-call (OpenAI-совместимый)
    parameters_schema: dict = {}
    
    def __init__(self):
        self.log = get_logger(f"tool.{self.name}")
    
    @abstractmethod
    async def execute(self, **kwargs) -> ToolResult:
        """Выполнить инструмент."""
        ...
    
    def to_openai_schema(self) -> dict:
        """Вернуть схему для OpenAI tool-call API."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters_schema,
            },
        }
    
    def requires_permission(self, **kwargs) -> bool:
        """Нуждается ли этот вызов в подтверждении пользователя.
        
        По умолчанию — нет. Переопределяется в опасных инструментах.
        """
        return False


def get_tool_schemas(tools: list[Tool]) -> list[dict]:
    """Получить схемы всех инструментов для передачи в LLM."""
    return [t.to_openai_schema() for t in tools]
