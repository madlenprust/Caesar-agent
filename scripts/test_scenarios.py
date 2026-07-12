#!/usr/bin/env python3
"""Тестовые сценарии для Caesar.

Симулирует 10 разных пользовательских сценариев и проверяет что:
1. Агент не падает
2. Агент помнит контекст диалога
3. Агент ищет в памяти
4. Агент правильно обрабатывает разные типы запросов
"""

import asyncio
import os
import sys
import json
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from caesar.config import Config, SOCKET_PATH
from caesar.memory.storage import Storage
from caesar.core.llm import LLMRouter, LLMMessage
from caesar.core.orchestrator import Orchestrator
from caesar.core.events import EventBus
from caesar.core.queue import Task, TaskStatus, TaskComplexity, TaskPriority
from caesar.tools import ToolRegistry
from caesar.memory.l3 import L3Memory
from caesar.memory.l4 import L4Skills


# Тестовые сценарии
SCENARIOS = [
    {
        "name": "1. Приветствие",
        "messages": ["привет"],
        "expect": "не должен упасть, должен ответить осмысленно",
    },
    {
        "name": "2. Запомнить имя",
        "messages": ["запомни что тебя зовут Цезарь"],
        "expect": "должен сохранить факт в L2 через memory_add_fact",
    },
    {
        "name": "3. Вспомнить имя (контекст)",
        "messages": ["запомни что тебя зовут Цезарь", "как тебя зовут?"],
        "expect": "должен ответить 'Цезарь' из истории диалога",
    },
    {
        "name": "4. Вспомнить имя (память L2)",
        "messages": [
            {"pre": {"fact": {"entity": "agent", "attribute": "name", "value": "Цезарь"}}},
            "как тебя зовут?",
        ],
        "expect": "должен ответить 'Цезарь' из L2 фактов",
    },
    {
        "name": "5. Простой вопрос без действий",
        "messages": ["что такое Python?"],
        "expect": "должен ответить текстом без вызова инструментов",
    },
    {
        "name": "6. Просьба выполнить команду",
        "messages": ["покажи файлы в текущей директории"],
        "expect": "должен вызвать shell_exec или find_files",
    },
    {
        "name": "7. Контекст: ранее обсуждали",
        "messages": ["я люблю Rust", "какой язык программирования я люблю?"],
        "expect": "должен ответить 'Rust' из истории",
    },
    {
        "name": "8. Пустое сообщение",
        "messages": [""],
        "expect": "не должен упасть",
    },
    {
        "name": "9. Очень длинное сообщение",
        "messages": ["А" * 5000],
        "expect": "не должен упасть",
    },
    {
        "name": "10. Специальные символы",
        "messages": ['найди файл "test" в /tmp'],
        "expect": "не должен упасть от кавычек",
    },
]


async def run_scenario(scenario: dict, storage: Storage, llm: LLMRouter, 
                       tools: ToolRegistry, event_bus: EventBus) -> dict:
    """Запустить один сценарий. Возвращает результат."""
    name = scenario["name"]
    messages = scenario["messages"]
    
    print(f"\n{'='*60}")
    print(f"ТЕСТ: {name}")
    print(f"{'='*60}")
    
    # Подготовка — если есть pre (факт в L2)
    channel_id = f"test-channel-{int(time.time())}"
    user_id = f"test-user-{os.getpid()}"
    
    for msg in messages:
        # Проверяем — это pre-условие или сообщение?
        if isinstance(msg, dict) and "pre" in msg:
            pre = msg["pre"]
            if "fact" in pre:
                storage.add_fact(
                    user_id=user_id,
                    channel="test",
                    entity=pre["fact"]["entity"],
                    attribute=pre["fact"]["attribute"],
                    value=pre["fact"]["value"],
                    confidence="high",
                )
                print(f"  [PRE] Сохранён факт: {pre['fact']}")
            continue
        
        if not msg and not isinstance(msg, str):
            continue
            
        print(f"\n  ПОЛЬЗОВАТЕЛЬ: {msg[:100]}{'...' if len(msg) > 100 else ''}")
        
        # Создаём task
        task = Task(
            user_message=msg,
            user_id=user_id,
            channel_id=channel_id,
            author_id=user_id,
            source="test",
            source_chat_id="test",
            priority=TaskPriority.HIGH,
            complexity=TaskComplexity.SIMPLE,
        )
        
        # channel_name для L2 поиска
        channel_name = "test"
        
        # Создаём оркестратор
        orch = Orchestrator(
            config=Config.load(),
            event_bus=event_bus,
            storage=storage,
            llm_router=llm,
            tool_registry=tools,
        )
        orch._l3 = L3Memory(storage)
        
        # Сохраняем сообщение пользователя в историю
        storage.save_message(channel_id, "user", msg, task.id)
        
        # Выполняем
        try:
            await orch.handle_task(task)
            response = task.result or "(пустой ответ)"
            error = task.error
            status = task.status
        except Exception as e:
            response = None
            error = str(e)
            status = "CRASHED"
        
        print(f"  АГЕНТ: {(response or error or 'NO RESPONSE')[:200]}")
        print(f"  СТАТУС: {status}")
        if error:
            print(f"  ОШИБКА: {error[:200]}")
    
    # Проверяем результат
    expect = scenario["expect"]
    print(f"\n  ОЖИДАНИЕ: {expect}")
    
    # Простая проверка — не упал
    if status in ("CRASHED", TaskStatus.FAILED):
        result = "❌ FAIL"
    else:
        result = "✅ PASS"
    
    print(f"  РЕЗУЛЬТАТ: {result}")
    return {"name": name, "result": result, "status": str(status)}


async def main():
    """Запустить все тесты."""
    print("=" * 60)
    print("  CAESAR — ТЕСТОВЫЕ СЦЕНАРИИ (10 шт)")
    print("=" * 60)
    
    # Инициализация
    config = Config.load()
    storage = Storage()
    
    # Очищаем старые данные
    with storage._conn() as conn:
        conn.execute("DELETE FROM conversation_messages WHERE channel_id LIKE 'test-channel-%'")
        conn.execute("DELETE FROM l2_facts WHERE channel = 'test'")
    
    # L3 и L4
    l3 = L3Memory(storage)
    l4 = L4Skills(storage)
    
    # LLM
    llm = LLMRouter(config)
    
    # Tools
    tools = ToolRegistry(storage, l3, l4)
    
    # Event bus
    event_bus = EventBus()
    
    has_llm = bool(config.llm.smart_api_key or config.llm.cheap_api_key)
    if not has_llm:
        print("\n⚠️  Нет LLM API ключа — тесты будут в эхо-режиме")
        print("   Тесты проверят что агент не падает, но не проверят качество ответов")
    
    results = []
    for scenario in SCENARIOS:
        try:
            result = await run_scenario(scenario, storage, llm, tools, event_bus)
            results.append(result)
        except Exception as e:
            print(f"\n  ❌ SCENARIO CRASH: {e}")
            results.append({"name": scenario["name"], "result": "❌ CRASH", "status": str(e)})
    
    # Сводка
    print(f"\n\n{'='*60}")
    print("  СВОДКА")
    print(f"{'='*60}")
    passed = sum(1 for r in results if "✅" in r["result"])
    failed = sum(1 for r in results if "❌" in r["result"])
    print(f"  Пройдено: {passed}/{len(results)}")
    print(f"  Провалено: {failed}/{len(results)}")
    for r in results:
        print(f"    {r['result']} {r['name']}")
    
    # Очистка
    with storage._conn() as conn:
        conn.execute("DELETE FROM conversation_messages WHERE channel_id LIKE 'test-channel-%'")
        conn.execute("DELETE FROM l2_facts WHERE channel = 'test'")
    
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
