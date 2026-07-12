"""Тест socket-action 'index_memory' — end-to-end через CLIAdapter.

Проверяем что:
1. CLIAdapter.handle_request(action='index_memory') корректно вызывает
   daemon._run_dream_cycle_on_demand
2. Возвращает отчёт со статистикой
3. Обрабатывает ошибки (daemon=None)
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

sys.path.insert(0, str(Path(__file__).parent.parent))


async def test_index_memory_action():
    """Socket action 'index_memory' — happy path."""
    from caesar.channels.cli_adapter import CLIAdapter
    from caesar.config import Config
    from caesar.core.events import EventBus
    from caesar.core.queue import TaskQueue
    
    config = Config.load() if hasattr(Config, 'load') else MagicMock()
    
    adapter = CLIAdapter(
        config=config,
        event_bus=EventBus(),
        queue=MagicMock(),
        storage=MagicMock(),
        daemon=None,  # set below
    )
    
    # Mock daemon с методом _run_dream_cycle_on_demand
    mock_daemon = MagicMock()
    expected_report = {
        "topics_consolidated": 3,
        "chunks_created": 3,
        "chunks_processed": 15,
        "duration_sec": 12.5,
    }
    mock_daemon._run_dream_cycle_on_demand = AsyncMock(return_value=expected_report)
    adapter._daemon = mock_daemon
    
    # Вызываем action
    request = {
        "action": "index_memory",
        "force_all": True,
        "topic_only": True,
    }
    response = await adapter.handle_request(request)
    
    assert response == expected_report, f"Expected {expected_report}, got {response}"
    mock_daemon._run_dream_cycle_on_demand.assert_called_once_with(
        force_all=True,
        force_topic_only=True,
    )
    print(f"  ✅ index_memory action returns correct report: {response}")


async def test_index_memory_no_daemon():
    """Socket action 'index_memory' — daemon not connected."""
    from caesar.channels.cli_adapter import CLIAdapter
    from caesar.config import Config
    from caesar.core.events import EventBus
    
    config = Config.load() if hasattr(Config, 'load') else MagicMock()
    
    adapter = CLIAdapter(
        config=config,
        event_bus=EventBus(),
        queue=MagicMock(),
        storage=MagicMock(),
        daemon=None,
    )
    
    response = await adapter.handle_request({"action": "index_memory"})
    
    assert response.get("error") == "no_daemon"
    print(f"  ✅ No-daemon case returns error: {response}")


async def test_index_memory_with_exception():
    """Socket action 'index_memory' — daemon raises exception."""
    from caesar.channels.cli_adapter import CLIAdapter
    from caesar.config import Config
    from caesar.core.events import EventBus
    
    config = Config.load() if hasattr(Config, 'load') else MagicMock()
    
    adapter = CLIAdapter(
        config=config,
        event_bus=EventBus(),
        queue=MagicMock(),
        storage=MagicMock(),
        daemon=None,
    )
    
    mock_daemon = MagicMock()
    mock_daemon._run_dream_cycle_on_demand = AsyncMock(side_effect=RuntimeError("DB locked"))
    adapter._daemon = mock_daemon
    
    response = await adapter.handle_request({"action": "index_memory"})
    
    assert "error" in response
    assert "DB locked" in response["error"]
    print(f"  ✅ Exception case returns error: {response}")


async def test_unknown_action_still_works():
    """Regression: unknown actions still return error."""
    from caesar.channels.cli_adapter import CLIAdapter
    from caesar.config import Config
    from caesar.core.events import EventBus
    
    config = Config.load() if hasattr(Config, 'load') else MagicMock()
    
    adapter = CLIAdapter(
        config=config,
        event_bus=EventBus(),
        queue=MagicMock(),
        storage=MagicMock(),
        daemon=MagicMock(),
    )
    
    response = await adapter.handle_request({"action": "unknown_xyz"})
    assert response.get("error") == "unknown_action"
    print(f"  ✅ Unknown action still returns error: {response}")


async def main():
    print("=" * 60)
    print("TEST 1: index_memory happy path")
    print("=" * 60)
    await test_index_memory_action()
    
    print()
    print("=" * 60)
    print("TEST 2: index_memory with no daemon")
    print("=" * 60)
    await test_index_memory_no_daemon()
    
    print()
    print("=" * 60)
    print("TEST 3: index_memory with exception")
    print("=" * 60)
    await test_index_memory_with_exception()
    
    print()
    print("=" * 60)
    print("TEST 4: unknown action regression")
    print("=" * 60)
    await test_unknown_action_still_works()
    
    print()
    print("🎉 All tests passed!")


if __name__ == "__main__":
    asyncio.run(main())
