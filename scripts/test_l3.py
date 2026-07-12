"""End-to-end тест L3 векторной памяти."""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "/home/z/my-project")

tmp_dir = tempfile.mkdtemp(prefix="caesar_l3_test_")
db_path = Path(tmp_dir) / "test.db"

import caesar.memory.storage as storage_module
storage_module.DB_PATH = db_path

from caesar.memory.storage import Storage
from caesar.memory.l3 import L3Memory, _get_embedding_model, DEFAULT_MODEL_KEY


async def main():
    print("=" * 60)
    print("L3 VECTOR MEMORY E2E TEST")
    print("=" * 60)

    print("\n--- 0. Check model ---")
    model = _get_embedding_model(DEFAULT_MODEL_KEY)
    if model is None:
        print("  ⚠️ sentence-transformers не установлен — тест пропускается")
        return
    print(f"  ✓ Model loaded: {DEFAULT_MODEL_KEY}")

    print("\n--- 1. Init Storage + L3 ---")
    storage = Storage()
    l3 = L3Memory(storage, model_key=DEFAULT_MODEL_KEY)
    print(f"  ✓ L3 cache: {len(l3._vectors_cache)} vectors")

    print("\n--- 2. Add dialogues ---")
    dialogues = [
        ("user-1", "main", "Пользователь сказал: я люблю шашлык по-карски с дымком."),
        ("user-1", "main", "Обсуждали отпуск в Греции: острова, пляжи, средиземноморская кухня."),
        ("user-1", "main", "Caesar — автономный AI-агент на Ubuntu."),
        ("user-1", "main", "Пользователь жаловался на медленный интернет — 5 Мбит/сек."),
        ("user-1", "main", "Готовили борщ: свёкла, капуста, томатная паста."),
    ]
    for user_id, channel, content in dialogues:
        await l3.add(user_id=user_id, channel=channel, content=content, author_id=user_id)
    print(f"  ✓ Added {len(dialogues)} dialogues, cache: {len(l3._vectors_cache)}")

    print("\n--- 3. Search 'что я люблю есть?' ---")
    results = await l3.search(query="что я люблю есть?", user_id="user-1", channel="main", final_k=3)
    for i, r in enumerate(results):
        print(f"    [{i+1}] score={r.score:.3f}: {r.content[:80]}")
    top = " ".join(r.content.lower() for r in results[:2])
    assert "шашлык" in top or "борщ" in top, f"Expected food in top-2: {top}"
    print("  ✓ Food query found food chunks")

    print("\n--- 4. Search 'какой у меня интернет?' ---")
    results = await l3.search(query="какой у меня интернет скорость?", user_id="user-1", final_k=2)
    for i, r in enumerate(results):
        print(f"    [{i+1}] score={r.score:.3f}: {r.content[:80]}")
    top = " ".join(r.content.lower() for r in results)
    assert "интернет" in top, f"Expected internet: {top}"
    print("  ✓ Internet query found internet chunk")

    print("\n--- 5. Restart simulation ---")
    l3_new = L3Memory(storage, model_key=DEFAULT_MODEL_KEY)
    # Cache теперь lazy — вызываем явно для теста
    l3_new._load_cache()
    print(f"  Restored: {len(l3_new._vectors_cache)} vectors")
    assert len(l3_new._vectors_cache) > 0, "Cache should restore from DB"
    print("  ✓ Cache restored from chunk_metadata JSON")

    import shutil
    shutil.rmtree(tmp_dir, ignore_errors=True)
    print("\n=== ALL TESTS PASSED ===")


if __name__ == "__main__":
    asyncio.run(main())
