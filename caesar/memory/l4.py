"""L4 — процедурная память (скиллы).

См. roadmap раздел 6.10, 6.11.

Скилл = YAML-файл с:
- name, trigger
- prerequisites — что спросить у пользователя
- exact_recipe — дословные команды (выполняет код, не LLM)
- anti_patterns — ошибки, которые уже были
- pitfalls — подводные камни
- example, notes
- version, success_count, failure_count
"""

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from caesar.config import SKILLS_DIR
from caesar.logging_setup import get_logger
from caesar.memory.storage import Storage


@dataclass
class Skill:
    name: str
    trigger: str
    prerequisites: list[str] = field(default_factory=list)
    exact_recipe: list[dict] = field(default_factory=list)
    anti_patterns: list[dict] = field(default_factory=list)
    pitfalls: list[str] = field(default_factory=list)
    example: str = ""
    notes: str = ""
    version: int = 1
    created_at: str = ""
    last_success: str = ""
    success_count: int = 0
    failure_count: int = 0
    needs_validation: bool = False
    broken: bool = False
    yaml_path: str = ""
    
    @classmethod
    def from_dict(cls, d: dict) -> "Skill":
        return cls(
            name=d["name"],
            trigger=d.get("trigger", ""),
            prerequisites=d.get("prerequisites", []),
            exact_recipe=d.get("exact_recipe", []),
            anti_patterns=d.get("anti_patterns", []),
            pitfalls=d.get("pitfalls", []),
            example=d.get("example", ""),
            notes=d.get("notes", ""),
            version=d.get("version", 1),
            created_at=d.get("created_at", ""),
            last_success=d.get("last_success", ""),
            success_count=d.get("success_count", 0),
            failure_count=d.get("failure_count", 0),
            needs_validation=d.get("needs_validation", False),
            broken=d.get("broken", False),
            yaml_path=d.get("yaml_path", ""),
        )
    
    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "trigger": self.trigger,
            "prerequisites": self.prerequisites,
            "exact_recipe": self.exact_recipe,
            "anti_patterns": self.anti_patterns,
            "pitfalls": self.pitfalls,
            "example": self.example,
            "notes": self.notes,
            "version": self.version,
            "created_at": self.created_at,
            "last_success": self.last_success,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "needs_validation": self.needs_validation,
            "broken": self.broken,
        }
    
    def to_yaml(self) -> str:
        return yaml.dump(self.to_dict(), allow_unicode=True, default_flow_style=False, sort_keys=False)


class L4Skills:
    """Управление скиллами.
    
    Скиллы хранятся:
    - YAML-файлы в SKILLS_DIR/ (читаемые человеком)
    - SQLite-таблица l4_skills (для быстрого поиска)
    
    Синхронизация: при изменении YAML → обновляем SQLite.
    """
    
    def __init__(self, storage: Storage, skills_dir: Path = SKILLS_DIR):
        self.storage = storage
        self.skills_dir = skills_dir
        self.log = get_logger("l4")
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        self._sync_from_yaml()
    
    def _sync_from_yaml(self) -> None:
        """Загрузить все YAML-файлы в SQLite."""
        count = 0
        for yaml_file in self.skills_dir.glob("*.yaml"):
            try:
                with open(yaml_file, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f)
                if not data or "name" not in data:
                    continue
                data["yaml_path"] = str(yaml_file)
                self.storage.upsert_skill(data)
                count += 1
            except Exception as e:
                self.log.warning(f"Cannot load skill {yaml_file}: {e}")
        self.log.info(f"Loaded {count} skills from YAML")
    
    def save_skill(self, skill: Skill, write_yaml: bool = True) -> None:
        """Сохранить скилл в SQLite и (опционально) в YAML."""
        data = skill.to_dict()
        data["yaml_path"] = skill.yaml_path
        self.storage.upsert_skill(data)
        
        if write_yaml:
            yaml_path = Path(skill.yaml_path) if skill.yaml_path else self.skills_dir / f"{skill.name}.yaml"
            yaml_path.parent.mkdir(parents=True, exist_ok=True)
            with open(yaml_path, "w", encoding="utf-8") as f:
                f.write(skill.to_yaml())
            skill.yaml_path = str(yaml_path)
    
    def get_skill(self, name: str) -> Skill | None:
        data = self.storage.get_skill(name)
        if not data:
            return None
        return Skill.from_dict(data)
    
    def list_skills(self, only_enabled: bool = False) -> list[dict]:
        return self.storage.list_skills(only_enabled)
    
    async def find_skill(
        self,
        query: str,
        confidence_threshold: str = "medium",
    ) -> list[dict]:
        """Найти подходящие скиллы по описанию.
        
        V1: простой matching по trigger через includes.
        V2: через embeddings trigger-ов.
        """
        query_lower = query.lower()
        all_skills = self.list_skills(only_enabled=True)
        results: list[dict] = []
        for skill in all_skills:
            trigger_lower = skill["trigger"].lower()
            # Простой matching: если query содержит ключевые слова из trigger
            trigger_words = [w for w in trigger_lower.split() if len(w) > 3]
            if not trigger_words:
                continue
            matches = sum(1 for w in trigger_words if w in query_lower)
            if matches == 0:
                continue
            confidence = "high" if matches >= len(trigger_words) * 0.7 else (
                "medium" if matches >= len(trigger_words) * 0.4 else "low"
            )
            if confidence_threshold == "high" and confidence != "high":
                continue
            if confidence_threshold == "medium" and confidence == "low":
                continue
            results.append({
                **skill,
                "confidence": confidence,
                "matches": matches,
            })
        # Сортируем по confidence
        conf_order = {"high": 0, "medium": 1, "low": 2}
        results.sort(key=lambda x: (conf_order.get(x["confidence"], 3), -x["matches"]))
        return results
    
    def record_success(self, name: str) -> None:
        """Отметить успешное применение скилла."""
        with self.storage._conn() as conn:
            conn.execute("""
                UPDATE l4_skills SET 
                    success_count = success_count + 1,
                    last_success = CURRENT_TIMESTAMP,
                    consecutive_failures = 0,
                    needs_validation = 0
                WHERE name = ?
            """, (name,))
    
    def record_failure(self, name: str, error: str) -> None:
        """Отметить неудачу."""
        with self.storage._conn() as conn:
            conn.execute("""
                UPDATE l4_skills SET 
                    failure_count = failure_count + 1
                WHERE name = ?
            """, (name,))
            # Если 3 неудачи подряд — помечаем broken
            row = conn.execute(
                "SELECT failure_count, success_count FROM l4_skills WHERE name = ?", (name,)
            ).fetchone()
            if row and row["failure_count"] >= 3:
                conn.execute("UPDATE l4_skills SET broken = 1 WHERE name = ?", (name,))
    
    def add_anti_pattern(self, name: str, error: str, source_msg_id: str | None = None) -> None:
        """Добавить anti_pattern к скиллу."""
        skill = self.get_skill(name)
        if not skill:
            return
        skill.anti_patterns.append({
            "error": error,
            "happened": str(uuid.uuid4())[:8],  # timestamp заменится
            "source_msg_id": source_msg_id,
        })
        skill.version += 1
        self.save_skill(skill)
