"""Dream Cycle — ночной цикл обработки памяти.

Запускается по cron (обычно в 2:00 ночи). Пока пользователь спит:

Phase 1: Entity Sweep — найти новые сущности в дневных L3 чанках
Phase 2: Enrich — дополнить тонкие entity pages через cheap LLM
Phase 3: Consolidate — объединить дубликаты entities
Phase 4: Fix citations — проверить metadata чанков
Phase 5: Report — отправить утренний дайджест

Логирование: что добавлено, enriched, объединено.
Уведомление: отправляется как morning briefing (если включен).
"""

import asyncio
import hashlib
import json
import uuid
from datetime import datetime, timedelta
from typing import Any

from caesar.config import Config
from caesar.core.events import EventBus, Event, info_notification
from caesar.logging_setup import get_logger
from caesar.memory.storage import Storage
from caesar.memory.knowledge_graph import KnowledgeGraph
from caesar.memory.l3 import L3Memory


class DreamCycle:
    """Ночной цикл обработки памяти."""
    
    def __init__(
        self,
        config: Config,
        storage: Storage,
        kg: KnowledgeGraph,
        llm_router=None,
        event_bus: EventBus | None = None,
        l3_memory: L3Memory | None = None,
    ):
        self.config = config
        self.storage = storage
        self.kg = kg
        self.llm = llm_router
        self.event_bus = event_bus
        self.l3 = l3_memory
        self.log = get_logger("dream")
    
    async def run(
        self,
        user_id: str = "",
        channel_id: str = "",
        **kwargs,
    ) -> dict:
        """Запустить полный dream cycle.
        
        Параметры (kwargs):
            force_topic_consolidation: bool — запустить только topic consolidation
                (для команды 'проиндексируй память')
            force_all: bool — консолидировать ВСЕ чанки, не только за 24 часа
        
        Возвращает отчёт:
        {
            "entities_extracted": N,
            "entities_enriched": N,
            "duplicates_merged": N,
            "chunks_processed": N,
            "citations_fixed": N,
            "topics_consolidated": N,
            "chunks_created": N,
            "duration_sec": float,
        }
        """
        start_time = datetime.now()
        self.log.info(f"🌙 Dream cycle started for user={user_id or 'all'}")
        
        report = {
            "entities_extracted": 0,
            "entities_enriched": 0,
            "duplicates_merged": 0,
            "chunks_processed": 0,
            "citations_fixed": 0,
            "topics_consolidated": 0,
            "chunks_created": 0,
            "stale_entities": 0,
            "duration_sec": 0.0,
        }
        
        # Дополнительные параметры для запуска по требованию
        force_topic = kwargs.get("force_topic_consolidation", False) if kwargs else False
        force_all = kwargs.get("force_all", False) if kwargs else False
        
        # Phase 1: Entity Sweep
        try:
            p1 = await self._phase_entity_sweep(user_id)
            report["entities_extracted"] = p1
            report["chunks_processed"] += p1  # approx
        except Exception as e:
            self.log.error(f"Phase 1 (entity sweep) failed: {e}")
        
        # Phase 2: Enrich
        try:
            p2 = await self._phase_enrich(user_id)
            report["entities_enriched"] = p2
        except Exception as e:
            self.log.error(f"Phase 2 (enrich) failed: {e}")
        
        # Phase 3: Consolidate
        try:
            p3 = await self._phase_consolidate(user_id)
            report["duplicates_merged"] = p3
        except Exception as e:
            self.log.error(f"Phase 3 (consolidate) failed: {e}")
        
        # Phase 4: Fix citations (пропускаем если force_topic — пользователь
        # явно попросил только консолидацию)
        if not force_topic:
            try:
                p4 = await self._phase_fix_citations(user_id)
                report["citations_fixed"] = p4
            except Exception as e:
                self.log.error(f"Phase 4 (fix citations) failed: {e}")
        
        # Phase 5: Topic consolidation — собираем consolidated summaries
        # по топикам из чанков за последние 24 часа (или всех, если force_all).
        try:
            p5 = await self._phase_topic_consolidation(user_id, force_all=force_all)
            report["topics_consolidated"] = p5["topics"]
            report["chunks_created"] = p5["chunks_created"]
            report["chunks_processed"] += p5["chunks_processed"]
        except Exception as e:
            self.log.error(f"Phase 5 (topic consolidation) failed: {e}")
        
        # Phase 6: Stale entities flagging — помечаем entities которые
        # не упоминались > 30 дней. Эти entities потом подсвечиваются
        # в gap analysis как "возможно устаревшие".
        if not force_topic:
            try:
                p6 = await self._phase_stale_entities(user_id)
                report["stale_entities"] = p6
            except Exception as e:
                self.log.error(f"Phase 6 (stale entities) failed: {e}")
        else:
            report["stale_entities"] = 0

        # Phase 7 (T2): Mind Mirror — регенерируем Markdown-проекцию памяти (auto/).
        # После всех consolidate-фаз — факты/сущности свежие. manual/ не трогает.
        try:
            from caesar.memory.mind_mirror import MindMirror
            mirror = MindMirror(self.storage, kg=getattr(self, "kg", None))
            m = mirror.export()
            report["mind_mirror"] = m
            self.log.info(f"Phase 7 (mind mirror): facts={m['facts']} "
                          f"entities={m['entities']} relations={m['relations']}")
        except Exception as e:
            self.log.error(f"Phase 7 (mind mirror) failed: {e}")

        report["duration_sec"] = (datetime.now() - start_time).total_seconds()
        
        self.log.info(
            f"🌙 Dream cycle complete: "
            f"{report['entities_extracted']} extracted, "
            f"{report['entities_enriched']} enriched, "
            f"{report['duplicates_merged']} merged, "
            f"{report['citations_fixed']} fixed, "
            f"{report['topics_consolidated']} topics consolidated, "
            f"{report['chunks_created']} consolidated chunks created, "
            f"{report['stale_entities']} stale entities flagged, "
            f"{report['duration_sec']:.1f}s"
        )
        
        return report
    
    async def _phase_entity_sweep(self, user_id: str) -> int:
        """Phase 1: Найти новые сущности в чанках за последние 24 часа.
        
        Проходим по всем L3 чанкам за сегодня и извлекаем entities.
        """
        self.log.info("Phase 1: Entity sweep — extracting from recent chunks")
        
        cutoff = datetime.now() - timedelta(hours=24)
        
        with self.storage._conn() as conn:
            if user_id:
                rows = conn.execute(
                    """SELECT id, user_id, content, chunk_metadata 
                       FROM l3_chunks 
                       WHERE user_id = ? AND rowid > 
                       (SELECT COALESCE(MAX(rowid), 0) FROM l3_chunks WHERE chunk_metadata LIKE '%dream_processed%')
                       ORDER BY rowid ASC LIMIT 500""",
                    (user_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT id, user_id, content, chunk_metadata 
                       FROM l3_chunks 
                       WHERE rowid > 
                       (SELECT COALESCE(MAX(rowid), 0) FROM l3_chunks WHERE chunk_metadata LIKE '%dream_processed%')
                       ORDER BY rowid ASC LIMIT 500""",
                ).fetchall()
        
        if not rows:
            self.log.info("  No new chunks to process")
            return 0
        
        total_entities = 0
        for row in rows:
            d = dict(row)
            try:
                # Извлекаем entities
                result = self.kg.process_text(
                    text=d["content"],
                    user_id=d["user_id"],
                    source_chunk_id=d["id"],
                )
                total_entities += result["entities_new"]
                
                # Помечаем чанк как обработанный
                meta = json.loads(d.get("chunk_metadata") or "{}")
                meta["dream_processed"] = datetime.now().isoformat()
                with self.storage._conn() as conn:
                    conn.execute(
                        "UPDATE l3_chunks SET chunk_metadata = ? WHERE id = ?",
                        (json.dumps(meta, ensure_ascii=False), d["id"]),
                    )
                    conn.commit()
            except Exception as e:
                self.log.debug(f"Failed to process chunk {d['id']}: {e}")
        
        self.log.info(f"  Processed {len(rows)} chunks, found {total_entities} new entities")
        return total_entities
    
    async def _phase_enrich(self, user_id: str) -> int:
        """Phase 2: Дополнить тонкие entity pages.
        
        Entity с mention_count=1 и без relations — "тонкий".
        Просим cheap LLM сгенерировать краткое описание.
        """
        self.log.info("Phase 2: Enriching thin entities")
        
        if not self.llm or not self.llm.cheap.api_key:
            self.log.info("  No cheap LLM configured, skipping enrichment")
            return 0
        
        # Находим тонкие entities (mention_count=1, type != concept)
        with self.storage._conn() as conn:
            if user_id:
                rows = conn.execute(
                    """SELECT * FROM kg_entities 
                       WHERE user_id = ? AND mention_count = 1 
                       AND entity_type IN ('person', 'company', 'project')
                       AND metadata NOT LIKE '%enriched%'
                       LIMIT 20""",
                    (user_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT * FROM kg_entities 
                       WHERE mention_count = 1 
                       AND entity_type IN ('person', 'company', 'project')
                       AND metadata NOT LIKE '%enriched%'
                       LIMIT 20""",
                ).fetchall()
        
        if not rows:
            self.log.info("  No thin entities to enrich")
            return 0
        
        from caesar.core.llm import LLMMessage
        
        enriched = 0
        for row in rows:
            d = dict(row)
            try:
                # Просим cheap LLM сгенерировать описание
                prompt = (
                    f"Кратко опиши {d['entity_type']} '{d['name']}' в 1-2 предложениях. "
                    f"Если это известная персона/компания — дай ключевые факты. "
                    f"Если неизвестно — напиши 'неизвестно'."
                )
                
                resp = await self.llm.cheap_chat(
                    messages=[LLMMessage(role="user", content=prompt)],
                    temperature=0.3,
                    max_tokens=200,
                )
                
                description = resp.content.strip()
                if description and description.lower() != "неизвестно":
                    # Сохраняем в metadata
                    meta = json.loads(d.get("metadata") or "{}")
                    meta["description"] = description
                    meta["enriched"] = datetime.now().isoformat()
                    
                    with self.storage._conn() as conn:
                        conn.execute(
                            "UPDATE kg_entities SET metadata = ? WHERE id = ?",
                            (json.dumps(meta, ensure_ascii=False), d["id"]),
                        )
                        conn.commit()
                    
                    enriched += 1
                    self.log.info(f"  Enriched: {d['name']} ({d['entity_type']})")
                
                # Небольшая пауза чтобы не перегружать API
                await asyncio.sleep(0.5)
                
            except Exception as e:
                self.log.debug(f"Failed to enrich {d['name']}: {e}")
        
        self.log.info(f"  Enriched {enriched}/{len(rows)} entities")
        return enriched
    
    async def _phase_consolidate(self, user_id: str) -> int:
        """Phase 3: Объединить дубликаты entities.
        
        Ищем entities с похожими именами (case-insensitive, different endings).
        """
        self.log.info("Phase 3: Consolidating duplicate entities")
        
        with self.storage._conn() as conn:
            if user_id:
                rows = conn.execute(
                    """SELECT name, entity_type, COUNT(*) as cnt, 
                              GROUP_CONCAT(id) as ids, 
                              SUM(mention_count) as total_mentions
                       FROM kg_entities 
                       WHERE user_id = ?
                       GROUP BY LOWER(name), entity_type
                       HAVING cnt > 1
                       LIMIT 50""",
                    (user_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT name, entity_type, COUNT(*) as cnt,
                              GROUP_CONCAT(id) as ids,
                              SUM(mention_count) as total_mentions
                       FROM kg_entities
                       GROUP BY LOWER(name), entity_type
                       HAVING cnt > 1
                       LIMIT 50""",
                ).fetchall()
        
        if not rows:
            self.log.info("  No duplicates found")
            return 0
        
        merged = 0
        for row in rows:
            d = dict(row)
            ids = d["ids"].split(",")
            
            # Оставляем первый, удаляем остальные
            keep_id = ids[0]
            delete_ids = ids[1:]
            
            # Переносим relations на оставшийся
            with self.storage._conn() as conn:
                for did in delete_ids:
                    # Получаем name удаляемого
                    del_row = conn.execute(
                        "SELECT name FROM kg_entities WHERE id = ?", (did,)
                    ).fetchone()
                    if del_row:
                        del_name = del_row["name"]
                        # Переносим relations на оставшийся entity.
                        # Если user_id задан — только этого пользователя;
                        # иначе (ночной all-users dream) — ВСЕХ, иначе
                        # `user_id or "%"` с оператором `=` не матчит ни одну
                        # строку (user_id — реальный id, а не литерал "%"),
                        # и relations остаются dangling на удалённом entity.
                        if user_id:
                            conn.execute(
                                "UPDATE kg_relations SET from_entity = ? WHERE from_entity = ? AND user_id = ?",
                                (d["name"], del_name, user_id),
                            )
                            conn.execute(
                                "UPDATE kg_relations SET to_entity = ? WHERE to_entity = ? AND user_id = ?",
                                (d["name"], del_name, user_id),
                            )
                        else:
                            conn.execute(
                                "UPDATE kg_relations SET from_entity = ? WHERE from_entity = ?",
                                (d["name"], del_name),
                            )
                            conn.execute(
                                "UPDATE kg_relations SET to_entity = ? WHERE to_entity = ?",
                                (d["name"], del_name),
                            )
                    
                    conn.execute("DELETE FROM kg_entities WHERE id = ?", (did,))
                
                # Обновляем mention_count
                conn.execute(
                    "UPDATE kg_entities SET mention_count = ? WHERE id = ?",
                    (d["total_mentions"], keep_id),
                )
                conn.commit()
            
            merged += len(delete_ids)
        
        self.log.info(f"  Merged {merged} duplicate entities")
        return merged
    
    async def _phase_fix_citations(self, user_id: str) -> int:
        """Phase 4: Проверить metadata чанков.
        
        Ищем чанки с битым metadata (нет embedding, нет hash).
        """
        self.log.info("Phase 4: Fixing citations")
        
        fixed = 0
        
        with self.storage._conn() as conn:
            # Чанки без hash или с фейковым hash ('needs_recalc' от старой версии)
            rows = conn.execute(
                """SELECT id, chunk_metadata, content FROM l3_chunks
                   WHERE chunk_metadata NOT LIKE '%hash%'
                      OR chunk_metadata LIKE '%needs_recalc%'
                   LIMIT 100""",
            ).fetchall()

            for row in rows:
                d = dict(row)
                try:
                    import hashlib
                    meta = json.loads(d.get("chunk_metadata") or "{}")

                    # Реальный hash из content чанка (раньше писали 'needs_recalc' — no-op)
                    content = d.get("content") or ""
                    meta["hash"] = hashlib.sha256(content.encode()).hexdigest()[:16]
                    with self.storage._conn() as conn2:
                        conn2.execute(
                            "UPDATE l3_chunks SET chunk_metadata = ? WHERE id = ?",
                            (json.dumps(meta, ensure_ascii=False), d["id"]),
                        )
                        conn2.commit()
                    fixed += 1
                except Exception:
                    pass
        
        self.log.info(f"  Fixed {fixed} citations")
        return fixed
    
    async def _phase_topic_consolidation(
        self, user_id: str, force_all: bool = False,
    ) -> dict:
        """Phase 5: Консолидация чанков по топикам.
        
        Алгоритм:
        1. Берём чанки за последние 24 часа (или все, если force_all),
           которые ещё не были консолидированы (нет 'consolidated_in' в metadata).
           ПРОПУСКАЕМ чанки с type='consolidated' (мы сами их создали).
        2. Батчим по 15 чанков.
        3. Для каждого батча просим cheap LLM сгруппировать по темам
           и сгенерировать consolidated summary для каждой темы.
        4. Сохраняем summaries как новые L3 чанки с metadata:
           - type: 'consolidated'
           - topic: 'дизайн лендинга'
           - source_chunks: [chunk_id1, chunk_id2, ...]
           - consolidated_at: ISO timestamp
        5. Помечаем source chunks metadata: consolidated_in = [new_chunk_ids]
        
        Возвращает:
            {"topics": N, "chunks_created": N, "chunks_processed": N}
        """
        self.log.info(
            f"Phase 5: Topic consolidation "
            f"({'ALL chunks' if force_all else 'last 24h'})"
        )
        
        if not self.llm or not self.llm.cheap.api_key:
            self.log.info("  No cheap LLM configured, skipping topic consolidation")
            return {"topics": 0, "chunks_created": 0, "chunks_processed": 0}
        
        if not self.l3:
            self.log.info("  No L3 memory instance, skipping topic consolidation")
            return {"topics": 0, "chunks_created": 0, "chunks_processed": 0}
        
        # 1. Загружаем не-консолидированные чанки
        if force_all:
            cutoff = datetime(2000, 1, 1)
        else:
            cutoff = datetime.now() - timedelta(hours=24)
        
        with self.storage._conn() as conn:
            if user_id:
                rows = conn.execute(
                    """SELECT id, user_id, content, chunk_metadata, created_at
                       FROM l3_chunks
                       WHERE user_id = ?
                       AND created_at >= ?
                       ORDER BY created_at ASC LIMIT 200""",
                    (user_id, cutoff),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT id, user_id, content, chunk_metadata, created_at
                       FROM l3_chunks
                       WHERE created_at >= ?
                       ORDER BY created_at ASC LIMIT 200""",
                    (cutoff,),
                ).fetchall()
        
        # Фильтруем: пропускаем уже консолидированные и сами consolidated-чанки
        candidates = []
        for row in rows:
            d = dict(row)
            try:
                meta = json.loads(d.get("chunk_metadata") or "{}")
            except (json.JSONDecodeError, TypeError):
                meta = {}
            
            # Пропускаем consolidated-чанки (мы их сами создали)
            if meta.get("type") == "consolidated":
                continue
            # Пропускаем уже консолидированные
            if meta.get("consolidated_in"):
                continue
            
            candidates.append({
                "id": d["id"],
                "user_id": d["user_id"],
                "content": d["content"],
                "created_at": d["created_at"],
            })
        
        if not candidates:
            self.log.info("  No new chunks to consolidate")
            return {"topics": 0, "chunks_created": 0, "chunks_processed": 0}
        
        self.log.info(f"  {len(candidates)} chunks to consolidate")
        
        # 2. Батчим по 15 чанков
        BATCH_SIZE = 15
        batches = [
            candidates[i:i + BATCH_SIZE]
            for i in range(0, len(candidates), BATCH_SIZE)
        ]
        
        total_topics = 0
        total_created = 0
        total_processed = 0
        
        from caesar.core.llm import LLMMessage
        
        for batch_idx, batch in enumerate(batches):
            self.log.info(
                f"  Batch {batch_idx + 1}/{len(batches)}: "
                f"{len(batch)} chunks"
            )
            
            # Готовим текст батча для LLM
            chunks_text_parts = []
            for i, ch in enumerate(batch, 1):
                # Урезаем каждый чанк до 400 символов чтобы не раздуть контекст
                content_preview = ch["content"][:400]
                chunks_text_parts.append(f"[{i}] (id={ch['id']})\n{content_preview}")
            chunks_text = "\n\n---\n\n".join(chunks_text_parts)
            
            # 3. Просим LLM сгруппировать и саммаризовать
            prompt = (
                "Ты ассистент-индексатор памяти. Тебе даются фрагменты диалогов "
                "пользователя с ассистентом за определённый период.\n\n"
                "ЗАДАЧА:\n"
                "1. Сгруппируй фрагменты по темам (1 тема = 1 разговорная нить).\n"
                "2. Для каждой темы, в которой БОЛЬШЕ 1 фрагмента, напиши "
                "краткое саммари (3-7 предложений), объединяющее все ключевые "
                "решения, факты и выводы по этой теме.\n"
                "3. Фрагменты, которые не относятся ни к одной теме или являются "
                "trivial (привет/ок/спасибо) — пропусти.\n\n"
                "ФОРМАТ ОТВЕТА — строго JSON (без markdown), массив объектов:\n"
                "[\n"
                "  {\n"
                '    "topic": "короткое название темы (1-4 слова)",\n'
                '    "summary": " consolidated саммари всех обсуждений по этой теме",\n'
                '    "source_indices": [1, 3, 7]\n'
                "  }\n"
                "]\n\n"
                "ВАЖНО:\n"
                "- source_indices — это числа из [N] в начале каждого фрагмента.\n"
                "- Не выдумывай факты — только то, что реально есть в текстах.\n"
                "- Саммари пиши на русском, в утвердительной форме.\n"
                "- Если в батче нет связанных тем — верни пустой массив [].\n\n"
                f"ФРАГМЕНТЫ:\n\n{chunks_text}"
            )
            
            try:
                resp = await self.llm.cheap_chat(
                    messages=[LLMMessage(role="user", content=prompt)],
                    temperature=0.2,
                    max_tokens=2000,
                )
                
                # Парсим JSON (LLM может обернуть в markdown — чистим)
                raw = resp.content.strip()
                # Убираем markdown-обёртку если есть
                if raw.startswith("```"):
                    raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
                    if raw.endswith("```"):
                        raw = raw[:-3]
                    raw = raw.strip()
                # Иногда LLM добавляет текст до/после JSON — берём [...] часть
                first_brace = raw.find("[")
                last_brace = raw.rfind("]")
                if first_brace == -1 or last_brace == -1:
                    self.log.warning(f"  Batch {batch_idx+1}: no JSON array in response")
                    continue
                raw_json = raw[first_brace:last_brace + 1]
                
                topics = json.loads(raw_json)
                if not isinstance(topics, list):
                    topics = []
                
            except json.JSONDecodeError as e:
                self.log.warning(f"  Batch {batch_idx+1}: JSON parse failed: {e}")
                continue
            except Exception as e:
                self.log.warning(f"  Batch {batch_idx+1}: LLM call failed: {e}")
                await asyncio.sleep(1)
                continue
            
            if not topics:
                self.log.info(f"  Batch {batch_idx+1}: no related topics found")
                # Помечаем чанки как processed (consolidated_in=[]) чтобы не
                # повторять обработку каждый раз
                for ch in batch:
                    await self._mark_consolidated(ch["id"], [])
                total_processed += len(batch)
                continue
            
            # 4. Сохраняем каждый topic-summary как новый L3 chunk
            for topic_data in topics:
                topic_name = (topic_data.get("topic") or "").strip()[:100]
                summary = (topic_data.get("summary") or "").strip()
                source_indices = topic_data.get("source_indices") or []
                
                # Валидация
                if not topic_name or not summary or len(summary) < 30:
                    continue
                if not isinstance(source_indices, list) or len(source_indices) < 2:
                    continue
                
                # Мапим indices → chunk IDs
                source_chunk_ids = []
                for idx in source_indices:
                    if isinstance(idx, int) and 1 <= idx <= len(batch):
                        source_chunk_ids.append(batch[idx - 1]["id"])
                
                if len(source_chunk_ids) < 2:
                    continue
                
                # Готовим content consolidated чанка
                # Берём user_id из первого source чанка
                owner_user_id = ""
                for ch in batch:
                    if ch["id"] in source_chunk_ids:
                        owner_user_id = ch["user_id"]
                        break
                if not owner_user_id:
                    continue
                
                consolidated_content = (
                    f"[Consolidated] Тема: {topic_name}\n\n"
                    f"{summary}\n\n"
                    f"(объединяет {len(source_chunk_ids)} фрагмента диалогов)"
                )
                
                try:
                    new_chunk_ids = await self.l3.add(
                        user_id=owner_user_id,
                        channel="main",
                        content=consolidated_content,
                        metadata={
                            "type": "consolidated",
                            "topic": topic_name,
                            "source_chunks": source_chunk_ids,
                            "consolidated_at": datetime.now().isoformat(),
                            "auto_indexed": True,
                        },
                    )
                    
                    if new_chunk_ids:
                        total_created += len(new_chunk_ids)
                        total_topics += 1
                        self.log.info(
                            f"  + topic '{topic_name}': "
                            f"{len(source_chunk_ids)} chunks → 1 consolidated"
                        )
                        
                        # 5. Помечаем source chunks как консолидированные
                        for src_id in source_chunk_ids:
                            await self._mark_consolidated(src_id, new_chunk_ids)
                except Exception as e:
                    self.log.warning(
                        f"  Failed to save consolidated chunk for '{topic_name}': {e}"
                    )
            
            total_processed += len(batch)
            
            # Небольшая пауза чтобы не перегружать API
            await asyncio.sleep(0.5)
        
        self.log.info(
            f"  Topic consolidation done: "
            f"{total_topics} topics, "
            f"{total_created} consolidated chunks created, "
            f"{total_processed} source chunks processed"
        )
        return {
            "topics": total_topics,
            "chunks_created": total_created,
            "chunks_processed": total_processed,
        }
    
    async def _mark_consolidated(self, chunk_id: str, new_chunk_ids: list[str]) -> None:
        """Пометить чанк как консолидированный (добавить consolidated_in в metadata)."""
        try:
            with self.storage._conn() as conn:
                row = conn.execute(
                    "SELECT chunk_metadata FROM l3_chunks WHERE id = ?",
                    (chunk_id,),
                ).fetchone()
                if not row:
                    return
                
                meta = json.loads(row["chunk_metadata"] or "{}")
                existing = meta.get("consolidated_in") or []
                existing.extend(new_chunk_ids)
                meta["consolidated_in"] = existing
                
                conn.execute(
                    "UPDATE l3_chunks SET chunk_metadata = ? WHERE id = ?",
                    (json.dumps(meta, ensure_ascii=False), chunk_id),
                )
                conn.commit()
        except Exception as e:
            self.log.debug(f"Failed to mark chunk {chunk_id} consolidated: {e}")
    
    async def _phase_stale_entities(self, user_id: str) -> int:
        """Phase 6: Пометить stale entities (>30 дней без упоминаний).
        
        Добавляет в metadata entities поле stale=True и stale_since=<timestamp>.
        Это позволяет gap analysis быстрее определять устаревшие сущности
        без повторного вычисления last_seen.
        
        Также снимает пометку stale если entity снова упоминался.
        
        Возвращает количество помеченных как stale.
        """
        self.log.info("Phase 6: Stale entities flagging")
        
        if not self.kg:
            self.log.info("  No KG instance, skipping")
            return 0
        
        flagged = 0
        unflagged = 0
        threshold_days = 30
        
        try:
            # Получаем все entities пользователя (или все, если user_id пустой)
            with self.storage._conn() as conn:
                if user_id:
                    rows = conn.execute(
                        """SELECT id, name, last_seen, metadata FROM kg_entities 
                           WHERE user_id = ?""",
                        (user_id,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT id, name, last_seen, metadata FROM kg_entities"
                    ).fetchall()
            
            now = datetime.now()
            
            for row in rows:
                d = dict(row)
                try:
                    last_seen_str = str(d.get("last_seen") or "")
                    if not last_seen_str:
                        continue
                    
                    # Парсим дату — может быть SQLite TIMESTAMP или ISO
                    last_seen_str_norm = last_seen_str.replace("T", " ").split(".")[0]
                    last_seen = datetime.strptime(last_seen_str_norm, "%Y-%m-%d %H:%M:%S")
                    age_days = (now - last_seen).days
                    
                    meta = json.loads(d.get("metadata") or "{}")
                    
                    if age_days > threshold_days:
                        # Помечаем как stale (если ещё не помечен)
                        if not meta.get("stale"):
                            meta["stale"] = True
                            meta["stale_since"] = now.isoformat()
                            meta["stale_days"] = age_days
                            
                            with self.storage._conn() as conn:
                                conn.execute(
                                    "UPDATE kg_entities SET metadata = ? WHERE id = ?",
                                    (json.dumps(meta, ensure_ascii=False), d["id"]),
                                )
                                conn.commit()
                            flagged += 1
                    else:
                        # Снимаем пометку stale если entity снова активен
                        if meta.get("stale"):
                            meta.pop("stale", None)
                            meta.pop("stale_since", None)
                            meta.pop("stale_days", None)
                            
                            with self.storage._conn() as conn:
                                conn.execute(
                                    "UPDATE kg_entities SET metadata = ? WHERE id = ?",
                                    (json.dumps(meta, ensure_ascii=False), d["id"]),
                                )
                                conn.commit()
                            unflagged += 1
                except (ValueError, TypeError) as e:
                    self.log.debug(f"Failed to process entity {d.get('name')}: {e}")
                    continue
        
        except Exception as e:
            self.log.error(f"Phase 6 failed: {e}")
            return 0
        
        self.log.info(
            f"  Stale flagging: {flagged} marked stale, {unflagged} un-marked"
        )
        return flagged
    
    def format_report(self, report: dict) -> str:
        """Форматировать отчёт для отправки пользователю."""
        lines = ["🌙 Dream Cycle завершён.\n"]
        
        if report["entities_extracted"] > 0:
            lines.append(f"  Новых сущностей: {report['entities_extracted']}")
        if report["entities_enriched"] > 0:
            lines.append(f"  Дополнено: {report['entities_enriched']}")
        if report["duplicates_merged"] > 0:
            lines.append(f"  Дубликатов объединено: {report['duplicates_merged']}")
        if report["citations_fixed"] > 0:
            lines.append(f"  Цитат исправлено: {report['citations_fixed']}")
        if report.get("topics_consolidated", 0) > 0:
            lines.append(f"  Тем консолидировано: {report['topics_consolidated']}")
            lines.append(f"  Consolidated чанков создано: {report['chunks_created']}")
        if report.get("stale_entities", 0) > 0:
            lines.append(f"  Устаревших сущностей: {report['stale_entities']}")
        
        if len(lines) == 1:
            lines.append("  Ничего нового за ночь.")
        
        lines.append(f"\n  Время: {report['duration_sec']:.1f} сек")
        
        return "\n".join(lines)
