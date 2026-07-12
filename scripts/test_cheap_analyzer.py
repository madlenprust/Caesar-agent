"""Тест: cheap LLM analyzer."""
import asyncio
import sys
sys.path.insert(0, "/home/z/my-project")

from unittest.mock import MagicMock, AsyncMock
from caesar.core.orchestrator import Orchestrator
from caesar.core.events import EventBus
from caesar.core.queue import Task, TaskComplexity
from caesar.core.llm import LLMResponse, LLMMessage
from caesar.config import Config


class MockCheapLLM:
    """Mock cheap LLM — возвращает предзаготовленные JSON ответы."""
    def __init__(self):
        self.api_key = "mock"
        self.cheap = self  # self-reference
        self.smart = MagicMock(api_key="mock")
        # Map запросов в ответы
        self.responses = {
            "привет": None,  # обрабатывается эвристикой без LLM
            "что такое агрессия": '{"is_trivial": false, "trivial_response": "", "needs_tools": false, "needs_memory": true, "complexity": "simple"}',
            "найди новости": '{"is_trivial": false, "trivial_response": "", "needs_tools": true, "needs_memory": false, "complexity": "medium"}',
            "проанализируй код": '{"is_trivial": false, "trivial_response": "", "needs_tools": true, "needs_memory": false, "complexity": "complex"}',
            "запомни цвет синий": '{"is_trivial": false, "trivial_response": "", "needs_tools": false, "needs_memory": false, "complexity": "simple"}',
        }
    
    async def cheap_chat(self, messages, temperature=0.3, max_tokens=1000):
        # Last user message
        user_msg = ""
        for m in reversed(messages):
            if m.role == "user":
                user_msg = m.content.lower()
                break
        
        # Find matching response
        for key, response in self.responses.items():
            if key in user_msg:
                if response is None:
                    return LLMResponse(content="{}", total_tokens=10, prompt_tokens=5, completion_tokens=5, model="mock")
                return LLMResponse(content=response, total_tokens=50, prompt_tokens=30, completion_tokens=20, model="mock")
        
        # Default — не тривиальный, БЕЗ tools (для "запомни шашлык" и подобных)
        return LLMResponse(
            content='{"is_trivial": false, "trivial_response": "", "needs_tools": false, "needs_memory": false, "complexity": "simple"}',
            total_tokens=50, prompt_tokens=30, completion_tokens=20, model="mock"
        )


async def main():
    print("=" * 60)
    print("CHEAP LLM ANALYZER TEST")
    print("=" * 60)
    
    config = Config()
    event_bus = EventBus()
    
    storage = MagicMock()
    storage.get_messages.return_value = []
    storage.get_facts.return_value = []
    
    tools = MagicMock()
    tools.get_schemas = lambda: []
    tools.set_context = lambda **kw: None
    
    llm = MockCheapLLM()
    llm.smart = MagicMock(api_key="mock")
    
    orch = Orchestrator(
        config=config, event_bus=event_bus, storage=storage,
        llm_router=llm, tool_registry=tools,
    )
    await orch.start()
    
    # Тест 1: "привет" — должен попасть в эвристику (без LLM)
    print("\n--- Test 1: 'привет' (эвристика, без LLM) ---")
    task = Task(
        id="t1", user_message="привет",
        channel_id="test:cli", source_chat_id="test:cli",
        user_id="u1", complexity=TaskComplexity.SIMPLE,
    )
    analysis = await orch._analyze_request(task, has_history=False)
    print(f"  is_trivial: {analysis.get('is_trivial')}")
    print(f"  trivial_response: {analysis.get('trivial_response')}")
    assert analysis.get("is_trivial") == True
    assert "Привет" in analysis.get("trivial_response", "")
    print("  ✓ Эвристика сработала, LLM не звался")
    
    # Тест 2: "что такое агрессия" — needs_memory=true
    print("\n--- Test 2: 'что такое агрессия' ---")
    task = Task(
        id="t2", user_message="что такое агрессия",
        channel_id="test:cli", source_chat_id="test:cli",
        user_id="u1", complexity=TaskComplexity.SIMPLE,
    )
    analysis = await orch._analyze_request(task, has_history=False)
    print(f"  is_trivial: {analysis.get('is_trivial')}")
    print(f"  needs_memory: {analysis.get('needs_memory')}")
    print(f"  complexity: {analysis.get('complexity')}")
    assert analysis.get("is_trivial") == False
    assert analysis.get("needs_memory") == True
    print("  ✓ Correctly identified as needing memory search")
    
    # Тест 3: "найди новости" — needs_tools=true, medium
    print("\n--- Test 3: 'найди новости' ---")
    task = Task(
        id="t3", user_message="найди новости про Hermes",
        channel_id="test:cli", source_chat_id="test:cli",
        user_id="u1", complexity=TaskComplexity.SIMPLE,
    )
    analysis = await orch._analyze_request(task, has_history=False)
    print(f"  needs_tools: {analysis.get('needs_tools')}")
    print(f"  complexity: {analysis.get('complexity')}")
    assert analysis.get("needs_tools") == True
    assert analysis.get("complexity") == "medium"
    print("  ✓ Correctly identified as needing tools, medium complexity")
    
    # Тест 4: "проанализируй код" — complex
    print("\n--- Test 4: 'проанализируй код' ---")
    task = Task(
        id="t4", user_message="проанализируй код Linux kernel",
        channel_id="test:cli", source_chat_id="test:cli",
        user_id="u1", complexity=TaskComplexity.SIMPLE,
    )
    analysis = await orch._analyze_request(task, has_history=False)
    print(f"  needs_tools: {analysis.get('needs_tools')}")
    print(f"  complexity: {analysis.get('complexity')}")
    assert analysis.get("needs_tools") == True
    assert analysis.get("complexity") == "complex"
    print("  ✓ Correctly identified as complex")
    
    # Тест 5: "спасибо" — эвристика
    print("\n--- Test 5: 'спасибо' (эвристика) ---")
    task = Task(
        id="t5", user_message="спасибо",
        channel_id="test:cli", source_chat_id="test:cli",
        user_id="u1", complexity=TaskComplexity.SIMPLE,
    )
    analysis = await orch._analyze_request(task, has_history=False)
    print(f"  is_trivial: {analysis.get('is_trivial')}")
    print(f"  trivial_response: {analysis.get('trivial_response')}")
    assert analysis.get("is_trivial") == True
    assert "Пожалуйста" in analysis.get("trivial_response", "")
    print("  ✓ Correctly identified as trivial")
    
    # Тест 6: "запомни цвет синий" — NOT trivial, NOT tools, NOT memory
    print("\n--- Test 6: 'запомни что я люблю шашлык' ---")
    task = Task(
        id="t6", user_message="запомни что я люблю шашлык",
        channel_id="test:cli", source_chat_id="test:cli",
        user_id="u1", complexity=TaskComplexity.SIMPLE,
    )
    analysis = await orch._analyze_request(task, has_history=False)
    print(f"  is_trivial: {analysis.get('is_trivial')}")
    print(f"  needs_tools: {analysis.get('needs_tools')}")
    print(f"  needs_memory: {analysis.get('needs_memory')}")
    assert analysis.get("is_trivial") == False
    assert analysis.get("needs_tools") == False
    print("  ✓ Correctly identified as non-trivial, no tools needed")
    
    print("\n=== ALL TESTS PASSED ===")


if __name__ == "__main__":
    asyncio.run(main())
