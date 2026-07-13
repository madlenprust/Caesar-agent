"""Реестр всех инструментов.

См. roadmap раздел 11.9.

35 инструментов в V1, 7 категорий.
"""

from typing import Any

from caesar.tools.base import Tool, get_tool_schemas
from caesar.tools.shell_files import get_shell_files_tools
from caesar.tools.internet import get_internet_tools
from caesar.tools.sources import get_sources_tools
from caesar.tools.memory_tools import get_memory_tools
from caesar.tools.self_knowledge import get_self_knowledge_tools
from caesar.tools.documents import get_documents_tools
from caesar.tools.stt import get_audio_tools
from caesar.tools.cron_tools import get_cron_tools


class ToolRegistry:
    """Реестр инструментов.
    
    Хранит все инструменты, позволяет получить по имени,
    вернуть схемы для LLM.
    """
    
    def __init__(self, storage, l3_memory, l4_skills, access_mode: str = "sandboxed"):
        self._tools: dict[str, Tool] = {}
        self.access_mode = access_mode
        from caesar.logging_setup import get_logger
        self.log = get_logger("tools")
        self._register_all(storage, l3_memory, l4_skills, access_mode)
    
    def _register_all(self, storage, l3_memory, l4_skills, access_mode: str) -> None:
        # Категория 1: Shell + Files
        for tool in get_shell_files_tools():
            # Передаём access_mode в ShellExecTool чтобы он знал
            # что в full mode можно ВСЁ (даже rm -rf /)
            if hasattr(tool, "access_mode"):
                tool.access_mode = access_mode
            self._tools[tool.name] = tool
        
        # Категория 2: Интернет
        for tool in get_internet_tools():
            self._tools[tool.name] = tool
        
        # Категория 3: Источники
        for tool in get_sources_tools():
            self._tools[tool.name] = tool
        
        # Категория 4: Коммуникации (TODO: tg_post, email_send, webhook_call — V0.4+)
        # Пропускаем в V0.3, добавим в V0.4 с Telegram
        
        # Категория 5: Документы
        for tool in get_documents_tools():
            self._tools[tool.name] = tool
        
        # Категория 6: Память
        for tool in get_memory_tools(storage, l3_memory, l4_skills):
            self._tools[tool.name] = tool
        
        # Категория 7: Self-knowledge
        for tool in get_self_knowledge_tools(access_mode):
            self._tools[tool.name] = tool
        
        # Категория 8: Аудио (STT — транскрипция голосовых)
        for tool in get_audio_tools():
            self._tools[tool.name] = tool
        
        # Категория 9: Cron (планировщик задач)
        for tool in get_cron_tools():
            self._tools[tool.name] = tool
    
    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)
    
    def list_names(self) -> list[str]:
        return list(self._tools.keys())
    
    def get_schemas(self) -> list[dict]:
        """Схемы всех инструментов для передачи в LLM."""
        return [t.to_openai_schema() for t in self._tools.values()]
    
    def get_schemas_for(self, names: list[str]) -> list[dict]:
        """Схемы конкретных инструментов."""
        result = []
        for name in names:
            t = self._tools.get(name)
            if t:
                result.append(t.to_openai_schema())
        return result
    
    # Группы инструментов — для adaptive schemas
    # Ключевые инструменты — всегда слать
    CORE_TOOLS = {
        "shell_exec", "memory_search", "memory_add_fact", "skill_find",
    }
    # Инструменты поиска — для news/research задач
    SEARCH_TOOLS = {
        "web_search", "web_fetch", "http_request",
        "github_releases", "github_search",
        "hn_search", "reddit_search", "wikipedia_read",
        "rss_read", "tg_read_channel",
    }
    # Инструменты файлов — для file/code задач
    FILE_TOOLS = {
        "read_file", "write_file", "edit_file", "find_files", "grep",
        "parse_pdf", "parse_docx", "parse_xlsx", "parse_csv",
    }
    # Self-knowledge — для caesar-задач
    SELF_TOOLS = {
        "self_read", "self_edit", "self_scan", "self_test",
        "self_install_package",
    }
    # Cron — для планировщика
    CRON_TOOLS = {
        "cron_add", "cron_list", "cron_remove",
    }
    
    def get_schemas_smart(self, user_message: str) -> list[dict]:
        """Отобрать только релевантные инструменты по тексту запроса.
        
        Экономия: вместо 34 инструментов (~4000 токенов) — 8-12 (~1200 токенов).
        """
        msg = user_message.lower()
        
        # Всегда включаем core
        relevant = set(self.CORE_TOOLS)
        
        # Поиск — если запрос про новости, поиск, информацию
        if any(w in msg for w in [
            "найди", "новости", "что нового", "поиск", "информация",
            "нового", "обновлени", "релиз", "что так", "расскажи",
            "статья", "сайт", "обзор", "исследуй", "собери",
            "find", "search", "news", "what is", "latest",
        ]):
            relevant |= self.SEARCH_TOOLS
        
        # Файлы — если запрос про файлы, код, документы
        if any(w in msg for w in [
            "файл", "код", "папк", "директори", "pdf", "docx", "xlsx",
            "csv", "прочитай", "запиши", "отредактируй", "найди файл",
            "file", "code", "directory", "read", "write", "edit",
        ]):
            relevant |= self.FILE_TOOLS
        
        # Self-knowledge — если запрос про caesar
        if any(w in msg for w in [
            "caesar", "агент", "настрой", "обнови", "код агента",
            "agent", "config", "setup",
        ]):
            relevant |= self.SELF_TOOLS
        
        # Cron — если запрос про расписание
        if any(w in msg for w in [
            "cron", "расписание", "каждый день", "по будням",
            "напомни", "запланируй", "schedule",
        ]):
            relevant |= self.CRON_TOOLS
        
        # memory_delete — если просит удалить
        if any(w in msg for w in ["удали", "забудь", "стереть", "delete", "remove"]):
            relevant.add("memory_delete")
        
        # Если не нашли ничего кроме core — добавляем всё
        # (лучше потратить токены чем не дать нужный инструмент)
        if len(relevant) <= len(self.CORE_TOOLS):
            return self.get_schemas()
        
        return self.get_schemas_for(list(relevant))
    
    def set_context(
        self,
        channel_id: str = "",
        user_id: str = "",
        author_id: str = "",
        stt_model: str | None = None,
        stt_language: str | None = None,
    ) -> None:
        """Установить контекст для инструментов."""
        for tool in self._tools.values():
            if hasattr(tool, "default_channel"):
                tool.default_channel = channel_id
                tool.default_user = user_id
                tool.default_author = author_id
            # STT: передаём модель/язык из конфига
            if hasattr(tool, "default_model") and stt_model:
                tool.default_model = stt_model
            if hasattr(tool, "default_language"):
                tool.default_language = stt_language
            # Cron: передаём user_id и channel_id
            if hasattr(tool, "default_user_id"):
                tool.default_user_id = user_id
            if hasattr(tool, "default_channel_id") and hasattr(tool, "cron_scheduler"):
                if not hasattr(tool, "default_channel"):  # cron tools не имеют default_channel
                    tool.default_channel_id = channel_id
    
    async def execute(self, name: str, **kwargs) -> Any:
        """Выполнить инструмент по имени.
        
        Всегда возвращает ToolResult (даже если инструмент не найден).
        
        БЕЗОПАСНОСТЬ:
        - access_mode='full' — НЕ проверяет requires_permission.
          Пользователь сам решил что бот может выполнять любые команды.
          Это его сервер, его правила.
        - access_mode='sandboxed' (по умолчанию) — проверяет requires_permission.
          LLM не может выполнить опасные команды без подтверждения.
        - is_dangerous_command (rm -rf /, mkfs, ...) блокируется в sandboxed.
          В full/god_mode отключается — владелец (бот привязан → god только у
          owner) берёт ответственность за любые команды.
        """
        tool = self._tools.get(name)
        if not tool:
            from caesar.tools.base import ToolResult
            return ToolResult(success=False, error=f"Tool '{name}' not found")
        
        # Проверка разрешений — только в sandboxed и не в god_mode.
        # full/god_mode — владелец разрешил любые команды (бот привязан).
        if self.access_mode != "full" and not getattr(tool, "god_mode", False):
            try:
                if hasattr(tool, "requires_permission"):
                    needs_perm = tool.requires_permission(**kwargs)
                    if needs_perm:
                        from caesar.tools.base import ToolResult
                        # Извлекаем команду/путь для информативного сообщения
                        cmd_preview = ""
                        for key in ("command", "path", "file_path"):
                            if key in kwargs:
                                cmd_preview = str(kwargs[key])[:100]
                                break
                        
                        self.log.warning(
                            f"BLOCKED dangerous tool call: {name}({cmd_preview}) — "
                            f"requires permission (access_mode={self.access_mode})"
                        )
                        return ToolResult(
                            success=False,
                            error=(
                                f"BLOCKED: команда требует подтверждения пользователя. "
                                f"access_mode={self.access_mode}. "
                                f"Чтобы разрешить опасные команды — поставь "
                                f"mode: full в config.yaml и перезапусти daemon. "
                                f"Команда: {cmd_preview}"
                            ),
                        )
            except Exception as e:
                from caesar.tools.base import ToolResult
                return ToolResult(
                    success=False,
                    error=f"Permission check failed: {e}",
                )
        
        result = await tool.execute(**kwargs)
        return result
