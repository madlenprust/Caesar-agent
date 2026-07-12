"""Тест: L3 search quality — находит ли 'агрессия' в документе.

Воспроизводит баг пользователя:
- Документ про психологию (агрессия, кожный голод, Берн)
- Чаты про кожный голод (прошлые диалоги)
- Поиск 'агрессия' должен найти документ, а не кожный голод
"""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "/home/z/my-project")

tmp_dir = tempfile.mkdtemp(prefix="caesar_l3_search_test_")
db_path = Path(tmp_dir) / "test.db"

import caesar.memory.storage as storage_module
storage_module.DB_PATH = db_path

from caesar.memory.storage import Storage
from caesar.memory.l3 import L3Memory, _get_embedding_model, DEFAULT_MODEL_KEY


async def main():
    print("=" * 60)
    print("L3 SEARCH QUALITY TEST")
    print("=" * 60)

    print("\n--- 0. Check model ---")
    model = _get_embedding_model(DEFAULT_MODEL_KEY)
    if model is None:
        print("  ⚠️ sentence-transformers не установлен — тест пропускается")
        return
    print(f"  ✓ Model loaded")

    print("\n--- 1. Init ---")
    storage = Storage()
    l3 = L3Memory(storage, model_key=DEFAULT_MODEL_KEY)

    print("\n--- 2. Add document about aggression (channel='documents') ---")
    # Реальный документ про агрессию (как у пользователя)
    document_content = """
Агрессия — это форма поведения, направленная на причинение вреда другому
человеку. Эрих Фромм различал доброкачественную агрессию (биологически
адаптивную, служащую выживанию) и злокачественную агрессию (деструктивную,
не связанную с выживанием).

Виды агрессии:
1. Реактивная — ответная реакция на угрозу
2. Проактивная — заранее спланированная для достижения цели
3. Враждебная — причинение вреда ради самого вреда
4. Инструментальная — агрессия как средство достижения цели

Причины агрессии:
- Биологические: гормоны, генетика, повреждения мозга
- Психологические: фрустрация, стресс, низкая самооценка
- Социальные: научение, наблюдение насилия в семье
- Средовые: жара, шум, теснота

Теория фрустрации: когда цель блокируется, возникает фрустрация,
которая может привести к агрессии.

Эрик Берн в своих работах описывал агрессию как один из способов
получения "поглаживаний" — даже негативное внимание лучше
безразличия. Человек без поглаживаний умирает психологически.
""".strip()

    await l3.add(
        user_id="user-1",
        channel="documents",  # документы в отдельном канале
        content=document_content,
        author_id="user-1",
        metadata={"file_name": "psychology.txt", "source": "telegram_document"},
    )
    print("  ✓ Document indexed")

    print("\n--- 3. Add chat history about skin hunger (channel='main') ---")
    # Прошлые чаты про кожный голод (которые мешали найти агрессию)
    chat_contents = [
        "Пользователь сказал: я читал про кожный голод у младенцев.",
        "Обсуждали как важно давать детям тактильный контакт.",
        "Кожный голод — это потребность в прикосновениях.",
        "Берн писал про поглаживания как форму признания.",
    ]
    for content in chat_contents:
        await l3.add(
            user_id="user-1",
            channel="main",  # чаты в main
            content=content,
            author_id="user-1",
        )
    print(f"  ✓ Added {len(chat_contents)} chat chunks")

    print("\n--- 4. Search 'агрессия' (старый баг: нашёл кожный голод) ---")
    results = await l3.search(
        query="агрессия",
        user_id="user-1",
        channel="main",  # как раньше
        final_k=5,
        boost_same_channel=1.0,  # ОТКЛЮЧЕН
        min_similarity=0.15,
    )
    print(f"  Found {len(results)} results:")
    for i, r in enumerate(results):
        print(f"    [{i+1}] score={r.score:.3f} channel={r.channel}")
        print(f"        content: {r.content[:100]}")

    # Проверяем что агрессия в top-3
    top3_text = " ".join(r.content.lower() for r in results[:3])
    assert "агресс" in top3_text, \
        f"FAIL: 'агрессия' should be in top-3, got: {top3_text[:300]}"
    print("\n  ✓ 'агрессия' found in top-3 results!")

    # Проверяем что документ про агрессию в результатах
    doc_results = [r for r in results if r.channel == "documents"]
    assert len(doc_results) > 0, "FAIL: document chunk should be in results"
    print(f"  ✓ Document chunk in results (score: {doc_results[0].score:.3f})")

    print("\n--- 5. Search 'почему человек без поглаживания умирает' ---")
    results = await l3.search(
        query="почему человек без поглаживания умирает",
        user_id="user-1",
        channel="main",
        final_k=5,
        boost_same_channel=1.0,
        min_similarity=0.15,
    )
    print(f"  Found {len(results)} results:")
    for i, r in enumerate(results):
        print(f"    [{i+1}] score={r.score:.3f} channel={r.channel}")
        print(f"        content: {r.content[:100]}")

    # Должен найти чанк про Берна и поглаживания
    top_text = " ".join(r.content.lower() for r in results[:3])
    assert "поглаж" in top_text or "берн" in top_text, \
        f"FAIL: should find Bern/strokes content"
    print("  ✓ Found Bern/strokes content")

    print("\n--- 6. Compare: old boost (1.5) vs new (1.0) ---")
    # Сравним со старым багом
    results_old = await l3.search(
        query="агрессия",
        user_id="user-1",
        channel="main",
        final_k=5,
        boost_same_channel=1.5,  # старый баг
        min_similarity=0.0,  # без порога
    )
    # NEW results для "агрессия" (из шага 4)
    results_new_aggression = await l3.search(
        query="агрессия",
        user_id="user-1",
        channel="main",
        final_k=5,
        boost_same_channel=1.0,
        min_similarity=0.15,
    )
    print("  OLD (boost=1.5, no threshold) — search 'агрессия':")
    for i, r in enumerate(results_old[:3]):
        print(f"    [{i+1}] score={r.score:.3f} channel={r.channel}: {r.content[:80]}")

    print("\n  NEW (boost=1.0, threshold=0.15) — search 'агрессия':")
    for i, r in enumerate(results_new_aggression[:3]):
        print(f"    [{i+1}] score={r.score:.3f} channel={r.channel}: {r.content[:80]}")

    # Проверяем что NEW находит документ в top-1 для 'агрессия'
    new_has_doc = any(r.channel == "documents" for r in results_new_aggression[:3])
    assert new_has_doc, "NEW should find document in top-3 for 'агрессия'"
    print(f"\n  ✓ NEW finds document in top-3 for 'агрессия'")

    import shutil
    shutil.rmtree(tmp_dir, ignore_errors=True)
    print("\n=== ALL TESTS PASSED ===")


if __name__ == "__main__":
    asyncio.run(main())
