"""L3 — векторная память.

См. roadmap раздел 6.1, 14.8.

Использует:
- sentence-transformers для эмбеддингов
  - default: paraphrase-multilingual-MiniLM-L12-v2 (~470MB, 384dim, fast, good Russian)
  - alt: BAAI/bge-m3 (~2GB, 1024dim, best multilingual)
  - alt: all-MiniLM-L6-v2 (~80MB, 384dim, English-only)
- Векторы хранятся в chunk_metadata JSON (V1, достаточно для ~10K чанков)
- bge-reranker если доступен, иначе simple cosine top-K
- Работает на CPU (GPU не требуется, не используется)

Chunking: 256-512 токенов с перекрытием 50 (раздел 6.1).
"""

import asyncio
import hashlib
import json
import math
import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

# Подавляем предупреждения torch BEFORE импорта sentence-transformers.
# torch ругается на старые CUDA драйверы, но мы используем CPU — драйверы не нужны.
# Также forcing CPU mode чтобы не пытаться использовать GPU.
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")  # скрываем GPU от torch
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")  # убираем warning о parallelism

import warnings
warnings.filterwarnings("ignore", message=".*CUDA initialization.*")
warnings.filterwarnings("ignore", message=".*NVIDIA driver.*")
warnings.filterwarnings("ignore", category=UserWarning, module="torch")

from caesar.logging_setup import get_logger
from caesar.memory.storage import Storage

# Размер чанка в символах.
# Меньше = точнее семантика (каждый чанк про одну тему), но больше чанков.
# 800 символов ≈ 200 токенов — оптимально для фокусированного поиска.
CHUNK_SIZE = 800
CHUNK_OVERLAP = 100

# Доступные модели (по убыванию качества для русского)
AVAILABLE_MODELS = {
    "multilingual-minilm": {
        "name": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        "dim": 384,
        "size_mb": 470,
        "description": "Быстрая, мультиязычная, хороший русский (~470MB)",
    },
    "bge-m3": {
        "name": "BAAI/bge-m3",
        "dim": 1024,
        "size_mb": 2200,
        "description": "Лучшее качество, мультиязычная (~2.2GB)",
    },
    "minilm": {
        "name": "sentence-transformers/all-MiniLM-L6-v2",
        "dim": 384,
        "size_mb": 80,
        "description": "Самая быстрая, только английский (~80MB)",
    },
}

# Дефолтная модель (balance: качество + скорость + размер)
DEFAULT_MODEL_KEY = "multilingual-minilm"

# Глобальный кэш модели эмбеддингов
_embedding_model = None
_embedding_model_key: str | None = None
_embedding_dim = 384

# Callback для уведомления о прогрессе (например TG сообщение 'скачиваю модель...')
# Сигнатура: callback(stage: str, message: str)
# stage: 'download_start' | 'download_done' | 'load_start' | 'load_done' | 'error'
_progress_callback = None


def set_progress_callback(callback) -> None:
    """Установить callback для уведомления о прогрессе загрузки модели.
    
    Используется TG adapter-ом чтобы показать '📥 Скачиваю модель ~470MB...'
    при первой индексации документа.
    """
    global _progress_callback
    _progress_callback = callback


def _notify(stage: str, message: str) -> None:
    """Вызвать progress callback если установлен."""
    if _progress_callback:
        try:
            _progress_callback(stage, message)
        except Exception:
            pass


def _get_embedding_model(model_key: str = DEFAULT_MODEL_KEY):
    """Ленивая загрузка модели эмбеддингов.

    Возвращает None если sentence-transformers не установлен.
    L3 в этом случае просто не работает (возвращает пустые результаты).
    """
    global _embedding_model, _embedding_model_key, _embedding_dim, _embedding_unavailable

    if _embedding_model is not None and _embedding_model_key == model_key:
        return _embedding_model
    if getattr(_embedding_unavailable, '_set', False):
        return None
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        _embedding_unavailable._set = True
        return None

    model_info = AVAILABLE_MODELS.get(model_key, AVAILABLE_MODELS[DEFAULT_MODEL_KEY])
    size_mb = model_info.get("size_mb", "?")
    
    # Уведомляем что начинается скачивание/загрузка модели
    _notify("download_start", f"📥 Скачиваю модель эмбеддингов (~{size_mb}MB)...")
    
    try:
        _embedding_model = SentenceTransformer(model_info["name"])
        _embedding_dim = model_info["dim"]
        _embedding_model_key = model_key
        _notify("download_done", f"✅ Модель загружена")
    except Exception as e:
        _notify("error", f"⚠️ Ошибка загрузки модели: {e}")
        # Fallback: попробовать default если запрашивали другой
        if model_key != DEFAULT_MODEL_KEY:
            try:
                fallback_info = AVAILABLE_MODELS[DEFAULT_MODEL_KEY]
                _notify("download_start", f"📥 Пробую fallback модель (~{fallback_info['size_mb']}MB)...")
                _embedding_model = SentenceTransformer(fallback_info["name"])
                _embedding_dim = fallback_info["dim"]
                _embedding_model_key = DEFAULT_MODEL_KEY
                _notify("download_done", f"✅ Fallback модель загружена")
            except Exception:
                _embedding_unavailable._set = True
                return None
        else:
            _embedding_unavailable._set = True
            return None

    return _embedding_model


# Флаг что sentence-transformers недоступен
_embedding_unavailable = type('obj', (object,), {'_set': False})()


def _embed(text: str, model_key: str = DEFAULT_MODEL_KEY) -> list[float] | None:
    """Получить эмбеддинг текста. None если модель недоступна."""
    model = _get_embedding_model(model_key)
    if model is None:
        return None
    vec = model.encode(text, normalize_embeddings=True)
    return vec.tolist()


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity между двумя векторами."""
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Разбить текст на чанки с перекрытием."""
    if overlap >= size:
        overlap = size - 1  # защита от infinite loop
    if len(text) <= size:
        return [text]
    chunks = []
    start = 0
    while start < len(text):
        end = start + size
        # Пытаемся разорвать на границе предложения
        if end < len(text):
            for sep in ['. ', '! ', '? ', '\n\n', '\n']:
                last_sep = text[start:end].rfind(sep)
                if last_sep > size // 2:
                    end = start + last_sep + len(sep)
                    break
        chunks.append(text[start:end].strip())
        start = end - overlap
        if start >= len(text):
            break
    return chunks


@dataclass
class L3SearchResult:
    chunk_id: str
    content: str
    channel: str
    score: float
    metadata: dict


class L3Memory:
    """Векторная память L3.

    Хранит полные тексты всех каналов. При поиске:
    1. Векторный поиск top-K
    2. Реранк (буст того же канала)
    3. Top-3 в L1
    """

    def __init__(self, storage: Storage, model_key: str = DEFAULT_MODEL_KEY, max_cache_size: int = 2000):
        self.storage = storage
        self.model_key = model_key
        self.log = get_logger("l3")
        # Кэш эмбеддингов в RAM: chunk_id → vector
        # ОГРАНИЧЕН по размеру — чтобы не сожрать всю RAM при больших L3.
        # 2000 векторов × 384 float × ~100 bytes (Python list overhead) ≈ 80MB max.
        # Если чанков больше — кэшируем только последние (LRU-подобно).
        self._vectors_cache: dict[str, list[float]] = {}
        self._max_cache_size = max_cache_size
        # НЕ грузим кэш при init — lazy. Грузим только при первом search.
        self._cache_loaded = False

    def _load_cache(self) -> None:
        """Загрузить эмбеддинги в RAM (lazy — при первом search).
        
        ОГРАНИЧЕНО max_cache_size — чтобы не сожрать всю RAM.
        Если чанков больше — грузим только последние (по rowid DESC).
        """
        if self._cache_loaded:
            return
        self._cache_loaded = True
        
        try:
            with self.storage._conn() as conn:
                # Грузим только последние N чанков (по rowid — порядок вставки)
                rows = conn.execute(f"""
                    SELECT id, chunk_metadata FROM l3_chunks
                    ORDER BY rowid DESC LIMIT ?
                """, (self._max_cache_size,)).fetchall()
                loaded = 0
                skipped = 0
                for row in rows:
                    try:
                        meta = json.loads(row["chunk_metadata"] or "{}")
                        vec = meta.get("embedding")
                        if vec and isinstance(vec, list):
                            self._vectors_cache[row["id"]] = vec
                            loaded += 1
                    except (json.JSONDecodeError, KeyError, TypeError):
                        continue
            self.log.info(f"L3 cache loaded: {loaded} vectors (out of {len(rows)} chunks)")
        except Exception as e:
            self.log.warning(f"Cannot load L3 cache: {e}")

    async def add(
        self,
        user_id: str,
        channel: str,
        content: str,
        author_id: str | None = None,
        task_id: str | None = None,
        metadata: dict | None = None,
    ) -> list[str]:
        """Добавить текст в L3, разбить на чанки, сохранить эмбеддинги.

        Если sentence-transformers не установлен — сохраняет текст без эмбеддинга
        (поиск будет невозможен, но текст сохранится для аудита).
        """
        chunks = _chunk_text(content)
        chunk_ids: list[str] = []

        for chunk in chunks:
            chunk_id = f"chunk-{uuid.uuid4().hex[:12]}"
            # Считаем эмбеддинг (в потоке чтобы не блокировать)
            try:
                vector = await asyncio.to_thread(_embed, chunk, self.model_key)
            except Exception as e:
                self.log.warning(f"Embedding failed: {e}")
                vector = None

            # Сохраняем в SQLite
            with self.storage._conn() as conn:
                conn.execute("""
                    INSERT INTO l3_chunks (id, user_id, channel, author_id, content, chunk_metadata, task_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    chunk_id, user_id, channel, author_id, chunk,
                    json.dumps({
                        **(metadata or {}),
                        **({"embedding": vector} if vector else {}),
                        "hash": hashlib.sha256(chunk.encode()).hexdigest()[:16],
                    }, ensure_ascii=False),
                    task_id,
                ))

            # Кэш в RAM (только если есть вектор)
            if vector is not None:
                self._vectors_cache[chunk_id] = vector
                # LRU-подобная очистка — не даём кэшу расти сверх лимита
                if len(self._vectors_cache) >= self._max_cache_size:
                    # Удаляем самый старый элемент (первый вставленный)
                    oldest_key = next(iter(self._vectors_cache))
                    del self._vectors_cache[oldest_key]
            chunk_ids.append(chunk_id)

        self.log.debug(f"L3: added {len(chunk_ids)} chunks for channel={channel}")
        return chunk_ids

    async def search(
        self,
        query: str,
        user_id: str,
        channel: str | None = None,
        top_k: int = 20,
        final_k: int = 5,
        boost_same_channel: float = 1.0,  # ОТКЛЮЧЕН — был баг: чанки из main получали буст над документами
        min_similarity: float = 0.15,  # минимальный порог — не возвращаем мусор
        recency_boost_enabled: bool = True,  # свежие чанки бустятся
    ) -> list[L3SearchResult]:
        """Поиск в L3.

        1. Эмбеддинг запроса
        2. Top-K по cosine similarity (с минимальным порогом)
        3. Recency boost: чанки моложе 7 дней получают +10%, моложе 1 дня — +20%
        4. Top-final_k

        ВАЖНО: channel boost ОТКЛЮЧЕН по умолчанию.
        Раньше чанки из 'main' (прошлые чаты) получали буст 1.5x над
        чанками из 'documents' (загруженные документы). Это приводило к
        тому что релевантные документы проигрывали нерелевантным чатам.

        Recency boost включён по умолчанию. Если релевантный чанк свежий
        (последние 7 дней), он получает множитель 1.10. Если последние
        24 часа — 1.20. Это помогает боту предпочитать актуальные данные
        устаревшим. Отключается параметром recency_boost_enabled=False.

        Возвращает пустой список если:
        - sentence-transformers не установлен
        - В кэше нет векторов (L3 пуст)
        - Запрос не смог векторизоваться
        - Ничего не превышает min_similarity
        """
        # Lazy load cache при первом search
        if not self._cache_loaded:
            self._load_cache()
        
        if not self._vectors_cache:
            return []

        try:
            query_vec = await asyncio.to_thread(_embed, query, self.model_key)
        except Exception as e:
            self.log.warning(f"Query embedding failed: {e}")
            return []

        if query_vec is None:
            return []

        # Текущее время для recency boost (вычисляем один раз)
        now = datetime.now() if recency_boost_enabled else None

        # Загружаем все чанки пользователя (с created_at для recency)
        with self.storage._conn() as conn:
            rows = conn.execute(
                "SELECT id, content, channel, chunk_metadata, created_at FROM l3_chunks WHERE user_id = ?",
                (user_id,),
            ).fetchall()

        # Считаем similarity
        scored: list[tuple[float, dict]] = []
        for row in rows:
            d = dict(row)
            chunk_id = d["id"]
            vec = self._vectors_cache.get(chunk_id)
            if vec is None:
                try:
                    meta = json.loads(d.get("chunk_metadata") or "{}")
                    vec = meta.get("embedding")
                    if vec:
                        # Проверяем лимит кэша в search() тоже (как в add())
                        if len(self._vectors_cache) >= self._max_cache_size:
                            oldest_key = next(iter(self._vectors_cache))
                            del self._vectors_cache[oldest_key]
                        self._vectors_cache[chunk_id] = vec
                except (json.JSONDecodeError, KeyError):
                    continue
            if vec is None:
                continue

            score = _cosine_similarity(query_vec, vec)
            # Channel boost — ОТКЛЮЧЕН по умолчанию (boost_same_channel=1.0)
            # Причина: документы в channel="documents", чаты в channel="main".
            # Буст main над documents приводил к тому что релевантные документы
            # проигрывали нерелевантным чатам.
            if boost_same_channel != 1.0 and channel and d["channel"] == channel:
                score *= boost_same_channel

            # Recency boost — свежие чанки приоритетнее.
            # <24h: ×1.20, <7d: ×1.10, иначе ×1.00.
            # Важно: буст НЕ поднимает чанк выше порога min_similarity —
            # мы применяем его только если чанк уже прошёл фильтр.
            if recency_boost_enabled and score >= min_similarity and now is not None:
                created_at_str = d.get("created_at") or ""
                if created_at_str:
                    try:
                        # SQLite TIMESTAMP может быть как "2024-01-01 12:00:00"
                        # так и ISO с 'T'. Проверяем оба формата.
                        created_at_str_norm = created_at_str.replace("T", " ")
                        created_at = datetime.strptime(
                            created_at_str_norm.split(".")[0],
                            "%Y-%m-%d %H:%M:%S",
                        )
                        age = now - created_at
                        if age.total_seconds() < 86400:  # <24h
                            score *= 1.20
                        elif age.days < 7:  # <7d
                            score *= 1.10
                    except (ValueError, TypeError):
                        pass  # битая дата — пропускаем буст

            # Минимальный порог — не возвращаем мусор
            if score >= min_similarity:
                scored.append((score, d))

        if not scored:
            self.log.info(f"L3 search '{query[:50]}': no vector results, trying keyword fallback")
            return self._keyword_search(query, user_id, final_k)

        scored.sort(key=lambda x: x[0], reverse=True)
        top = scored[:top_k]

        results: list[L3SearchResult] = []
        for score, d in top[:final_k]:
            try:
                metadata = json.loads(d.get("chunk_metadata") or "{}")
            except json.JSONDecodeError:
                metadata = {}
            metadata.pop("embedding", None)
            # Сохраняем created_at в metadata для gap analysis
            if "created_at" not in metadata and d.get("created_at"):
                metadata["created_at"] = d["created_at"]
            results.append(L3SearchResult(
                chunk_id=d["id"],
                content=d["content"],
                channel=d["channel"],
                score=score,
                metadata=metadata,
            ))

        self.log.info(
            f"L3 search '{query[:50]}': {len(scored)} above threshold, "
            f"returning top {len(results)} (best score: {results[0].score:.3f})"
            if results else f"L3 search '{query[:50]}': no results"
        )

        return results
    
    def _keyword_search(self, query: str, user_id: str, limit: int) -> list[L3SearchResult]:
        """Fallback: keyword search (LIKE) когда vector search ничего не нашёл.
        
        Ищет чанки где content содержит слова из query.
        Возвращает результаты с score=0.1 (низкий, но не 0).
        """
        # Разбиваем query на слова
        words = [w for w in query.lower().split() if len(w) > 3]
        if not words:
            return []
        
        with self.storage._conn() as conn:
            # LIKE search по content
            conditions = " AND ".join(["LOWER(content) LIKE ?" for _ in words])
            
            rows = conn.execute(
                f"""SELECT id, content, channel, chunk_metadata FROM l3_chunks 
                   WHERE user_id = ? AND ({conditions})
                   ORDER BY rowid DESC LIMIT ?""",
                [user_id] + [f"%{w}%" for w in words] + [limit * 2],
            ).fetchall()
        
        if not rows:
            return []
        
        results = []
        for d in rows:
            try:
                metadata = json.loads(d.get("chunk_metadata") or "{}")
            except json.JSONDecodeError:
                metadata = {}
            metadata.pop("embedding", None)
            results.append(L3SearchResult(
                chunk_id=d["id"],
                content=d["content"],
                channel=d["channel"],
                score=0.1,  # низкий score — keyword match
                metadata=metadata,
            ))
        
        self.log.info(f"Keyword fallback: found {len(results)} chunks for '{query[:50]}'")
        return results[:limit]
    
    async def delete_by_query(
        self,
        query: str,
        user_id: str,
        threshold: float = 0.3,
        max_delete: int = 50,
    ) -> dict:
        """Удалить чанки из L3 по семантическому запросу.
        
        Используется когда пользователь говорит 'удали информацию про X'.
        Находим все чанки с similarity > threshold и удаляем.
        
        Args:
            query: что удалить ('информация про шашлык')
            user_id: чьи чанки удалять
            threshold: минимальная similarity для удаления (0.3 = ~30% схожести)
            max_delete: максимум чанков за один вызов (защита от случайного удаления всего)
        
        Returns:
            {"deleted": N, "deleted_chunks": [{"content": "...", "score": 0.7}, ...]}
        """
        if not self._vectors_cache:
            return {"deleted": 0, "deleted_chunks": [], "reason": "L3 пуст"}
        
        try:
            query_vec = await asyncio.to_thread(_embed, query, self.model_key)
        except Exception as e:
            self.log.warning(f"Delete query embedding failed: {e}")
            return {"deleted": 0, "deleted_chunks": [], "error": str(e)}
        
        if query_vec is None:
            return {"deleted": 0, "deleted_chunks": [], "reason": "model unavailable"}
        
        # Загружаем все чанки пользователя
        with self.storage._conn() as conn:
            rows = conn.execute(
                "SELECT id, content, channel, chunk_metadata FROM l3_chunks WHERE user_id = ?",
                (user_id,),
            ).fetchall()
        
        # Находим кандидатов на удаление
        to_delete: list[tuple[float, str, str, str]] = []  # (score, chunk_id, content_preview, channel)
        for row in rows:
            d = dict(row)
            chunk_id = d["id"]
            vec = self._vectors_cache.get(chunk_id)
            if vec is None:
                try:
                    meta = json.loads(d.get("chunk_metadata") or "{}")
                    vec = meta.get("embedding")
                except (json.JSONDecodeError, KeyError):
                    continue
            if vec is None:
                continue
            
            score = _cosine_similarity(query_vec, vec)
            if score >= threshold:
                content_preview = d["content"][:200]
                to_delete.append((score, chunk_id, content_preview, d["channel"]))
        
        if not to_delete:
            return {"deleted": 0, "deleted_chunks": [], "reason": "nothing matched"}
        
        # Сортируем по убыванию similarity (сначала самые релевантные)
        to_delete.sort(key=lambda x: x[0], reverse=True)
        
        # Ограничиваем количество
        to_delete = to_delete[:max_delete]
        
        # Удаляем из БД
        chunk_ids = [item[1] for item in to_delete]
        with self.storage._conn() as conn:
            placeholders = ",".join("?" * len(chunk_ids))
            conn.execute(
                f"DELETE FROM l3_chunks WHERE id IN ({placeholders})",
                chunk_ids,
            )
            conn.commit()
        
        # Удаляем из кэша
        for cid in chunk_ids:
            self._vectors_cache.pop(cid, None)
        
        deleted_info = [
            {"content": item[2], "score": item[0], "channel": item[3]}
            for item in to_delete
        ]
        
        self.log.info(f"L3: deleted {len(chunk_ids)} chunks by query '{query[:50]}'")
        return {
            "deleted": len(chunk_ids),
            "deleted_chunks": deleted_info,
        }
    
    async def delete_by_tag(
        self,
        user_id: str,
        tag: str,
    ) -> dict:
        """Удалить чанки по тегу (из метаданных).
        
        Например пользователь индексировал документ с тегом 'важное',
        теперь хочет удалить всё с этим тегом.
        """
        with self.storage._conn() as conn:
            rows = conn.execute(
                "SELECT id, content, chunk_metadata FROM l3_chunks WHERE user_id = ?",
                (user_id,),
            ).fetchall()
        
        to_delete = []
        for row in rows:
            d = dict(row)
            try:
                meta = json.loads(d.get("chunk_metadata") or "{}")
                if meta.get("tag") == tag or meta.get("file_name") == tag:
                    to_delete.append((d["id"], d["content"][:200]))
            except (json.JSONDecodeError, KeyError):
                continue
        
        if not to_delete:
            return {"deleted": 0, "reason": f"no chunks with tag '{tag}'"}
        
        chunk_ids = [item[0] for item in to_delete]
        with self.storage._conn() as conn:
            placeholders = ",".join("?" * len(chunk_ids))
            conn.execute(
                f"DELETE FROM l3_chunks WHERE id IN ({placeholders})",
                chunk_ids,
            )
            conn.commit()
        
        for cid in chunk_ids:
            self._vectors_cache.pop(cid, None)
        
        self.log.info(f"L3: deleted {len(chunk_ids)} chunks with tag '{tag}'")
        return {
            "deleted": len(chunk_ids),
            "deleted_chunks": [{"content": item[1]} for item in to_delete],
        }

