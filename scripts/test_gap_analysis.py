"""Тесты для Recency boost, Gap Analysis v2 и Stale entities flagging.

3 компонента:
1. Recency boost в L3 search — чанки моложе 24h/7d бустятся
2. Gap Analysis v2 в orchestrator — проверяет пустой L3, старые чанки, consolidated-only
3. Stale entities flagging в dream.py Phase 6 — помечает entities >30 дней
"""

import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))


# ============================================================
# TEST 1: Recency boost
# ============================================================
async def test_recency_boost():
    """Recency boost — свежие чанки должны получать буст к score."""
    from caesar.memory.l3 import L3Memory, L3SearchResult
    from caesar.memory.storage import Storage
    
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        storage = Storage(db_path=tmpdir / "test.db")
        
        user_id = "test-user"
        storage.upsert_user(user_id=user_id, unix_uid=os.getuid(), display_name="Test")
        storage.upsert_channel(
            channel_id=f"channel:{user_id}:main",
            user_id=user_id, source="cli", source_chat_id="t", display_name="main",
        )
        
        l3 = L3Memory(storage)
        
        # Stub embedding — все чанки получают ОДИНАКОВЫЙ вектор
        # (чтобы cosine similarity был одинаковым, и только recency boost
        # влиял на финальный score)
        import caesar.memory.l3 as l3_module
        original_embed = l3_module._embed
        same_vec = [0.5, 0.5, 0.5]
        l3_module._embed = lambda text, model_key="multilingual-minilm": same_vec
        
        # Создаём 3 чанка с РАЗНЫМИ датами
        # 1. Свежий (<24h)
        # 2. Недавний (3 дня назад)
        # 3. Старый (60 дней назад)
        
        # Текст одинаковый, чтобы embedding был тот же
        text = "Python asyncio programming tutorial"
        
        # Создаём через l3.add — это сохранит с created_at=now
        fresh_ids = await l3.add(
            user_id=user_id, channel="main", content=text,
            metadata={"label": "fresh"},
        )
        
        # Для недавнего и старого — меняем created_at через UPDATE в БД
        # потому что l3.add сохраняет с now()
        recent_ids = await l3.add(
            user_id=user_id, channel="main", content=text,
            metadata={"label": "recent"},
        )
        old_ids = await l3.add(
            user_id=user_id, channel="main", content=text,
            metadata={"label": "old"},
        )
        
        # Обновляем created_at
        now = datetime.now()
        recent_dt = now - timedelta(days=3)
        old_dt = now - timedelta(days=60)
        
        with storage._conn() as conn:
            conn.execute(
                "UPDATE l3_chunks SET created_at = ? WHERE id = ?",
                (recent_dt.strftime("%Y-%m-%d %H:%M:%S"), recent_ids[0]),
            )
            conn.execute(
                "UPDATE l3_chunks SET created_at = ? WHERE id = ?",
                (old_dt.strftime("%Y-%m-%d %H:%M:%S"), old_ids[0]),
            )
            conn.commit()
        
        # Сбрасываем cache
        l3._cache_loaded = False
        l3._vectors_cache = {}
        
        # Поиск С recency boost
        results = await l3.search(
            query="python asyncio", user_id=user_id,
            min_similarity=0.05,  # низкий порог чтобы точно прошли
            final_k=5,
            recency_boost_enabled=True,
        )
        
        assert len(results) == 3, f"Expected 3 results, got {len(results)}"
        
        # Должны быть отсортированы: fresh (×1.20) > recent (×1.10) > old (×1.00)
        # Но так как они ИДЕНТИЧНЫ по embedding, cosine sim одинаковый.
        # Recency boost определяет порядок.
        labels = [r.metadata.get("label") for r in results]
        assert labels[0] == "fresh", f"Expected fresh first, got {labels[0]}"
        assert labels[1] == "recent", f"Expected recent second, got {labels[1]}"
        assert labels[2] == "old", f"Expected old last, got {labels[2]}"
        
        # Проверяем что score fresh > recent > old
        assert results[0].score > results[1].score > results[2].score
        
        # Поиск БЕЗ recency boost — score должны быть одинаковыми
        l3._cache_loaded = False
        l3._vectors_cache = {}
        results_no_boost = await l3.search(
            query="python asyncio", user_id=user_id,
            min_similarity=0.05, final_k=5,
            recency_boost_enabled=False,
        )
        
        assert len(results_no_boost) == 3
        # Без буста все score должны быть примерно одинаковыми
        scores = [r.score for r in results_no_boost]
        assert max(scores) - min(scores) < 0.01, f"Scores should be equal: {scores}"
        
        print(f"  ✅ Fresh > Recent > Old ordering correct")
        print(f"    Fresh: {results[0].score:.3f} (boost ×1.20)")
        print(f"    Recent: {results[1].score:.3f} (boost ×1.10)")
        print(f"    Old: {results[2].score:.3f} (boost ×1.00)")
        print(f"  ✅ No-boost scores equal: {scores}")
        
        l3_module._embed = original_embed


# ============================================================
# TEST 2: Gap Analysis — empty L3
# ============================================================
async def test_gap_analysis_empty_l3():
    """Gap Analysis — пустой L3, но в памяти есть другие чанки."""
    from caesar.core.orchestrator import Orchestrator
    from caesar.memory.storage import Storage
    
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        storage = Storage(db_path=tmpdir / "test.db")
        
        user_id = "test-user"
        storage.upsert_user(user_id=user_id, unix_uid=os.getuid(), display_name="Test")
        storage.upsert_channel(
            channel_id=f"channel:{user_id}:main",
            user_id=user_id, source="cli", source_chat_id="t", display_name="main",
        )
        
        # Создаём несколько чанков (про другое — не про наш запрос)
        with storage._conn() as conn:
            for i in range(5):
                conn.execute(
                    """INSERT INTO l3_chunks (id, user_id, channel, content, chunk_metadata)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        f"chunk-{i}", user_id, "main",
                        f"Some random content {i} about cooking",
                        json.dumps({"auto_indexed": True}),
                    ),
                )
            conn.commit()
        
        # Mock orchestrator с нужными зависимостями
        orch = Orchestrator.__new__(Orchestrator)  # без __init__
        orch.storage = storage
        orch._kg = MagicMock()
        orch._kg.search_entities = MagicMock(return_value=[])
        # Mock extract_entities чтобы вернуть пустой список
        with patch("caesar.memory.knowledge_graph.extract_entities", return_value=[]):
            gap = orch._gap_analysis_empty_l3("что-то про космос", user_id)
        
        # Должно быть предупреждение что в памяти есть чанки, но по этому запросу — пусто
        assert "ничего не найдено" in gap.lower(), f"Expected 'not found' warning, got: {gap}"
        assert "5" in gap, f"Expected total_chunks=5 in message, got: {gap}"
        print(f"  ✅ Empty L3 warning: {gap[:100]}...")


async def test_gap_analysis_empty_l3_truly_empty():
    """Gap Analysis — L3 совсем пустой (нет ни одного чанка)."""
    from caesar.core.orchestrator import Orchestrator
    from caesar.memory.storage import Storage
    
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        storage = Storage(db_path=tmpdir / "test.db")
        
        user_id = "test-user"
        storage.upsert_user(user_id=user_id, unix_uid=os.getuid(), display_name="Test")
        storage.upsert_channel(
            channel_id=f"channel:{user_id}:main",
            user_id=user_id, source="cli", source_chat_id="t", display_name="main",
        )
        
        # Не создаём НИ ОДНОГО чанка
        
        orch = Orchestrator.__new__(Orchestrator)
        orch.storage = storage
        orch._kg = MagicMock()
        orch._kg.search_entities = MagicMock(return_value=[])
        
        with patch("caesar.memory.knowledge_graph.extract_entities", return_value=[]):
            gap = orch._gap_analysis_empty_l3("что-то", user_id)
        
        # Должна быть пустая строка — нет ничего предупреждать
        assert gap == "", f"Expected empty gap, got: {gap}"
        print(f"  ✅ Truly empty L3 → no warning (correct)")


# ============================================================
# TEST 3: Gap Analysis — stale chunks
# ============================================================
async def test_gap_analysis_stale_chunks():
    """Gap Analysis — L3 results все старые (>30 дней)."""
    from caesar.core.orchestrator import Orchestrator
    from caesar.memory.l3 import L3SearchResult
    
    orch = Orchestrator.__new__(Orchestrator)
    orch.storage = MagicMock()
    orch._kg = MagicMock()
    orch._kg.search_entities = MagicMock(return_value=[])
    
    with patch("caesar.memory.knowledge_graph.extract_entities", return_value=[]):
        # Создаём L3 results со старыми created_at
        old_date = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d %H:%M:%S")
        results = [
            L3SearchResult(
                chunk_id="c1", content="content 1", channel="main",
                score=0.7, metadata={"created_at": old_date},
            ),
            L3SearchResult(
                chunk_id="c2", content="content 2", channel="main",
                score=0.6, metadata={"created_at": old_date},
            ),
        ]
        
        gap = orch._gap_analysis("что-то", "user-1", results)
        
        assert "нет свежих данных" in gap.lower(), f"Expected 'no fresh data' warning, got: {gap}"
        assert "60" in gap, f"Expected 60 days, got: {gap}"
        print(f"  ✅ Stale chunks warning: {gap[:100]}...")


async def test_gap_analysis_consolidated_only():
    """Gap Analysis — только consolidated чанки, нет свежих индивидуальных."""
    from caesar.core.orchestrator import Orchestrator
    from caesar.memory.l3 import L3SearchResult
    
    orch = Orchestrator.__new__(Orchestrator)
    orch.storage = MagicMock()
    orch._kg = MagicMock()
    orch._kg.search_entities = MagicMock(return_value=[])
    
    with patch("caesar.memory.knowledge_graph.extract_entities", return_value=[]):
        # СВЕЖИЕ consolidated чанки (не старые) — чтобы проверить consolidated-only warning
        fresh_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        results = [
            L3SearchResult(
                chunk_id="c1", content="content 1", channel="main",
                score=0.7,
                metadata={"created_at": fresh_date, "type": "consolidated"},
            ),
        ]
        
        gap = orch._gap_analysis("что-то", "user-1", results)
        
        assert "consolidated" in gap.lower(), f"Expected consolidated warning, got: {gap}"
        print(f"  ✅ Consolidated-only warning: {gap[:100]}...")


async def test_gap_analysis_fresh_chunks_no_warning():
    """Gap Analysis — есть свежие чанки, не должно быть stale warning."""
    from caesar.core.orchestrator import Orchestrator
    from caesar.memory.l3 import L3SearchResult
    
    orch = Orchestrator.__new__(Orchestrator)
    orch.storage = MagicMock()
    orch._kg = MagicMock()
    orch._kg.search_entities = MagicMock(return_value=[])
    
    with patch("caesar.memory.knowledge_graph.extract_entities", return_value=[]):
        fresh_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        results = [
            L3SearchResult(
                chunk_id="c1", content="content 1", channel="main",
                score=0.7, metadata={"created_at": fresh_date},
            ),
        ]
        
        gap = orch._gap_analysis("что-то", "user-1", results)
        
        # Не должно быть stale/consolidated warnings
        assert "нет свежих" not in gap.lower()
        assert "consolidated" not in gap.lower()
        assert gap == "", f"Expected empty gap for fresh chunks, got: {gap}"
        print(f"  ✅ Fresh chunks → no warning (correct)")


# ============================================================
# TEST 4: Stale entities flagging (Phase 6)
# ============================================================
async def test_stale_entities_flagging():
    """Phase 6: пометить entities >30 дней как stale."""
    from caesar.core.dream import DreamCycle
    from caesar.memory.storage import Storage
    
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        storage = Storage(db_path=tmpdir / "test.db")
        
        user_id = "test-user"
        storage.upsert_user(user_id=user_id, unix_uid=os.getuid(), display_name="Test")
        storage.upsert_channel(
            channel_id=f"channel:{user_id}:main",
            user_id=user_id, source="cli", source_chat_id="t", display_name="main",
        )
        
        # Создаём 3 entities: fresh, stale, very stale
        now = datetime.now()
        fresh_dt = now - timedelta(days=5)
        stale_dt = now - timedelta(days=45)
        very_stale_dt = now - timedelta(days=120)
        
        with storage._conn() as conn:
            for i, (name, dt) in enumerate([
                ("FreshEntity", fresh_dt),
                ("StaleEntity", stale_dt),
                ("VeryStaleEntity", very_stale_dt),
            ]):
                conn.execute(
                    """INSERT INTO kg_entities 
                       (id, user_id, name, entity_type, first_seen, last_seen, 
                        mention_count, metadata)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        f"ent-{i}", user_id, name, "concept",
                        dt.strftime("%Y-%m-%d %H:%M:%S"),
                        dt.strftime("%Y-%m-%d %H:%M:%S"),
                        5,  # mention_count
                        json.dumps({}),
                    ),
                )
            conn.commit()
        
        dream = DreamCycle(
            config=MagicMock(),
            storage=storage,
            kg=MagicMock(),  # KG не используется в Phase 6 напрямую
            llm_router=None,
            l3_memory=None,
        )
        
        flagged = await dream._phase_stale_entities(user_id)
        
        # Должно быть помечено 2 (StaleEntity и VeryStaleEntity)
        assert flagged == 2, f"Expected 2 flagged, got {flagged}"
        
        # Проверяем что FreshEntity НЕ помечен
        with storage._conn() as conn:
            for name in ["FreshEntity", "StaleEntity", "VeryStaleEntity"]:
                row = conn.execute(
                    "SELECT metadata FROM kg_entities WHERE user_id = ? AND name = ?",
                    (user_id, name),
                ).fetchone()
                meta = json.loads(row["metadata"])
                
                if name == "FreshEntity":
                    assert not meta.get("stale"), f"{name} should NOT be marked stale"
                else:
                    assert meta.get("stale"), f"{name} should be marked stale"
                    assert "stale_since" in meta
                    assert meta.get("stale_days", 0) > 30
        
        print(f"  ✅ {flagged} entities flagged stale, FreshEntity preserved")


async def test_stale_entities_unflag_on_reactivation():
    """Phase 6: снять пометку stale если entity снова активен."""
    from caesar.core.dream import DreamCycle
    from caesar.memory.storage import Storage
    
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        storage = Storage(db_path=tmpdir / "test.db")
        
        user_id = "test-user"
        storage.upsert_user(user_id=user_id, unix_uid=os.getuid(), display_name="Test")
        storage.upsert_channel(
            channel_id=f"channel:{user_id}:main",
            user_id=user_id, source="cli", source_chat_id="t", display_name="main",
        )
        
        # Entity с old last_seen (был stale), но потом реактивировался — last_seen=now
        # Но metadata.stale=True ещё осталось
        now = datetime.now()
        
        with storage._conn() as conn:
            conn.execute(
                """INSERT INTO kg_entities 
                   (id, user_id, name, entity_type, first_seen, last_seen, 
                    mention_count, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "ent-1", user_id, "ReactivatedEntity", "concept",
                    now.strftime("%Y-%m-%d %H:%M:%S"),
                    now.strftime("%Y-%m-%d %H:%M:%S"),  # active now
                    5,
                    json.dumps({"stale": True, "stale_since": "2020-01-01", "stale_days": 100}),
                ),
            )
            conn.commit()
        
        dream = DreamCycle(
            config=MagicMock(), storage=storage, kg=MagicMock(),
            llm_router=None, l3_memory=None,
        )
        
        flagged = await dream._phase_stale_entities(user_id)
        
        # Должно быть 0 новых flagged, но 1 un-flagged
        assert flagged == 0, f"Expected 0 newly flagged, got {flagged}"
        
        # Проверяем что stale снят
        with storage._conn() as conn:
            row = conn.execute(
                "SELECT metadata FROM kg_entities WHERE name = ?", 
                ("ReactivatedEntity",),
            ).fetchone()
            meta = json.loads(row["metadata"])
            assert not meta.get("stale"), "stale should be removed after reactivation"
            assert "stale_since" not in meta
        
        print(f"  ✅ Reactivated entity: stale flag removed")


# ============================================================
# RUN ALL TESTS
# ============================================================
async def main():
    print("=" * 60)
    print("TEST 1: Recency boost — fresh > recent > old")
    print("=" * 60)
    await test_recency_boost()
    
    print()
    print("=" * 60)
    print("TEST 2a: Gap Analysis — empty L3 with other chunks")
    print("=" * 60)
    await test_gap_analysis_empty_l3()
    
    print()
    print("=" * 60)
    print("TEST 2b: Gap Analysis — truly empty L3")
    print("=" * 60)
    await test_gap_analysis_empty_l3_truly_empty()
    
    print()
    print("=" * 60)
    print("TEST 3a: Gap Analysis — all stale chunks")
    print("=" * 60)
    await test_gap_analysis_stale_chunks()
    
    print()
    print("=" * 60)
    print("TEST 3b: Gap Analysis — consolidated only")
    print("=" * 60)
    await test_gap_analysis_consolidated_only()
    
    print()
    print("=" * 60)
    print("TEST 3c: Gap Analysis — fresh chunks, no warning")
    print("=" * 60)
    await test_gap_analysis_fresh_chunks_no_warning()
    
    print()
    print("=" * 60)
    print("TEST 4a: Stale entities flagging")
    print("=" * 60)
    await test_stale_entities_flagging()
    
    print()
    print("=" * 60)
    print("TEST 4b: Stale entities — unflag on reactivation")
    print("=" * 60)
    await test_stale_entities_unflag_on_reactivation()
    
    print()
    print("🎉 All tests passed!")


if __name__ == "__main__":
    asyncio.run(main())
