"""Интеграционный тест: loop-detector + partial delivery.

Сценарий A: LLM "послушный" — после warning injection переключается на финальный ответ.
Сценарий B: LLM "упрямый" — всегда вызывает web_search. Должен быть force_finish с aggregated partial.
"""
import asyncio
import json
import sys
sys.path.insert(0, "/home/z/my-project")

from unittest.mock import MagicMock

from caesar.core.orchestrator import Orchestrator
from caesar.core.events import EventBus
from caesar.core.queue import Task, TaskStatus, TaskComplexity
from caesar.core.llm import LLMResponse, ToolCall
from caesar.tools import ToolRegistry
from caesar.tools.base import Tool, ToolResult
from caesar.config import Config
from caesar.memory.storage import Storage


class MockWebSearchTool(Tool):
    """Mock web_search что всегда возвращает 0 результатов (симулируем empty DDG)."""
    name = "web_search"
    description = "mock"
    category = "internet"
    parameters_schema = {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}

    def __init__(self):
        self.call_count = 0

    async def execute(self, query: str, **_) -> ToolResult:
        self.call_count += 1
        return ToolResult(
            success=True,
            data={
                "results": [
                    {"title": f"Result {self.call_count} for {query}", "url": f"https://example.com/{self.call_count}", "snippet": "mocked"},
                ],
                "total_found": 1,
                "engine": "mock",
            },
        )


class StubbornLLM:
    """LLM который ВСЕГДА вызывает web_search с теми же args (никогда не финализирует)."""
    def __init__(self):
        self.api_key = "mock"
        self.smart = MagicMock(api_key="mock")
        self.cheap = MagicMock(api_key="")  # cheap LLM не настроена → analyzer fallback
        self.call_count = 0

    async def smart_chat(self, messages, tools=None, **kwargs):
        self.call_count += 1
        tc = ToolCall(
            id=f"call_{self.call_count}",
            name="web_search",
            arguments={"query": "Hermes agent news"},
        )
        return LLMResponse(
            content=f"Ищу информацию (попытка {self.call_count})",
            tool_calls=[tc],
            total_tokens=100,
            prompt_tokens=50,
            completion_tokens=50,
            model="mock",
        )


class SmartLLM:
    """LLM который после warning injection (когда видит skipped=true в tool result) переключается."""
    def __init__(self):
        self.api_key = "mock"
        self.smart = MagicMock(api_key="mock")
        self.cheap = MagicMock(api_key="")  # cheap LLM не настроена
        self.call_count = 0

    async def smart_chat(self, messages, tools=None, **kwargs):
        self.call_count += 1
        # Проверяем последние messages — если есть skipped=true, даём финальный ответ
        for m in reversed(messages[-3:]):
            if m.role == "tool" and m.content and "skipped" in m.content:
                return LLMResponse(
                    content="Не нашёл свежих новостей про Hermes agent. Поисковые движки пустые.",
                    tool_calls=[],
                    total_tokens=80,
                    prompt_tokens=40,
                    completion_tokens=40,
                    model="mock",
                )
        
        tc = ToolCall(
            id=f"call_{self.call_count}",
            name="web_search",
            arguments={"query": "Hermes agent news"},
        )
        return LLMResponse(
            content=f"Ищу информацию (попытка {self.call_count})",
            tool_calls=[tc],
            total_tokens=100,
            prompt_tokens=50,
            completion_tokens=50,
            model="mock",
        )


def make_orchestrator(mock_llm, mock_tool):
    config = Config()
    config.orchestrator.action_dedup_threshold = 3
    config.orchestrator.max_steps_simple = 10
    
    event_bus = EventBus()
    
    storage = MagicMock(spec=Storage)
    storage.get_messages.return_value = []
    storage.get_facts.return_value = []
    
    tools = ToolRegistry.__new__(ToolRegistry)
    tools._tools = {"web_search": mock_tool}
    tools.get_schemas = lambda: []
    tools.set_context = lambda **kwargs: None
    
    async def execute_wrapper(name, **kwargs):
        return await mock_tool.execute(**kwargs)
    tools.execute = execute_wrapper
    
    orch = Orchestrator(
        config=config,
        event_bus=event_bus,
        storage=storage,
        llm_router=mock_llm,
        tool_registry=tools,
    )
    return orch


async def test_stubborn():
    print("\n=== Test A: Stubborn LLM (always loops) ===")
    mock_tool = MockWebSearchTool()
    llm = StubbornLLM()
    orch = make_orchestrator(llm, mock_tool)
    await orch.start()
    
    task = Task(
        id="stubborn-1",
        user_message="Найди что нового про Hermes agent",
        channel_id="test:cli",
        source_chat_id="test:cli",
        user_id="u1",
        complexity=TaskComplexity.SIMPLE,
    )
    await orch.handle_task(task)
    
    print(f"Task status: {task.status}")
    print(f"LLM calls: {llm.call_count}")
    print(f"WebSearch executions: {mock_tool.call_count}")
    print(f"Result:\n{task.result}")
    print()
    
    # web_search должен быть вызван ОДИН раз (потом все дубликаты skip)
    assert mock_tool.call_count == 1, f"Expected 1 execution, got {mock_tool.call_count}"
    print(f"✓ web_search executed only once (got {mock_tool.call_count})")
    
    # Task должен быть force-finished
    assert task.status == TaskStatus.COMPLETED, f"Expected COMPLETED, got {task.status}"
    print(f"✓ Task completed (force-finished via loop detector)")
    
    # Result должен содержать aggregated partial info
    assert "⚠️" in task.result, "Result should have warning"
    assert "Обнаружен цикл" in task.result, "Result should mention loop detected"
    print(f"✓ Result has loop warning")
    
    # Result должен содержать aggregated findings (mock returned 1 result)
    assert "Result 1 for Hermes agent news" in task.result, \
        f"Result should have aggregated tool findings, got: {task.result}"
    print(f"✓ Result aggregates tool findings (not just last assistant text)")
    
    # Result НЕ должен содержать бесполезного "Попробую другие источники"
    assert "Попробую другие источники" not in task.result, \
        "Result should NOT have useless 'try other sources' text"
    print(f"✓ Result does NOT have useless 'try other sources' text")


async def test_smart():
    print("\n=== Test B: Smart LLM (recovers after warning) ===")
    mock_tool = MockWebSearchTool()
    llm = SmartLLM()
    orch = make_orchestrator(llm, mock_tool)
    await orch.start()
    
    task = Task(
        id="smart-1",
        user_message="Найди что нового про Hermes agent",
        channel_id="test:cli",
        source_chat_id="test:cli",
        user_id="u1",
        complexity=TaskComplexity.SIMPLE,
    )
    await orch.handle_task(task)
    
    print(f"Task status: {task.status}")
    print(f"LLM calls: {llm.call_count}")
    print(f"WebSearch executions: {mock_tool.call_count}")
    print(f"Result:\n{task.result}")
    print()
    
    # LLM должен был вызвать web_search 1 раз, потом понять warning и финализировать
    assert mock_tool.call_count == 1, f"Expected 1 execution, got {mock_tool.call_count}"
    print(f"✓ web_search executed only once")
    
    assert llm.call_count <= 3, f"LLM should recover within 3 calls, got {llm.call_count}"
    print(f"✓ LLM recovered after warning injection ({llm.call_count} LLM calls)")
    
    assert task.status == TaskStatus.COMPLETED
    print(f"✓ Task completed normally (no force_finish)")
    
    assert "⚠️" not in task.result, "Smart path should not have warning prefix"
    print(f"✓ Result is clean (no warning prefix)")


async def main():
    await test_stubborn()
    await test_smart()
    print("\n=== ALL TESTS PASSED ===")


if __name__ == "__main__":
    asyncio.run(main())
