"""Knowledge Graph — entity extraction и graph operations.

Извлекает сущности из текста БЕЗ LLM (regex patterns).
Создаёт typed edges между entities.

Типы entities:
- person: имена людей (Capitalized words, русские имена)
- company: названия компаний (LLC, Inc, Corp, ООО, АО)
- concept: ключевые понятия (термины, темы)
- place: географические названия
- project: названия проектов (GitHub repos, apps)

Типы relations:
- works_at: person → company
- founded: person → company
- invested_in: person → company
- attended: person → event/place
- advises: person → company
- related_to: general
"""

import json
import re
import uuid
from datetime import datetime
from typing import Any

from caesar.logging_setup import get_logger
from caesar.memory.storage import Storage


logger = get_logger("kg")


# === ENTITY EXTRACTION PATTERNS ===

# Person names: "Иван Петров", "Alice Smith", "Берн Эрик"
# Русские имена: 2 слова, оба начинаются с заглавной
PERSON_PATTERNS = [
    # Русские имена: "Эрик Берн", "Иван Петров"
    re.compile(r'\b([А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?)\s+([А-ЯЁ][а-яё]+)\b'),
    # English names: "Alice Smith", "Eric Berne"
    re.compile(r'\b([A-Z][a-z]+)\s+([A-Z][a-z]+)\b'),
]

# Company names: "Acme AI", "OpenAI", "Nous Research", "ООО Ромашка"
COMPANY_PATTERNS = [
    # С суффиксами
    re.compile(r'\b([A-Z][A-Za-z0-9]*(?:\s+[A-Z][A-Za-z0-9]*)+)\s+(Inc|LLC|Corp|Ltd|Co|AI|Labs|Research|Group)\b'),
    # Русские юрлица
    re.compile(r'\b(ООО|АО|ПАО|ЗАО|ИП)\s+["«]?([А-ЯЁ][а-яё]+)["»]?\b'),
    # Known companies (camelCase or ALLCAPS)
    re.compile(r'\b(OpenAI|Anthropic|Google|Microsoft|Apple|Meta|Amazon|Nous Research|Acme AI|Hermes)\b'),
]

# Concepts: "агрессия", "поглаживания", "трансактный анализ"
# Существительные 4+ букв, встречающиеся 2+ раза в тексте
CONCEPT_MIN_LENGTH = 5
CONCEPT_MIN_MENTIONS = 2

# Places: "Москва", "San Francisco", "Токио"
PLACE_PATTERNS = [
    re.compile(r'\b(Москва|Санкт-Петербург|Токио|Лондон|Нью-Йорк|San Francisco|Tokyo|London|New York|Berlin|Paris)\b'),
]

# Projects: "hermes-agent", "caesar", GitHub repos
PROJECT_PATTERNS = [
    # GitHub repos: owner/repo
    re.compile(r'\b([a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+)\b'),
    # Known projects
    re.compile(r'\b(hermes-agent|caesar|openclaw|gbrain|llama|whisper|transformers)\b', re.IGNORECASE),
]

# === RELATION PATTERNS ===

# "Alice works at Acme" → works_at
WORKS_AT_PATTERNS = [
    re.compile(r'([А-ЯЁ][а-яё]+)\s+(?:работает|works?\s+at)\s+([A-ZА-ЯЁ][\w\s]+)', re.IGNORECASE),
    re.compile(r'([A-Z][a-z]+)\s+(?:works?\s+at|joined)\s+([A-Z][\w\s]+)', re.IGNORECASE),
]

# "Alice founded Acme" → founded
FOUNDED_PATTERNS = [
    re.compile(r'([А-ЯЁ][а-яё]+|[A-Z][a-z]+)\s+(?:основал|founded|co-founded)\s+([A-ZА-ЯЁ][\w\s]+)', re.IGNORECASE),
]

# "Alice invested in Acme" → invested_in
INVESTED_PATTERNS = [
    re.compile(r'([А-ЯЁ][а-яё]+|[A-Z][a-z]+)\s+(?:инвестировал|invested\s+in)\s+([A-ZА-ЯЁ][\w\s]+)', re.IGNORECASE),
]


def extract_entities(text: str) -> list[dict]:
    """Извлечь сущности из текста БЕЗ LLM.
    
    Возвращает список:
    [{name, entity_type, positions: [start, end]}, ...]
    """
    entities = []
    seen_names = set()
    
    def add_entity(name: str, entity_type: str):
        name = name.strip()
        if not name or len(name) < 2:
            return
        # Нормализуем — убираем лишние пробелы
        name = re.sub(r'\s+', ' ', name)
        key = (name.lower(), entity_type)
        if key in seen_names:
            return
        seen_names.add(key)
        entities.append({"name": name, "entity_type": entity_type})
    
    # Persons
    for pattern in PERSON_PATTERNS:
        for m in pattern.finditer(text):
            full_name = f"{m.group(1)} {m.group(2)}"
            # Фильтр: не "The This", "And Then" etc.
            if full_name.lower() not in ("the this", "and then", "this is", "it is", "we are", "they are"):
                add_entity(full_name, "person")
    
    # Companies
    for pattern in COMPANY_PATTERNS:
        for m in pattern.finditer(text):
            if m.lastindex >= 2:
                name = f"{m.group(1)} {m.group(2)}" if not m.group(0).startswith(("ООО", "АО", "ПАО", "ЗАО", "ИП")) else m.group(2)
            else:
                name = m.group(1)
            add_entity(name, "company")
    
    # Places
    for pattern in PLACE_PATTERNS:
        for m in pattern.finditer(text):
            add_entity(m.group(1), "place")
    
    # Projects
    for pattern in PROJECT_PATTERNS:
        for m in pattern.finditer(text):
            name = m.group(1)
            # Фильтруем false positives (URLs, paths)
            if "/" not in name or re.match(r'^[a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+$', name):
                add_entity(name.lower() if "/" in name else name, "project")
    
    # Concepts — слова 5+ букв, встречающиеся 2+ раза
    words = re.findall(r'\b[А-ЯЁа-яё]{5,}\b', text)
    word_counts = {}
    for w in words:
        w_lower = w.lower()
        word_counts[w_lower] = word_counts.get(w_lower, 0) + 1
    
    for word, count in word_counts.items():
        if count >= CONCEPT_MIN_MENTIONS:
            # Проверяем что не stop-word
            stop_words = {"потому", "который", "которая", "которые", "это", "этот", "этом", "тогда", "чтобы", "может", "если", "через", "только", "будет", "было", "есть"}
            if word not in stop_words:
                add_entity(word, "concept")
    
    return entities


def extract_relations(text: str, entities: list[dict]) -> list[dict]:
    """Извлечь отношения между сущностями из текста.
    
    Возвращает список:
    [{from_entity, to_entity, relation_type}, ...]
    """
    relations = []
    entity_names = {e["name"].lower(): e["name"] for e in entities}
    
    def find_entity_in_text(text_fragment: str) -> str | None:
        text_lower = text_fragment.lower()
        for name_lower, name_orig in entity_names.items():
            if name_lower in text_lower:
                return name_orig
        return None
    
    # works_at
    for pattern in WORKS_AT_PATTERNS:
        for m in pattern.finditer(text):
            from_name = m.group(1).strip()
            to_name = m.group(2).strip().rstrip('.,;')
            # Проверяем что это известные entities
            from_match = find_entity_in_text(from_name)
            to_match = find_entity_in_text(to_name)
            if from_match and to_match:
                relations.append({
                    "from_entity": from_match,
                    "to_entity": to_match,
                    "relation_type": "works_at",
                })
    
    # founded
    for pattern in FOUNDED_PATTERNS:
        for m in pattern.finditer(text):
            from_name = m.group(1).strip()
            to_name = m.group(2).strip().rstrip('.,;')
            from_match = find_entity_in_text(from_name)
            to_match = find_entity_in_text(to_name)
            if from_match and to_match:
                relations.append({
                    "from_entity": from_match,
                    "to_entity": to_match,
                    "relation_type": "founded",
                })
    
    # invested_in
    for pattern in INVESTED_PATTERNS:
        for m in pattern.finditer(text):
            from_name = m.group(1).strip()
            to_name = m.group(2).strip().rstrip('.,;')
            from_match = find_entity_in_text(from_name)
            to_match = find_entity_in_text(to_name)
            if from_match and to_match:
                relations.append({
                    "from_entity": from_match,
                    "to_entity": to_match,
                    "relation_type": "invested_in",
                })
    
    # Если есть person и company в одном тексте но без явной связи — related_to
    persons = [e for e in entities if e["entity_type"] == "person"]
    companies = [e for e in entities if e["entity_type"] == "company"]
    for p in persons:
        for c in companies:
            # Проверяем что они упоминаются рядом (в пределах 100 символов)
            p_pos = text.find(p["name"])
            c_pos = text.find(c["name"])
            if p_pos >= 0 and c_pos >= 0 and abs(p_pos - c_pos) < 200:
                # Проверяем что ещё не добавили
                already = any(
                    r["from_entity"] == p["name"] and r["to_entity"] == c["name"]
                    for r in relations
                )
                if not already:
                    relations.append({
                        "from_entity": p["name"],
                        "to_entity": c["name"],
                        "relation_type": "related_to",
                    })
    
    return relations


class KnowledgeGraph:
    """Knowledge Graph — хранит entities и relations, ищет по графу."""
    
    def __init__(self, storage: Storage):
        self.storage = storage
        self.log = get_logger("kg")
    
    def process_text(
        self,
        text: str,
        user_id: str,
        source_chunk_id: str | None = None,
    ) -> dict:
        """Извлечь entities и relations из текста, сохранить в БД.
        
        Возвращает:
        {
            "entities_found": N,
            "entities_new": N,
            "relations_found": N,
            "relations_new": N,
        }
        """
        if not text or len(text) < 20:
            return {"entities_found": 0, "entities_new": 0, "relations_found": 0, "relations_new": 0}
        
        # Извлекаем
        entities = extract_entities(text)
        relations = extract_relations(text, entities)
        
        # Сохраняем entities
        new_entities = 0
        for ent in entities:
            try:
                created = self._upsert_entity(user_id, ent, source_chunk_id)
                if created:
                    new_entities += 1
            except Exception as e:
                self.log.debug(f"Failed to save entity {ent['name']}: {e}")
        
        # Сохраняем relations
        new_relations = 0
        for rel in relations:
            try:
                created = self._add_relation(user_id, rel, source_chunk_id)
                if created:
                    new_relations += 1
            except Exception as e:
                self.log.debug(f"Failed to save relation: {e}")
        
        if entities or relations:
            self.log.info(
                f"KG: extracted {len(entities)} entities ({new_entities} new), "
                f"{len(relations)} relations ({new_relations} new)"
            )
        
        return {
            "entities_found": len(entities),
            "entities_new": new_entities,
            "relations_found": len(relations),
            "relations_new": new_relations,
        }
    
    def _upsert_entity(self, user_id: str, entity: dict, source_chunk_id: str | None) -> bool:
        """Сохранить или обновить entity. Возвращает True если создан новый."""
        name = entity["name"]
        entity_type = entity["entity_type"]
        
        with self.storage._conn() as conn:
            # Проверяем существует ли
            row = conn.execute(
                "SELECT id, mention_count, metadata FROM kg_entities WHERE user_id = ? AND name = ?",
                (user_id, name),
            ).fetchone()
            
            if row:
                # Обновляем — увеличиваем mention_count, обновляем last_seen
                old_meta = json.loads(row["metadata"] or "{}")
                sources = old_meta.get("source_chunks", [])
                if source_chunk_id and source_chunk_id not in sources:
                    sources.append(source_chunk_id)
                    if len(sources) > 50:
                        sources = sources[-50:]  # ограничиваем
                old_meta["source_chunks"] = sources
                
                conn.execute(
                    """UPDATE kg_entities 
                       SET mention_count = mention_count + 1,
                           last_seen = CURRENT_TIMESTAMP,
                           metadata = ?
                       WHERE id = ?""",
                    (json.dumps(old_meta, ensure_ascii=False), row["id"]),
                )
                conn.commit()
                return False
            else:
                # Создаём новый
                entity_id = f"ent-{uuid.uuid4().hex[:12]}"
                meta = {"source_chunks": [source_chunk_id] if source_chunk_id else []}
                conn.execute(
                    """INSERT INTO kg_entities (id, user_id, name, entity_type, metadata)
                       VALUES (?, ?, ?, ?, ?)""",
                    (entity_id, user_id, name, entity_type, json.dumps(meta, ensure_ascii=False)),
                )
                conn.commit()
                return True
    
    def _add_relation(self, user_id: str, rel: dict, source_chunk_id: str | None) -> bool:
        """Добавить relation. Возвращает True если создан новый."""
        # Проверяем существует ли уже такая связь
        with self.storage._conn() as conn:
            row = conn.execute(
                """SELECT id FROM kg_relations 
                   WHERE user_id = ? AND from_entity = ? AND to_entity = ? AND relation_type = ?""",
                (user_id, rel["from_entity"], rel["to_entity"], rel["relation_type"]),
            ).fetchone()
            
            if row:
                return False
            
            rel_id = f"rel-{uuid.uuid4().hex[:12]}"
            conn.execute(
                """INSERT INTO kg_relations (id, user_id, from_entity, to_entity, relation_type, source_chunk_id)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (rel_id, user_id, rel["from_entity"], rel["to_entity"], rel["relation_type"], source_chunk_id),
            )
            conn.commit()
            return True
    
    def search_entities(self, user_id: str, query: str, limit: int = 10) -> list[dict]:
        """Найти entities по имени (partial match)."""
        with self.storage._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM kg_entities
                   WHERE user_id = ? AND name LIKE ?
                   ORDER BY mention_count DESC, last_seen DESC LIMIT ?""",
                (user_id, f"%{query}%", limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def extract_entities(self, text: str) -> list[dict]:
        """Извлечь entities из текста (делегирует в модульную функцию).

        Обёртка для удобства вызова через инстанс: kg.extract_entities(query).
        Используется в L3 search для KG-boost ранжирования: чанки, в контенте
        которых упоминается entity из запроса, получают +15% к score.
        """
        return extract_entities(text)
    
    def get_relations(self, user_id: str, entity_name: str, direction: str = "both") -> list[dict]:
        """Получить связи entity.
        
        direction: 'outgoing' (from_entity), 'incoming' (to_entity), 'both'
        """
        with self.storage._conn() as conn:
            if direction == "outgoing":
                rows = conn.execute(
                    """SELECT * FROM kg_relations 
                       WHERE user_id = ? AND from_entity = ?""",
                    (user_id, entity_name),
                ).fetchall()
            elif direction == "incoming":
                rows = conn.execute(
                    """SELECT * FROM kg_relations 
                       WHERE user_id = ? AND to_entity = ?""",
                    (user_id, entity_name),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT * FROM kg_relations 
                       WHERE user_id = ? AND (from_entity = ? OR to_entity = ?)""",
                    (user_id, entity_name, entity_name),
                ).fetchall()
        return [dict(r) for r in rows]
    
    def traverse_graph(self, user_id: str, entity_name: str, depth: int = 2) -> dict:
        """Обход графа от entity на depth уровней.
        
        Возвращает:
        {
            "start": entity_name,
            "nodes": [{name, type, distance}, ...],
            "edges": [{from, to, type}, ...],
        }
        """
        visited = set()
        nodes = []
        edges = []
        frontier = [(entity_name, 0)]
        
        while frontier:
            current, dist = frontier.pop(0)
            if current in visited or dist > depth:
                continue
            visited.add(current)
            
            # Получаем entity info
            ents = self.search_entities(user_id, current, limit=1)
            ent_type = ents[0]["entity_type"] if ents else "unknown"
            nodes.append({"name": current, "type": ent_type, "distance": dist})
            
            # Получаем связи
            rels = self.get_relations(user_id, current, "both")
            for rel in rels:
                other = rel["to_entity"] if rel["from_entity"] == current else rel["from_entity"]
                edges.append({
                    "from": rel["from_entity"],
                    "to": rel["to_entity"],
                    "type": rel["relation_type"],
                })
                if other not in visited:
                    frontier.append((other, dist + 1))
        
        return {"start": entity_name, "nodes": nodes, "edges": edges}
    
    def get_stale_entities(self, user_id: str, days: int = 30) -> list[dict]:
        """Получить entities которые не упоминались > days дней."""
        with self.storage._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM kg_entities 
                   WHERE user_id = ? AND last_seen < datetime('now', ?)
                   ORDER BY last_seen ASC""",
                (user_id, f"-{days} days"),
            ).fetchall()
        return [dict(r) for r in rows]
    
    def get_stats(self, user_id: str) -> dict:
        """Статистика knowledge graph для пользователя."""
        with self.storage._conn() as conn:
            ent_count = conn.execute(
                "SELECT COUNT(*) as cnt FROM kg_entities WHERE user_id = ?", (user_id,)
            ).fetchone()
            rel_count = conn.execute(
                "SELECT COUNT(*) as cnt FROM kg_relations WHERE user_id = ?", (user_id,)
            ).fetchone()
            
            type_counts = conn.execute(
                """SELECT entity_type, COUNT(*) as cnt FROM kg_entities 
                   WHERE user_id = ? GROUP BY entity_type""",
                (user_id,),
            ).fetchall()
            
            return {
                "total_entities": ent_count["cnt"] if ent_count else 0,
                "total_relations": rel_count["cnt"] if rel_count else 0,
                "by_type": {r["entity_type"]: r["cnt"] for r in type_counts},
            }
