"""Mind Mirror (T2, Memory Transparency) — Markdown-проекция памяти агента.

Два слоя в `~/caesar/mind/` (точнее: storage.db_path.parent / "mind"):
- `auto/` — READ-ONLY проекция L2+KG. Регенерируется фазой Dream Cycle (после
  entity dedup) и по требованию `caesar memory export`. Не two-way sync — БД
  canonical, markdown = проекция. Browsable в Obsidian, но Obsidian не обязателен.
- `manual/` — user-curated; агент читает как авторитетные high-priority факты
  (аналог AGENTS.md). Правки юзера = прямой редактор «что агент должен знать».

Политика (audit/roadmap): НЕ markdown-vault-as-primary-storage — БД (L2 temporal +
  L3 vector + KG) canonical, markdown только projection + curated overlay.
"""
import re
from pathlib import Path


def _safe_filename(name: str) -> str:
    """Entity name → безопасное имя файла (без /, пробелов и т.п.)."""
    s = re.sub(r"[^\w\-]+", "_", str(name)).strip("_")
    return s or "_"


class MindMirror:
    """Markdown-проекция памяти + curated overlay."""

    def __init__(self, storage, kg=None, path: Path | None = None):
        self.storage = storage
        self.kg = kg
        self.path: Path = path or (storage.db_path.parent / "mind")
        self.auto: Path = self.path / "auto"
        self.manual: Path = self.path / "manual"

    # --- projection (auto/) ---

    def export(self) -> dict:
        """Регенерировать auto/ из L2+KG. Возвращает счётчики. manual/ НЕ трогает."""
        # 1. Очищаем auto/ (только авто-генерируемое; manual/ неприкосновенен).
        if self.auto.exists():
            for f in self.auto.rglob("*"):
                if f.is_file():
                    f.unlink()
            # пустые поддиректории
            for d in sorted(self.auto.rglob("*"), reverse=True):
                if d.is_dir() and not any(d.iterdir()):
                    d.rmdir()
        self.auto.mkdir(parents=True, exist_ok=True)
        (self.manual).mkdir(parents=True, exist_ok=True)

        # 2. Активные L2-факты (все юзеры — владелец инспектирует своего агента).
        with self.storage._conn() as conn:
            facts = [dict(r) for r in conn.execute(
                """SELECT user_id, channel, entity, attribute, value, category, confidence, seq
                   FROM l2_facts WHERE valid_until IS NULL
                   ORDER BY entity, category, seq DESC"""
            ).fetchall()]
            # KG: сущности + отношения
            entities = [dict(r) for r in conn.execute(
                """SELECT DISTINCT name, entity_type FROM kg_entities"""
            ).fetchall()] if self._has_table(conn, "kg_entities") else []
            relations = [dict(r) for r in conn.execute(
                """SELECT from_entity, to_entity, relation_type FROM kg_relations"""
            ).fetchall()] if self._has_table(conn, "kg_relations") else []

        # 3. Индексы по категориям
        by_cat: dict[str, list[dict]] = {"decision": [], "win": [], "incident": []}
        for f in facts:
            c = f.get("category", "fact")
            if c in by_cat:
                by_cat[c].append(f)

        cat_titles = {"decision": "Решения", "win": "Победы", "incident": "Инциденты"}
        for cat, title in cat_titles.items():
            items = by_cat[cat]
            if not items:
                continue
            lines = [f"# {title} (активные, {len(items)})", ""]
            for f in items:
                lines.append(f"- **{f['entity']}** — {f['attribute']}: {f['value']}"
                             f"  `(user={f['user_id']}, conf={f.get('confidence','?')})`")
            (self.auto / f"{cat}s.md").write_text("\n".join(lines), encoding="utf-8")

        # 4. Все факты — индекс по сущности (facts.md)
        if facts:
            lines = ["# Все активные факты (индекс по сущности)", ""]
            cur_entity = None
            for f in facts:
                if f["entity"] != cur_entity:
                    cur_entity = f["entity"]
                    lines.append(f"\n## {cur_entity}")
                cat = f.get("category", "fact")
                lines.append(f"- [{cat}] {f['attribute']}: {f['value']}"
                             f"  `(user={f['user_id']})`")
            (self.auto / "facts.md").write_text("\n".join(lines), encoding="utf-8")

        # 5. per-entity страницы: факты + relations как wikilinks
        ents_dir = self.auto / "entities"
        ents_dir.mkdir(exist_ok=True)
        # группируем факты по сущности
        facts_by_entity: dict[str, list[dict]] = {}
        for f in facts:
            facts_by_entity.setdefault(f["entity"], []).append(f)
        # relations по сущности (from + to)
        rels_by_entity: dict[str, list[dict]] = {}
        for r in relations:
            rels_by_entity.setdefault(r["from_entity"], []).append(
                {"dir": "→", "rel": r["relation_type"], "target": r["to_entity"]})
            rels_by_entity.setdefault(r["to_entity"], []).append(
                {"dir": "←", "rel": r["relation_type"], "target": r["from_entity"]})

        # все имена сущностей (из фактов + KG)
        all_names = set(facts_by_entity) | {e["name"] for e in entities}
        for name in sorted(all_names):
            lines = [f"# {name}", ""]
            etype = next((e["entity_type"] for e in entities if e["name"] == name), None)
            if etype:
                lines.append(f"_тип: {etype}_")
                lines.append("")
            fs = facts_by_entity.get(name, [])
            if fs:
                lines.append("## Факты")
                by_c: dict[str, list[dict]] = {}
                for f in fs:
                    by_c.setdefault(f.get("category", "fact"), []).append(f)
                for c in ("decision", "win", "incident", "preference", "fact"):
                    items = by_c.get(c)
                    if not items:
                        continue
                    lines.append(f"- **{c}**:")
                    for f in items:
                        lines.append(f"  - {f['attribute']}: {f['value']}"
                                     f"  `(user={f['user_id']}, conf={f.get('confidence','?')})`")
            rs = rels_by_entity.get(name, [])
            if rs:
                lines.append("")
                lines.append("## Связи")
                for r in rs:
                    target_safe = _safe_filename(r["target"])
                    lines.append(f"- `{r['dir']}` {r['rel']} → [[{target_safe}|{r['target']}]]")
            (ents_dir / f"{_safe_filename(name)}.md").write_text("\n".join(lines), encoding="utf-8")

        # 6. README
        (self.auto / "README.md").write_text(
            "# Auto-проекция памяти агента\n\n"
            "⚠️ НЕ редактируй — регенерируется Dream Cycle и `caesar memory export`.\n"
            "Чтобы поправить/добавить знание — пиши в `../manual/*.md` (curated overlay).\n\n"
            f"Сгенерировано: фактов {len(facts)}, сущностей {len(all_names)}, "
            f"связей {len(relations)}.\n",
            encoding="utf-8",
        )
        return {"facts": len(facts), "entities": len(all_names), "relations": len(relations)}

    @staticmethod
    def _has_table(conn, name: str) -> bool:
        try:
            return bool(conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
            ).fetchone())
        except Exception:
            return False

    # --- curated overlay (manual/) ---

    def load_manual_context(self, max_chars: int = 4000) -> str:
        """Прочитать manual/*.md как high-priority context (curated overlay).

        Возвращает склеенный текст с заголовками файлов. Пусто → ''.
        Агент инжектит это в контекст как авторитетные user-curated факты.
        """
        if not self.manual.exists():
            return ""
        chunks: list[str] = []
        total = 0
        suffix = "\n…(обрезано)"
        for f in sorted(self.manual.glob("*.md")):
            try:
                content = f.read_text(encoding="utf-8").strip()
            except Exception:
                continue
            if not content:
                continue
            header = f"## {f.stem} (curated)"
            block = f"{header}\n{content}"
            if total + len(block) <= max_chars:
                chunks.append(block)
                total += len(block)
                continue
            # Не помещается целиком — обрезаем до остатка бюджета (хоть заголовок + часть).
            remain = max_chars - total
            if remain > len(header) + len(suffix) + 20:
                chunks.append(block[: remain - len(suffix)] + suffix)
            break
        return "\n\n".join(chunks)
