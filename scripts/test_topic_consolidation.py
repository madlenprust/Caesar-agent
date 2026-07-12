"""Тест ночной консолидации по топикам (Phase 5 в dream.py).

Создаёт синтетические L3 чанки (5 штук, по 2 темы), запускает
DreamCycle._phase_topic_consolidation, проверяет что:
1. Создались consolidated чанки
2. Source чанки помечены consolidated_in
3. Метаданные consolidated чанка корректны (type=consolidated, topic, source_chunks)
"""

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

# Добавляем корень проекта в sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))


async def test_topic_consolidation():
    """Базовый тест: 5 чанков → 2 темы → 2 consolidated чанка."""
    from caesar.core.dream import DreamCycle
    from caesar.core.llm import LLMMessage, LLMResponse
    from caesar.memory.l3 import L3Memory
    from caesar.memory.storage import Storage
    
    # Создаём временную БД
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        db_path = tmpdir / "test.db"
        
        storage = Storage(db_path=db_path)
        
        # Создаём пользователя
        user_id = "test-user"
        storage.upsert_user(
            user_id=user_id,
            unix_uid=os.getuid(),
            display_name="Test",
        )
        storage.upsert_channel(
            channel_id=f"channel:{user_id}:main",
            user_id=user_id,
            source="cli",
            source_chat_id="test",
            display_name="main",
        )
        
        # L3 — но мы заменим _embed чтобы не грузить модель
        l3 = L3Memory(storage)
        # Stub embedding — 5-мерные векторы чтобы было быстро
        l3._embed = lambda text, model_key="multilingual-minilm": [0.1, 0.2, 0.3, 0.4, 0.5]
        # Также для глобальной функции
        import caesar.memory.l3 as l3_module
        original_embed = l3_module._embed
        l3_module._embed = lambda text, model_key="multilingual-minilm": [0.1, 0.2, 0.3, 0.4, 0.5]
        
        # Создаём 5 чанков — 2 про Python, 2 про маркетинг, 1 trivial
        chunks_data = [
            ("Python: использую asyncio для I/O-bound задач. Async/await синтаксис.", "python"),
            ("В Python asyncio.gather запускает корутины параллельно. Важно: не блокируй event loop.", "python"),
            ("Для лендинга лучше A/B тестирование CTA кнопок. Цвет влияет на конверсию.", "marketing"),
            ("A/B тест: тестировал зелёную vs красную кнопку. Зелёная дала +12% конверсии.", "marketing"),
            ("ок, понятно", "trivial"),
        ]
        
        chunk_ids = []
        for content, _tag in chunks_data:
            ids = await l3.add(
                user_id=user_id,
                channel="main",
                content=content,
                metadata={"auto_indexed": True},
            )
            chunk_ids.extend(ids)
        
        print(f"  Created {len(chunk_ids)} source chunks")
        
        # Mock LLM — возвращает правдоподобный JSON
        mock_llm = MagicMock()
        mock_llm.cheap.api_key = "test-key"
        
        mock_response = LLMResponse(
            content=json.dumps([
                {
                    "topic": "Python asyncio",
                    "summary": "Пользователь обсуждал использование asyncio для I/O-bound задач в Python. "
                              "Упоминались async/await синтаксис и asyncio.gather для параллельного запуска корутин. "
                              "Главное правило — не блокировать event loop.",
                    "source_indices": [1, 2],
                },
                {
                    "topic": "A/B тестирование",
                    "summary": "Пользователь обсуждал A/B тестирование CTA кнопок на лендинге. "
                              "Тестировал зелёную vs красную кнопку. Зелёная дала +12% конверсии. "
                              "Цвет значимо влияет на конверсию.",
                    "source_indices": [3, 4],
                },
            ]),
            model="test",
                    )
        mock_llm.cheap_chat = AsyncMock(return_value=mock_response)
        
        # DreamCycle с mock LLM и реальным L3
        from caesar.config import Config
        config = Config.load() if hasattr(Config, 'load') else MagicMock()
        
        dream = DreamCycle(
            config=config,
            storage=storage,
            kg=MagicMock(),
            llm_router=mock_llm,
            l3_memory=l3,
        )
        
        # Запускаем topic consolidation (force_all=True)
        result = await dream._phase_topic_consolidation(user_id, force_all=True)
        
        print(f"  Result: {result}")
        
        # ПРОВЕРКИ
        # 1. Должно быть 2 темы
        assert result["topics"] == 2, f"Expected 2 topics, got {result['topics']}"
        # 2. Должно быть 2 новых consolidated чанка
        assert result["chunks_created"] == 2, f"Expected 2 chunks_created, got {result['chunks_created']}"
        # 3. Должно быть обработано 5 чанков
        assert result["chunks_processed"] == 5, f"Expected 5 chunks_processed, got {result['chunks_processed']}"
        
        # 4. Проверяем что в БД появились consolidated чанки
        with storage._conn() as conn:
            rows = conn.execute(
                "SELECT id, content, chunk_metadata FROM l3_chunks WHERE chunk_metadata LIKE '%\"type\": \"consolidated\"%'",
            ).fetchall()
            
            assert len(rows) == 2, f"Expected 2 consolidated chunks in DB, got {len(rows)}"
            
            for row in rows:
                meta = json.loads(row["chunk_metadata"])
                assert meta["type"] == "consolidated"
                assert "topic" in meta
                assert "source_chunks" in meta
                assert len(meta["source_chunks"]) == 2
                assert "consolidated_at" in meta
                print(f"    ✓ Consolidated chunk: topic='{meta['topic']}', sources={len(meta['source_chunks'])}")
        
        # 5. Проверяем что source чанки помечены consolidated_in
        with storage._conn() as conn:
            marked_count = 0
            for row in conn.execute(
                "SELECT chunk_metadata FROM l3_chunks WHERE chunk_metadata LIKE '%consolidated_in%'",
            ).fetchall():
                meta = json.loads(row["chunk_metadata"])
                if "consolidated_in" in meta and len(meta["consolidated_in"]) > 0:
                    marked_count += 1
            
            # 4 source чанка должны быть помечены (5-й trivial не должен попасть в topic)
            # Но по нашей логике _mark_consolidated вызывается только для чанков, попавших в topic.
            # 4 чанка (2+2) должны быть помечены.
            assert marked_count == 4, f"Expected 4 marked source chunks, got {marked_count}"
            print(f"    ✓ {marked_count} source chunks marked with consolidated_in")
        
        # Восстанавливаем _embed
        l3_module._embed = original_embed
        
        print("  ✅ All assertions passed!")
        return True


async def test_no_chunks():
    """Тест: нет чанков → возвращаем 0."""
    from caesar.core.dream import DreamCycle
    from caesar.memory.storage import Storage
    
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        storage = Storage(db_path=tmpdir / "test.db")
        
        mock_llm = MagicMock()
        mock_llm.cheap.api_key = "test-key"
        mock_llm.cheap_chat = AsyncMock()
        
        dream = DreamCycle(
            config=MagicMock(),
            storage=storage,
            kg=MagicMock(),
            llm_router=mock_llm,
            l3_memory=MagicMock(),
        )
        
        result = await dream._phase_topic_consolidation("", force_all=True)
        
        assert result["topics"] == 0
        assert result["chunks_created"] == 0
        assert result["chunks_processed"] == 0
        
        # LLM не должен вызываться
        mock_llm.cheap_chat.assert_not_called()
        
        print("  ✅ Empty case handled correctly")
        return True


async def test_no_llm():
    """Тест: нет LLM → graceful skip."""
    from caesar.core.dream import DreamCycle
    from caesar.memory.storage import Storage
    
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        storage = Storage(db_path=tmpdir / "test.db")
        
        mock_llm = MagicMock()
        mock_llm.cheap.api_key = ""  # нет ключа
        
        dream = DreamCycle(
            config=MagicMock(),
            storage=storage,
            kg=MagicMock(),
            llm_router=mock_llm,
            l3_memory=MagicMock(),
        )
        
        result = await dream._phase_topic_consolidation("", force_all=True)
        
        assert result["topics"] == 0
        print("  ✅ No-LLM case handled correctly")
        return True


async def test_already_consolidated_skipped():
    """Тест: уже консолидированные чанки пропускаются."""
    from caesar.core.dream import DreamCycle
    from caesar.core.llm import LLMResponse
    from caesar.memory.l3 import L3Memory
    from caesar.memory.storage import Storage
    
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        storage = Storage(db_path=tmpdir / "test.db")
        
        user_id = "test-user"
        storage.upsert_user(user_id=user_id, unix_uid=os.getuid(), display_name="Test")
        storage.upsert_channel(
            channel_id=f"channel:{user_id}:main",
            user_id=user_id,
            source="cli",
            source_chat_id="test",
            display_name="main",
        )
        
        l3 = L3Memory(storage)
        import caesar.memory.l3 as l3_module
        original_embed = l3_module._embed
        l3_module._embed = lambda text, model_key="multilingual-minilm": [0.1, 0.2, 0.3]
        
        # Создаём 3 чанка, два уже консолидированы
        ids1 = await l3.add(
            user_id=user_id, channel="main",
            content="Fresh chunk about Python asyncio that was never consolidated.",
            metadata={"auto_indexed": True},
        )
        ids2 = await l3.add(
            user_id=user_id, channel="main",
            content="Already consolidated chunk 1.",
            metadata={"auto_indexed": True, "consolidated_in": ["chunk-existing1"]},
        )
        ids3 = await l3.add(
            user_id=user_id, channel="main",
            content="Already consolidated chunk 2.",
            metadata={"auto_indexed": True, "consolidated_in": ["chunk-existing2"]},
        )
        
        # Также создадим consolidated чанк (должен пропуститься)
        ids4 = await l3.add(
            user_id=user_id, channel="main",
            content="[Consolidated] Тема: старая тема\n\nСтарое саммари",
            metadata={"type": "consolidated", "topic": "старая тема"},
        )
        
        mock_llm = MagicMock()
        mock_llm.cheap.api_key = "test-key"
        mock_response = LLMResponse(
            content="[]",  # LLM говорит — нет связанных тем
            model="test",
                    )
        mock_llm.cheap_chat = AsyncMock(return_value=mock_response)
        
        from caesar.config import Config
        config = Config.load() if hasattr(Config, 'load') else MagicMock()
        
        dream = DreamCycle(
            config=config,
            storage=storage,
            kg=MagicMock(),
            llm_router=mock_llm,
            l3_memory=l3,
        )
        
        result = await dream._phase_topic_consolidation(user_id, force_all=True)
        
        # Только 1 чанк должен быть обработан (fresh, без consolidated_in)
        assert result["chunks_processed"] == 1, f"Expected 1 processed, got {result['chunks_processed']}"
        assert result["topics"] == 0  # LLM вернул пустой массив
        print(f"  ✅ Already-consolidated chunks skipped correctly (processed={result['chunks_processed']})")
        
        l3_module._embed = original_embed
        return True


async def main():
    print("=" * 60)
    print("TEST 1: Topic consolidation basic flow")
    print("=" * 60)
    await test_topic_consolidation()
    
    print()
    print("=" * 60)
    print("TEST 2: No chunks to consolidate")
    print("=" * 60)
    await test_no_chunks()
    
    print()
    print("=" * 60)
    print("TEST 3: No LLM configured")
    print("=" * 60)
    await test_no_llm()
    
    print()
    print("=" * 60)
    print("TEST 4: Already consolidated chunks skipped")
    print("=" * 60)
    await test_already_consolidated_skipped()
    
    print()
    print("🎉 All tests passed!")


if __name__ == "__main__":
    asyncio.run(main())
