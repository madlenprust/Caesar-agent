"""Инструменты категории 6: Память (внутренние).

См. roadmap раздел 11.7.
"""

import asyncio
from typing import Any

from caesar.tools.base import Tool, ToolResult


class MemorySearchTool(Tool):
    """Поиск в памяти (L2 + L3)."""
    
    name = "memory_search"
    description = (
        "Поиск в памяти агента. Сначала ищет в L2 (факты канала), потом в L3 (векторная). "
        "Используй когда нужно вспомнить что обсуждали ранее."
    )
    category = "memory"
    parameters_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Что искать"},
            "level": {"type": "string", "enum": ["auto", "L2", "L3", "both"], "default": "auto"},
            "top_k": {"type": "integer", "default": 3},
        },
        "required": ["query"],
    }
    
    def __init__(self, storage, l3_memory, channel_id: str = "", user_id: str = ""):
        super().__init__()
        self.storage = storage
        self.l3 = l3_memory
        self.default_channel = channel_id
        self.default_user = user_id
    
    async def execute(
        self,
        query: str,
        level: str = "auto",
        top_k: int = 3,
        channel: str | None = None,
        user_id: str | None = None,
        **_,
    ) -> ToolResult:
        ch = channel or self.default_channel
        uid = user_id or self.default_user
        
        if not ch or not uid:
            return ToolResult(
                success=False,
                error="No channel_id or user_id provided",
            )
        
        # Извлекаем имя канала (последняя часть после "channel:user:...")
        # channel_id формат: "channel:user-ivan:main" → имя "main"
        channel_name = ch.rsplit(":", 1)[-1] if ":" in ch else ch
        
        results = []
        
        # L2: факты канала
        if level in ("auto", "L2", "both"):
            facts = self.storage.get_facts(uid, channel_name, limit=top_k * 3)
            # Простое matching: ищем по словам в entity/attribute/value
            query_lower = query.lower()
            for f in facts:
                text = f"{f['entity']} {f['attribute']} {f['value']}".lower()
                if any(w in text for w in query_lower.split() if len(w) > 2):
                    results.append({
                        "level": "L2",
                        "content": f"{f['entity']}.{f['attribute']} = {f['value']}",
                        "entity": f["entity"],
                        "attribute": f["attribute"],
                        "value": f["value"],
                        "valid_from": f["valid_from"],
                        "relevance_score": 1.0,  # пока без скоринга
                    })
                    if len(results) >= top_k:
                        break
        
        # L3: векторный поиск
        if level in ("auto", "L3", "both") and len(results) < top_k:
            try:
                l3_results = await self.l3.search(
                    query=query,
                    user_id=uid,
                    channel=channel_name,
                    final_k=top_k - len(results),
                )
                for r in l3_results:
                    results.append({
                        "level": "L3",
                        "content": r.content,
                        "channel": r.channel,
                        "relevance_score": r.score,
                    })
            except Exception as e:
                self.log.warning(f"L3 search failed: {e}")
        
        return ToolResult(
            success=True,
            data={"results": results, "total_found": len(results)},
        )


class MemoryAddFactTool(Tool):
    """Сохранить факт в L2."""
    
    name = "memory_add_fact"
    description = (
        "Сохранить факт в долгосрочную память L2. "
        "Только concrete facts с confidence medium или high. "
        "Если факт с тем же entity+attribute существует — старый инвалидируется."
    )
    category = "memory"
    parameters_schema = {
        "type": "object",
        "properties": {
            "entity": {"type": "string", "description": "О чём факт (например: telegram, llm_provider)"},
            "attribute": {"type": "string", "description": "Что именно (например: token_file, model)"},
            "value": {"type": "string", "description": "Значение"},
            "confidence": {"type": "string", "enum": ["high", "medium"], "default": "medium"},
            "category": {
                "type": "string",
                "enum": ["fact", "decision", "win", "incident", "preference"],
                "default": "fact",
                "description": "Категория: decision (решение), win (успех), "
                "incident (сбой), preference (указание юзера), fact (прочее).",
            },
            "source_quote": {"type": "string", "description": "Цитата-источник (обязательно)"},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["entity", "attribute", "value", "source_quote"],
    }

    def __init__(self, storage, channel_id: str = "", user_id: str = "", author_id: str = ""):
        super().__init__()
        self.storage = storage
        self.default_channel = channel_id
        self.default_user = user_id
        self.default_author = author_id

    async def execute(
        self,
        entity: str,
        attribute: str,
        value: str,
        confidence: str = "medium",
        source_quote: str = "",
        category: str = "fact",
        tags: list[str] | None = None,
        channel: str | None = None,
        user_id: str | None = None,
        author_id: str | None = None,
        **_,
    ) -> ToolResult:
        ch = channel or self.default_channel
        uid = user_id or self.default_user
        au = author_id or self.default_author or uid

        if not ch or not uid:
            return ToolResult(success=False, error="No channel_id or user_id")

        channel_name = ch.rsplit(":", 1)[-1] if ":" in ch else ch

        result = self.storage.add_fact(
            user_id=uid,
            channel=channel_name,
            entity=entity,
            attribute=attribute,
            value=value,
            confidence=confidence,
            category=category,
            author_id=au,
            tags=tags,
        )
        
        return ToolResult(success=True, data=result)


class SkillFindTool(Tool):
    """Найти скилл по описанию."""
    
    name = "skill_find"
    description = "Найти подходящий скилл по описанию задачи. Возвращает с confidence."
    category = "memory"
    parameters_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Описание что нужно сделать"},
            "confidence_threshold": {"type": "string", "enum": ["high", "medium"], "default": "medium"},
        },
        "required": ["query"],
    }
    
    def __init__(self, l4_skills):
        super().__init__()
        self.l4 = l4_skills
    
    async def execute(self, query: str, confidence_threshold: str = "medium", **_) -> ToolResult:
        skills = await self.l4.find_skill(query, confidence_threshold)
        return ToolResult(
            success=True,
            data={"skills": skills, "total_found": len(skills)},
        )


class SkillSaveTool(Tool):
    """Создать/обновить скилл."""
    
    name = "skill_save"
    description = (
        "Создать новый скилл или обновить существующий. "
        "exact_recipe — массив шагов: [{id, command, expected_output, notes}]. "
        "anti_patterns — массив ошибок: [{error, happened}]."
    )
    category = "memory"
    parameters_schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "trigger": {"type": "string", "description": "На какие фразы реагирует"},
            "prerequisites": {"type": "array", "items": {"type": "string"}},
            "exact_recipe": {"type": "array"},
            "anti_patterns": {"type": "array"},
            "pitfalls": {"type": "array", "items": {"type": "string"}},
            "example": {"type": "string"},
            "notes": {"type": "string"},
        },
        "required": ["name", "trigger"],
    }
    
    def __init__(self, l4_skills):
        super().__init__()
        self.l4 = l4_skills
    
    async def execute(self, name: str, trigger: str, **kwargs) -> ToolResult:
        from caesar.memory.l4 import Skill
        existing = self.l4.get_skill(name)
        if existing:
            existing.trigger = trigger
            for k, v in kwargs.items():
                if hasattr(existing, k):
                    setattr(existing, k, v)
            existing.version += 1
            skill = existing
            status = "updated"
        else:
            skill = Skill(name=name, trigger=trigger, **kwargs)
            status = "created"
        
        self.l4.save_skill(skill)
        return ToolResult(
            success=True,
            data={"skill_id": name, "version": skill.version, "status": status},
        )


class MemoryDeleteTool(Tool):
    """Удалить информацию из L3 векторной памяти.
    
    Используется когда пользователь говорит 'удали информацию про X'
    или 'забудь что я говорил про Y'.
    """
    
    name = "memory_delete"
    description = (
        "Удалить информацию из долгосрочной векторной памяти (L3). "
        "Используй когда пользователь просит 'удали', 'забудь', 'стереть' "
        "информацию про что-то. Находит чанки по семантической схожести "
        "и удаляет их. Также можно удалить по тегу (если документ был "
        "проиндексирован с тегом)."
    )
    category = "memory"
    parameters_schema = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Что удалить (например 'информация про шашлык' или 'новости Hermes')",
            },
            "tag": {
                "type": "string",
                "description": "Альтернативно: удалить по тегу/имени файла (например 'важное' или 'books.txt')",
                "default": None,
            },
        },
        "required": [],
    }
    
    def __init__(self, storage, l3_memory, channel_id: str = "", user_id: str = ""):
        super().__init__()
        self.storage = storage
        self.l3 = l3_memory
        self.default_channel = channel_id
        self.default_user = user_id
    
    async def execute(
        self,
        query: str | None = None,
        tag: str | None = None,
        user_id: str | None = None,
        **_,
    ) -> ToolResult:
        uid = user_id or self.default_user
        if not uid:
            return ToolResult(success=False, error="No user_id provided")
        
        if not self.l3:
            return ToolResult(success=False, error="L3 memory not available")
        
        if not query and not tag:
            return ToolResult(success=False, error="Either query or tag required")
        
        # Удаление по тегу
        if tag and not query:
            result = await self.l3.delete_by_tag(user_id=uid, tag=tag)
            if result.get("deleted", 0) > 0:
                return ToolResult(
                    success=True,
                    data={
                        "deleted": result["deleted"],
                        "method": "by_tag",
                        "tag": tag,
                        "deleted_chunks": result.get("deleted_chunks", []),
                        "message": f"Удалил {result['deleted']} чанков с тегом '{tag}'",
                    },
                )
            else:
                return ToolResult(
                    success=True,
                    data={
                        "deleted": 0,
                        "method": "by_tag",
                        "tag": tag,
                        "message": f"Не нашёл чанков с тегом '{tag}'",
                    },
                )
        
        # Удаление по семантическому запросу
        result = await self.l3.delete_by_query(query=query, user_id=uid)
        deleted = result.get("deleted", 0)
        
        if deleted > 0:
            # Формируем сообщение что именно удалили
            previews = []
            for chunk in result.get("deleted_chunks", [])[:5]:  # первые 5 для превью
                preview = chunk.get("content", "")[:100]
                score = chunk.get("score", 0)
                previews.append(f"- (score: {score:.2f}) {preview}...")
            
            return ToolResult(
                success=True,
                data={
                    "deleted": deleted,
                    "method": "by_query",
                    "query": query,
                    "deleted_chunks": result.get("deleted_chunks", []),
                    "previews": previews,
                    "message": f"Удалил {deleted} чанков по запросу '{query}'",
                },
            )
        else:
            reason = result.get("reason", "nothing matched")
            return ToolResult(
                success=True,
                data={
                    "deleted": 0,
                    "method": "by_query",
                    "query": query,
                    "reason": reason,
                    "message": f"Не нашёл что удалить по запросу '{query}' ({reason})",
                },
            )


def get_memory_tools(storage, l3_memory, l4_skills) -> list[Tool]:
    return [
        MemorySearchTool(storage, l3_memory),
        MemoryAddFactTool(storage),
        MemoryDeleteTool(storage, l3_memory),
        SkillFindTool(l4_skills),
        SkillSaveTool(l4_skills),
    ]
