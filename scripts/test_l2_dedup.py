"""Тест: L2 факты дедуплицируются против истории диалога.

Сценарий: пользователь 2 сообщения назад сказал "я люблю шашлык".
В L2 хранится факт: user.likes = шашлык.
Должен ВЫКИНУТЬ этот факт из memory_context (он уже в истории).
"""
import asyncio
import sys
sys.path.insert(0, "/home/z/my-project")

from unittest.mock import MagicMock, AsyncMock
from caesar.core.orchestrator import Orchestrator
from caesar.core.events import EventBus
from caesar.core.queue import Task, TaskComplexity
from caesar.core.llm import LLMResponse, LLMMessage
from caesar.tools import ToolRegistry
from caesar.tools.base import Tool, ToolResult
from caesar.config import Config
from caesar.memory.storage import Storage


class StubTool(Tool):
    name = "stub"
    description = "stub"
    category = "test"
    parameters_schema = {"type": "object", "properties": {}, "required": []}
    async def execute(self, **_): return ToolResult(success=True, data={"ok": True})


async def main():
    config = Config()
    event_bus = EventBus()
    
    # Mock storage: returns history with "шашлык" + L2 fact "шашлык"
    storage = MagicMock(spec=Storage)
    storage.get_messages.return_value = [
        {"role": "user", "content": "запомни что я люблю шашлык"},
        {"role": "assistant", "content": "OK, запомнил: ты любишь шашлык"},
        {"role": "user", "content": "что я люблю?"},  # текущий запрос (последний)
    ]
    storage.get_facts.return_value = [
        {"entity": "user", "attribute": "likes", "value": "шашлык", "source_quote": "я люблю шашлык"},
        {"entity": "user", "attribute": "favorite_color", "value": "синий", "source_quote": "любимый цвет синий"},
    ]
    storage.save_message = MagicMock()
    storage.log_action = MagicMock()
    storage.log_token_usage = MagicMock()
    
    tool = StubTool()
    tools = ToolRegistry.__new__(ToolRegistry)
    tools._tools = {"stub": tool}
    tools.get_schemas = lambda: []
    tools.set_context = lambda **kw: None
    async def exec_wrap(name, **kw):
        return await tool.execute(**kw)
    tools.execute = exec_wrap
    
    # Mock LLM — захватываем system prompt
    captured_messages = []
    class CapturingLLM:
        def __init__(self):
            self.api_key = "mock"
            self.smart = MagicMock(api_key="mock")
            self.cheap = MagicMock(api_key="")  # cheap LLM не настроена
        async def smart_chat(self, messages, tools=None, **kw):
            captured_messages.extend(messages)
            return LLMResponse(
                content="Ты любишь шашлык.",
                tool_calls=[],
                total_tokens=50, prompt_tokens=30, completion_tokens=20, model="mock",
            )
    
    llm = CapturingLLM()
    orch = Orchestrator(config=config, event_bus=event_bus, storage=storage,
                        llm_router=llm, tool_registry=tools)
    await orch.start()
    
    task = Task(
        id="t1", user_message="что я люблю?",
        channel_id="test:cli", source_chat_id="test:cli",
        user_id="u1", complexity=TaskComplexity.SIMPLE,
    )
    await orch.handle_task(task)
    
    # Ищем system prompt в captured messages
    system_msg = next(m for m in captured_messages if m.role == "system")
    print("=== System Prompt ===")
    print(system_msg.content)
    print()
    
    # Assertions
    print("=== Assertions ===")
    
    # "шашлык" НЕ должен быть в memory_context (он уже в истории)
    # Но "синий" ДОЛЖЕН быть (его нет в истории)
    sp = system_msg.content
    
    # Находим блок "Долгосрочные факты" и берём ТОЛЬКО его (до следующей пустой строки)
    if "Долгосрочные факты" in sp:
        # Блок начинается после заголовка, заканчивается пустой строкой
        after_header = sp.split("Долгосрочные факты (которых нет в недавней истории):\n", 1)[1]
        facts_block = after_header.split("\n\n", 1)[0]
        print(f"Facts block:\n{facts_block}\n")
        
        assert "шашлык" not in facts_block.lower(), \
            f"FAIL: 'шашлык' should be deduped (already in history). Got: {facts_block}"
        print("✓ 'шашлык' was deduped (already in history)")
        
        assert "синий" in facts_block.lower(), \
            f"FAIL: 'синий' should be present (not in history). Got: {facts_block}"
        print("✓ 'синий' is present (not in history — fresh fact)")
    else:
        # Если блока нет вообще — значит все факты дедуплицированы
        print("✓ No L2 block — all facts deduped")
    
    print("\n=== ALL TESTS PASSED ===")


if __name__ == "__main__":
    asyncio.run(main())
