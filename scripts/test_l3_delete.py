"""Тест: удаление из L3 по семантическому запросу и по тегу."""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "/home/z/my-project")

tmp_dir = tempfile.mkdtemp(prefix="caesar_l3_del_test_")
db_path = Path(tmp_dir) / "test.db"

import caesar.memory.storage as storage_module
storage_module.DB_PATH = db_path

from caesar.memory.storage import Storage
from caesar.memory.l3 import L3Memory, _get_embedding_model, DEFAULT_MODEL_KEY


async def main():
    print("=" * 60)
    print("L3 DELETE TEST")
    print("=" * 60)

    print("\n--- 0. Check model ---")
    model = _get_embedding_model(DEFAULT_MODEL_KEY)
    if model is None:
        print("  ⚠️ sentence-transformers не установлен — тест пропускается")
        return
    print(f"  ✓ Model loaded")

    print("\n--- 1. Init + add documents ---")
    storage = Storage()
    l3 = L3Memory(storage, model_key=DEFAULT_MODEL_KEY)

    # Добавляем разные тексты
    docs = [
        ("user-1", "documents", "Шашлык из свинины: маринад с уксусом, лук, перец. Жарить 20 мин.", {"file_name": "recipes.txt", "tag": "food"}),
        ("user-1", "documents", "Hermes Agent — опенсорсный AI-агент от Nous Research. 208K звёзд.", {"file_name": "news.txt"}),
        ("user-1", "documents", "Борщ украинский: свёкла, капуста, томатная паста, чеснок.", {"file_name": "recipes.txt", "tag": "food"}),
        ("user-1", "documents", "Caesar — автономный AI-агент на Ubuntu с 4-уровневой памятью.", {"file_name": "tech.txt"}),
        ("user-1", "main", "Пользователь сказал: я люблю шашлык по-карски.", {}),
    ]
    for user_id, channel, content, meta in docs:
        await l3.add(user_id=user_id, channel=channel, content=content, author_id=user_id, metadata=meta)
    print(f"  ✓ Added {len(docs)} chunks, cache: {len(l3._vectors_cache)}")

    print("\n--- 2. Delete by query 'шашлык рецепты' ---")
    result = await l3.delete_by_query(query="шашлык рецепты", user_id="user-1")
    print(f"  deleted: {result['deleted']}")
    for chunk in result.get("deleted_chunks", [])[:5]:
        print(f"    - score: {chunk['score']:.3f}, content: {chunk['content'][:80]}")
    assert result["deleted"] > 0, "Should have deleted shashlik-related chunks"
    print(f"  ✓ Deleted {result['deleted']} chunks about shashlik")

    print("\n--- 3. Verify shashlik gone, others remain ---")
    results = await l3.search(query="шашлык", user_id="user-1", final_k=5)
    print(f"  Search 'шашлык': {len(results)} results (should be few or none)")
    for r in results:
        print(f"    - score: {r.score:.3f}, content: {r.content[:80]}")

    results = await l3.search(query="Hermes Agent", user_id="user-1", final_k=3)
    print(f"\n  Search 'Hermes Agent': {len(results)} results (should still be there)")
    for r in results:
        print(f"    - score: {r.score:.3f}, content: {r.content[:80]}")
    assert len(results) > 0, "Hermes should still be in L3"
    print(f"  ✓ Hermes still in L3 (not deleted)")

    print("\n--- 4. Delete by tag 'food' ---")
    # Добавим fresh чанки с тегом food (предыдущие могли быть удалены query)
    await l3.add(user_id="user-1", channel="documents",
                 content="Паста карбонара: бекон, яйца, пармезан, спагетти.",
                 author_id="user-1", metadata={"file_name": "pasta.txt", "tag": "food"})
    await l3.add(user_id="user-1", channel="documents",
                 content="Ризотто с грибами: арборио, бульон, пармезан, белое вино.",
                 author_id="user-1", metadata={"file_name": "risotto.txt", "tag": "food"})
    print("  Added 2 fresh food chunks")
    
    result = await l3.delete_by_tag(user_id="user-1", tag="food")
    print(f"  deleted: {result['deleted']}")
    for chunk in result.get("deleted_chunks", [])[:5]:
        print(f"    - content: {chunk['content'][:80]}")
    assert result["deleted"] > 0, "Should have deleted food-tagged chunks"
    print(f"  ✓ Deleted {result['deleted']} chunks with tag 'food'")

    print("\n--- 5. Delete by file_name 'news.txt' ---")
    result = await l3.delete_by_tag(user_id="user-1", tag="news.txt")
    print(f"  deleted: {result['deleted']}")
    assert result["deleted"] > 0, "Should have deleted news.txt chunks"
    print(f"  ✓ Deleted news.txt chunks")

    print("\n--- 6. Final state ---")
    print(f"  Cache size: {len(l3._vectors_cache)}")
    results = await l3.search(query="любой запрос", user_id="user-1", final_k=10)
    print(f"  Total remaining chunks: {len(results)}")
    for r in results:
        print(f"    - {r.content[:80]}")

    import shutil
    shutil.rmtree(tmp_dir, ignore_errors=True)
    print("\n=== ALL TESTS PASSED ===")


if __name__ == "__main__":
    asyncio.run(main())
