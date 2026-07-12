"""Тест: иконки источников памяти (💬 L1, 📌 L2) эмитятся до LLM работы.

Сценарий 1: есть история → 💬
Сценарий 2: есть L2 факты (fresh, не в истории) → 📌
Сценарий 3: есть и история и L2 → 💬 затем 📌
Сценарий 4: ничего нет → ни 💬 ни 📌
"""
import asyncio
import sys
sys.path.insert(0, "/home/z/my-project")

from unittest.mock import MagicMock
from caesar.core.orchestrator import Orchestrator
from caesar.core.events import EventBus
from caesar.core.queue import Task, TaskComplexity
from caesar.core.llm import LLMResponse
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


class CapturingLLM:
    def __init__(self):
        self.api_key = "mock"
        self.smart = MagicMock(api_key="mock")
        self.cheap = MagicMock(api_key="")  # cheap LLM не настроена
    async def smart_chat(self, messages, tools=None, **kw):
        return LLMResponse(
            content="ответ", tool_calls=[],
            total_tokens=10, prompt_tokens=5, completion_tokens=5, model="mock",
        )


async def run_scenario(name, history_msgs, l2_facts):
    config = Config()
    event_bus = EventBus()
    
    storage = MagicMock(spec=Storage)
    # history: последнее сообщение = текущий запрос (исключается), остальные = история
    if history_msgs:
        all_msgs = history_msgs + [{"role": "user", "content": "current question"}]
    else:
        all_msgs = [{"role": "user", "content": "current question"}]
    storage.get_messages.return_value = all_msgs
    storage.get_facts.return_value = l2_facts
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
    
    # Захватываем emitted events
    emitted_icons = []
    async def capture_emit(key, event):
        if event.type == "progress_update":
            emitted_icons.append(event.data.get("icon", ""))
    event_bus.emit = capture_emit
    
    llm = CapturingLLM()
    orch = Orchestrator(config=config, event_bus=event_bus, storage=storage,
                        llm_router=llm, tool_registry=tools)
    await orch.start()
    
    task = Task(
        id=name, user_message="current question",
        channel_id="test:cli", source_chat_id="test:cli",
        user_id="u1", complexity=TaskComplexity.SIMPLE,
    )
    await orch.handle_task(task)
    
    print(f"\n=== {name} ===")
    print(f"  history_msgs: {len(history_msgs)}, l2_facts: {len(l2_facts)}")
    print(f"  emitted: {' '.join(emitted_icons)}")
    return emitted_icons


async def main():
    hist = [
        {"role": "user", "content": "привет"},
        {"role": "assistant", "content": "привет!"},
    ]
    facts_in_hist = [
        {"entity": "user", "attribute": "likes", "value": "привет", "source_quote": "..."},
    ]
    facts_fresh = [
        {"entity": "user", "attribute": "favorite_color", "value": "синий", "source_quote": "..."},
    ]
    
    # Сценарий 1: только история
    icons1 = await run_scenario("S1: history only", hist, [])
    assert "💬" in icons1, f"S1: should have 💬, got {icons1}"
    assert "📌" not in icons1, f"S1: should NOT have 📌, got {icons1}"
    print("  ✓ 💬 emitted, no 📌")
    
    # Сценарий 2: только L2 (fresh facts)
    icons2 = await run_scenario("S2: L2 fresh only", [], facts_fresh)
    assert "📌" in icons2, f"S2: should have 📌, got {icons2}"
    assert "💬" not in icons2, f"S2: should NOT have 💬, got {icons2}"
    print("  ✓ 📌 emitted, no 💬")
    
    # Сценарий 3: и история и L2 fresh
    icons3 = await run_scenario("S3: history + L2 fresh", hist, facts_fresh)
    assert "💬" in icons3, f"S3: should have 💬, got {icons3}"
    assert "📌" in icons3, f"S3: should have 📌, got {icons3}"
    # 💬 должен быть раньше 📌
    assert icons3.index("💬") < icons3.index("📌"), \
        f"S3: 💬 should come before 📌, got {icons3}"
    print("  ✓ 💬 then 📌 (correct order)")
    
    # Сценарий 4: L2 fact уже в истории → 📌 НЕ должен эмититься (дедуплицирован)
    icons4 = await run_scenario("S4: L2 dup in history", hist, facts_in_hist)
    assert "💬" in icons4, f"S4: should have 💬 (history), got {icons4}"
    assert "📌" not in icons4, f"S4: should NOT have 📌 (dup), got {icons4}"
    print("  ✓ 💬 emitted, 📌 deduped (already in history)")
    
    # Сценарий 5: ничего нет
    icons5 = await run_scenario("S5: nothing", [], [])
    assert "💬" not in icons5, f"S5: should NOT have 💬, got {icons5}"
    assert "📌" not in icons5, f"S5: should NOT have 📌, got {icons5}"
    print("  ✓ no 💬, no 📌 (fresh start)")
    
    print("\n=== ALL TESTS PASSED ===")


if __name__ == "__main__":
    asyncio.run(main())
