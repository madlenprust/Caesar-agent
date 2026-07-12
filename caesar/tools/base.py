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


# exact_deny — зашитый чёрный список необратимых операций (раздел 11.1)
EXACT_DENY_PATTERNS = [
    r"^rm\s+-rf?\s+/(?!tmp/|var/tmp/)",  # rm -rf /, но не /tmp/
    r"^rm\s+-rf?\s+~",
    r"^rm\s+-rf?\s+/etc\b",
    r"^rm\s+-rf?\s+/var\b",
    r"^rm\s+-rf?\s+/home\b",
    r"^rm\s+-rf?\s+/usr\b",
    r"^rm\s+-rf?\s+/boot\b",
    r"^mkfs\.\w+\s+/dev/",
    r"^dd\s+if=.*\s+of=/dev/",
    r"^:\(\)\s*\{\s*:\|:&\s*\};\s*:",
    r"^chmod\s+-R\s+777\s+/",
    r"^chmod\s+-R\s+000\s+/",
    r"^chown\s+-R\s+\S+\s+/",
    r"^systemctl\s+disable\s+(sshd|networking|systemd-network)",
    r"^>\s*/dev/(sda|nvme|hda|vd)",
    r"^pip\s+uninstall\s+-y\s+pip",
    r"^npm\s+uninstall\s+-g\s+npm",
    # Обход через bash -c / python -c / sh -c
    r"bash\s+-c\s+.*\brm\s+-rf\b",
    r"sh\s+-c\s+.*\brm\s+-rf\b",
    r"python3?\s+-c\s+.*\b(os\.system|subprocess|os\.remove|shutil\.rmtree)\b",
    r"bash\s+-c\s+.*\bmkfs\b",
    r"bash\s+-c\s+.*\bdd\s+if=",
    r"bash\s+-c\s+.*\bchmod\s+-R\b",
]

EXACT_DENY_REGEXES = [re.compile(p) for p in EXACT_DENY_PATTERNS]


def is_dangerous_command(command: str) -> tuple[bool, str | None]:
    """Проверить, является ли команда опасной (exact_deny).
    
    Возвращает (is_dangerous, reason).
    """
    cmd = command.strip()
    for i, pattern in enumerate(EXACT_DENY_REGEXES):
        if pattern.search(cmd):
            return True, EXACT_DENY_PATTERNS[i]
    return False, None


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
