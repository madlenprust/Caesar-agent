"""Хранилище данных — SQLite.

См. roadmap разделы 6, 12, 13.

Хранит:
- users (раздел 13.12)
- channels (раздел 13.12)
- channel_members (раздел 13.12)
- tasks (раздел 12.4)
- task_actions (раздел 13.4) — лог каждого шага
- l2_facts (раздел 6.8) — temporal schema
- l3_chunks (раздел 14.8) — векторная база
- l4_skills (раздел 6.10) — процедурная память
- cron_tasks (раздел 13.9)
- permissions (раздел 11.1)
- token_usage (раздел 10.5) — счётчик токенов
"""

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from caesar.config import DB_PATH
from caesar.logging_setup import get_logger

# Python 3.12: дефолтный sqlite3-адаптер для datetime объявлен устаревшим.
# Регистрируем явный эквивалент (формат совпадает с прежним дефолтным —
# 'YYYY-MM-DD HH:MM:SS.ffffff'), чтобы убрать DeprecationWarning и сохранить
# совместимость со значениями, уже лежащими в БД (CURRENT_TIMESTAMP — тот же
# пространственно-разделённый формат). Действует глобально для модуля sqlite3.
sqlite3.register_adapter(datetime, lambda d: d.isoformat(" "))


SCHEMA_SQL = """
-- Users (раздел 13.12)
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    telegram_id TEXT,
    telegram_username TEXT,
    unix_uid INTEGER UNIQUE,
    display_name TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Channels (раздел 13.12) — сессия = канал
CREATE TABLE IF NOT EXISTS channels (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    source TEXT NOT NULL,           -- telegram | cli | web
    source_chat_id TEXT,            -- TG: chat_id, CLI: terminal, web: tab
    display_name TEXT NOT NULL,     -- main | Книга | Сайт А | news-bot
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_activity TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    status TEXT DEFAULT 'active'   -- active | idle | closed
);

CREATE TABLE IF NOT EXISTS channel_members (
    channel_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    role TEXT DEFAULT 'member',     -- owner | member
    joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (channel_id, user_id)
);

-- Tasks (раздел 12.4)
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    author_id TEXT,
    source TEXT,                    -- telegram | cli | cron | webhook
    source_chat_id TEXT,
    user_message TEXT NOT NULL,
    status TEXT DEFAULT 'pending',  -- pending | running | waiting_for_user | completed | failed
    priority INTEGER DEFAULT 2,     -- 1=high, 2=normal, 3=low
    complexity TEXT DEFAULT 'simple',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    worker_id TEXT,
    plan TEXT,                      -- JSON
    current_step INTEGER DEFAULT 0,
    tokens_used INTEGER DEFAULT 0,
    cost_rub REAL DEFAULT 0,
    result TEXT,
    error TEXT,
    retry_count INTEGER DEFAULT 0,
    waiting_question TEXT,
    waiting_since TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_channel ON tasks(channel_id);

-- Task actions (раздел 13.4) — лог каждого шага
CREATE TABLE IF NOT EXISTS task_actions (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    step_number INTEGER NOT NULL,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    action_type TEXT,               -- tool_call | llm_response | plan_update
    tool_name TEXT,
    tool_args TEXT,                 -- JSON
    tool_result TEXT,               -- JSON (обрезанный до 10KB)
    llm_thinking TEXT,
    tokens_used INTEGER DEFAULT 0,
    success INTEGER DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_actions_task ON task_actions(task_id, step_number);

-- L2 facts (раздел 6.8) — temporal schema
CREATE TABLE IF NOT EXISTS l2_facts (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    channel TEXT NOT NULL,          -- main | Книга | Сайт А | ...
    author_id TEXT,                 -- кто сказал (для multi-user)
    entity TEXT NOT NULL,
    attribute TEXT NOT NULL,
    value TEXT NOT NULL,
    valid_from TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    valid_until TIMESTAMP,          -- NULL = актуален
    source_msg_id TEXT,
    superseded_by TEXT,
    tags TEXT,                      -- JSON array
    summary TEXT,
    confidence TEXT,                -- high | medium
    seq INTEGER                    -- для сортировки новых вперёд
);

CREATE INDEX IF NOT EXISTS idx_l2_active ON l2_facts(channel, user_id) WHERE valid_until IS NULL;
CREATE INDEX IF NOT EXISTS idx_l2_entity ON l2_facts(entity, attribute, user_id) WHERE valid_until IS NULL;

-- L3 chunks (раздел 14.8) — векторная база (sqlite-vec отдельно)
CREATE TABLE IF NOT EXISTS l3_chunks (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    author_id TEXT,
    content TEXT NOT NULL,
    chunk_metadata TEXT,            -- JSON
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    task_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_l3_channel ON l3_chunks(channel, user_id);

-- L4 skills (раздел 6.10) — процедурная память
CREATE TABLE IF NOT EXISTS l4_skills (
    name TEXT PRIMARY KEY,
    trigger TEXT NOT NULL,
    prerequisites TEXT,             -- JSON array
    exact_recipe TEXT,              -- JSON array of steps
    anti_patterns TEXT,             -- JSON array
    pitfalls TEXT,                  -- JSON array
    example TEXT,
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_success TIMESTAMP,
    success_count INTEGER DEFAULT 0,
    failure_count INTEGER DEFAULT 0,
    consecutive_failures INTEGER DEFAULT 0,  -- 3 подряд → broken (не lifetime)
    version INTEGER DEFAULT 1,
    needs_validation INTEGER DEFAULT 0,
    broken INTEGER DEFAULT 0,
    yaml_path TEXT                  -- путь к YAML-файлу
);

-- Cron tasks (раздел 13.9)
CREATE TABLE IF NOT EXISTS cron_tasks (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    channel_id TEXT,
    schedule TEXT NOT NULL,         -- cron-формат
    schedule_human TEXT,
    task_to_execute TEXT NOT NULL,
    timezone TEXT DEFAULT 'Europe/Moscow',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_run_at TIMESTAMP,
    next_run_at TIMESTAMP,
    enabled INTEGER DEFAULT 1,
    notify_on_success INTEGER DEFAULT 0,
    notify_on_failure INTEGER DEFAULT 1,
    total_runs INTEGER DEFAULT 0,
    successful_runs INTEGER DEFAULT 0,
    failed_runs INTEGER DEFAULT 0,
    consecutive_failures INTEGER DEFAULT 0
);

-- Permissions (раздел 11.1) — whitelist разрешений
CREATE TABLE IF NOT EXISTS permissions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    tool TEXT NOT NULL,             -- shell_exec | write_file | tg_post | ...
    pattern TEXT,                   -- regex или точная команда
    permission_type TEXT,           -- exact_allow | pattern_allow | exact_deny
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    note TEXT
);

CREATE INDEX IF NOT EXISTS idx_permissions_user ON permissions(user_id, tool);

-- Token usage (раздел 10.5) — счётчик токенов
CREATE TABLE IF NOT EXISTS token_usage (
    id TEXT PRIMARY KEY,
    task_id TEXT,
    step INTEGER,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    llm_role TEXT,                  -- smart | cheap | none
    llm_model TEXT,
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    total_tokens INTEGER DEFAULT 0,
    cost_rub REAL DEFAULT 0,
    reason TEXT                     -- main_answer | reflection | analysis | extraction
);

CREATE INDEX IF NOT EXISTS idx_token_task ON token_usage(task_id);

-- Conversation messages — история диалога по каналам
CREATE TABLE IF NOT EXISTS conversation_messages (
    id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL,
    role TEXT NOT NULL,            -- user | assistant | system
    content TEXT NOT NULL,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    task_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_conv_channel ON conversation_messages(channel_id, timestamp);

-- Knowledge Graph: entities (people, companies, concepts)
CREATE TABLE IF NOT EXISTS kg_entities (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,             -- "Alice", "Acme AI", "агрессия"
    entity_type TEXT,               -- person | company | concept | place | project
    first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    mention_count INTEGER DEFAULT 1,
    metadata TEXT                   -- JSON: {source_chunks: [...], aliases: [...]}
);

CREATE INDEX IF NOT EXISTS idx_kg_entities_user_name ON kg_entities(user_id, name);
CREATE INDEX IF NOT EXISTS idx_kg_entities_type ON kg_entities(entity_type);

-- Knowledge Graph: relations between entities
CREATE TABLE IF NOT EXISTS kg_relations (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    from_entity TEXT NOT NULL,      -- entity name
    to_entity TEXT NOT NULL,        -- entity name
    relation_type TEXT NOT NULL,    -- works_at | founded | invested_in | attended | advises | related_to
    source_chunk_id TEXT,           -- l3_chunks.id откуда извлечено
    confidence REAL DEFAULT 1.0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_kg_relations_from ON kg_relations(from_entity);
CREATE INDEX IF NOT EXISTS idx_kg_relations_to ON kg_relations(to_entity);
CREATE INDEX IF NOT EXISTS idx_kg_relations_user ON kg_relations(user_id);
"""


class Storage:
    """SQLite-хранилище.
    
    Использует WAL mode для конкурентного доступа.
    """
    
    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self.log = get_logger("storage")
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
    
    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript(SCHEMA_SQL)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=OFF")
            # Миграция: убираем UNIQUE constraint с telegram_id
            # SQLite не поддерживает ALTER TABLE DROP CONSTRAINT
            # Поэтому пересоздаём таблицу
            try:
                # Проверяем схему таблицы users
                schema = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='users'").fetchone()
                if schema and 'UNIQUE' in schema['sql'].upper() and 'telegram_id' in schema['sql']:
                    self.log.info("Migrating users table: removing UNIQUE on telegram_id")
                    conn.executescript("""
                        CREATE TABLE IF NOT EXISTS users_new (
                            id TEXT PRIMARY KEY,
                            telegram_id TEXT,
                            telegram_username TEXT,
                            unix_uid INTEGER UNIQUE,
                            display_name TEXT,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        );
                        INSERT OR IGNORE INTO users_new (id, telegram_id, telegram_username, unix_uid, display_name, created_at)
                        SELECT id, telegram_id, telegram_username, unix_uid, display_name, created_at FROM users;
                        DROP TABLE users;
                        ALTER TABLE users_new RENAME TO users;
                    """)
                    self.log.info("Migration complete: users table recreated without UNIQUE on telegram_id")
            except Exception as e:
                self.log.warning(f"Migration failed (may be OK if already migrated): {e}")
            
            # Миграция: добавляем detected_language колонку для STT
            try:
                columns = [row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()]
                if "detected_language" not in columns:
                    conn.execute("ALTER TABLE users ADD COLUMN detected_language TEXT")
                    self.log.info("Migration: added detected_language column to users table")
                if "god_mode" not in columns:
                    conn.execute("ALTER TABLE users ADD COLUMN god_mode INTEGER DEFAULT 0")
                    self.log.info("Migration: added god_mode column to users table")
            except Exception as e:
                self.log.warning(f"Migration detected_language/god_mode failed: {e}")
            
            # Миграция: добавляем original_directive колонку в tasks
            try:
                task_columns = [row["name"] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()]
                if "original_directive" not in task_columns:
                    conn.execute("ALTER TABLE tasks ADD COLUMN original_directive TEXT")
                    self.log.info("Migration: added original_directive column to tasks table")
                # paused — флаг мягкой паузы (/pause). Watchdog исключает paused
                # задачи из hang-kill, поэтому колонка должна быть в DB, не только в RAM.
                if "paused" not in task_columns:
                    conn.execute("ALTER TABLE tasks ADD COLUMN paused INTEGER DEFAULT 0")
                    self.log.info("Migration: added paused column to tasks table")
            except Exception as e:
                self.log.warning(f"Migration original_directive/paused failed: {e}")

            # Миграция: добавляем consecutive_failures колонку в l4_skills
            try:
                l4_columns = [row["name"] for row in conn.execute("PRAGMA table_info(l4_skills)").fetchall()]
                if "consecutive_failures" not in l4_columns:
                    conn.execute("ALTER TABLE l4_skills ADD COLUMN consecutive_failures INTEGER DEFAULT 0")
                    self.log.info("Migration: added consecutive_failures column to l4_skills table")
            except Exception as e:
                self.log.warning(f"Migration consecutive_failures failed: {e}")

            # Миграция: добавляем индексы для скорости /status
            try:
                conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_created_at ON tasks(created_at DESC)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_token_usage_timestamp ON token_usage(timestamp DESC)")
                self.log.info("Migration: ensured indexes on tasks.created_at and token_usage.timestamp")
            except Exception as e:
                self.log.warning(f"Migration indexes failed: {e}")
    
    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=OFF")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    
    # === Users ===
    
    def upsert_user(
        self,
        user_id: str,
        telegram_id: str | None = None,
        telegram_username: str | None = None,
        unix_uid: int | None = None,
        display_name: str | None = None
    ) -> None:
        with self._conn() as conn:
            try:
                conn.execute("""
                    INSERT INTO users (id, telegram_id, telegram_username, unix_uid, display_name)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        telegram_id = COALESCE(excluded.telegram_id, telegram_id),
                        telegram_username = COALESCE(excluded.telegram_username, telegram_username),
                        unix_uid = COALESCE(excluded.unix_uid, unix_uid),
                        display_name = COALESCE(excluded.display_name, display_name)
                """, (user_id, telegram_id, telegram_username, unix_uid, display_name))
            except sqlite3.IntegrityError:
                # UNIQUE constraint на telegram_id — другой user уже имеет этот telegram_id
                # Просто обновляем того user'а который имеет этот telegram_id
                if telegram_id:
                    conn.execute("""
                        UPDATE users SET
                            unix_uid = COALESCE(?, unix_uid),
                            telegram_username = COALESCE(?, telegram_username),
                            display_name = COALESCE(?, display_name)
                        WHERE telegram_id = ?
                    """, (unix_uid, telegram_username, display_name, telegram_id))
                    self.log.info(f"Updated existing user with telegram_id={telegram_id}")
                else:
                    raise
    
    def get_user_by_telegram(self, telegram_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
            return dict(row) if row else None
    
    def get_user_by_uid(self, unix_uid: int) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE unix_uid = ?", (unix_uid,)
            ).fetchone()
            return dict(row) if row else None
    
    def get_user_language(self, user_id: str) -> str | None:
        """Получить определённый язык пользователя для STT.
        
        Возвращает код языка ('ru', 'en', 'ar', ...) или None если не определён.
        """
        with self._conn() as conn:
            row = conn.execute(
                "SELECT detected_language FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            if row and row["detected_language"]:
                return row["detected_language"]
            return None
    
    def set_user_language(self, user_id: str, language: str) -> None:
        """Сохранить определённый язык пользователя для STT."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE users SET detected_language = ? WHERE id = ?",
                (language, user_id),
            )
            conn.commit()
    
    def get_user_god_mode(self, user_id: str) -> bool:
        """Проверить активен ли GOD MODE для пользователя."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT god_mode FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            return bool(row and row["god_mode"])
    
    def set_user_god_mode(self, user_id: str, enabled: bool) -> None:
        """Включить/выключить GOD MODE для пользователя."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE users SET god_mode = ? WHERE id = ?",
                (1 if enabled else 0, user_id),
            )
            conn.commit()
    
    # === Channels ===
    
    def upsert_channel(
        self,
        channel_id: str,
        user_id: str,
        source: str,
        source_chat_id: str | None,
        display_name: str
    ) -> None:
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO channels (id, user_id, source, source_chat_id, display_name, last_activity)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(id) DO UPDATE SET
                    last_activity = CURRENT_TIMESTAMP,
                    status = 'active'
            """, (channel_id, user_id, source, source_chat_id, display_name))
    
    def get_channel(self, channel_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM channels WHERE id = ?", (channel_id,)
            ).fetchone()
            return dict(row) if row else None
    
    # === L2 facts (temporal) ===
    
    def add_fact(
        self,
        user_id: str,
        channel: str,
        entity: str,
        attribute: str,
        value: str,
        confidence: str = "medium",
        author_id: str | None = None,
        source_msg_id: str | None = None,
        tags: list[str] | None = None,
        summary: str | None = None
    ) -> dict:
        """Добавить факт. См. roadmap раздел 6.6 (lazy consolidation).
        
        Логика:
        - Если есть активный факт с тем же entity+attribute и тем же value → duplicate
        - Если есть с другим value → старый инвалидим (superseded), новый добавляем
        - Если нет → добавляем
        """
        with self._conn() as conn:
            # Ищем существующий активный факт
            row = conn.execute("""
                SELECT * FROM l2_facts
                WHERE user_id = ? AND channel = ? AND entity = ? AND attribute = ?
                  AND valid_until IS NULL
                ORDER BY seq DESC LIMIT 1
            """, (user_id, channel, entity, attribute)).fetchone()
            
            if row:
                if row["value"] == value:
                    return {"status": "duplicate", "fact_id": row["id"]}
                # Инвалидируем старый
                new_id = f"fact-{uuid.uuid4().hex[:12]}"
                conn.execute("""
                    UPDATE l2_facts SET valid_until = CURRENT_TIMESTAMP, superseded_by = ?
                    WHERE id = ?
                """, (new_id, row["id"]))
                # Добавляем новый
                seq = (row["seq"] or 0) + 1
                conn.execute("""
                    INSERT INTO l2_facts (id, user_id, channel, author_id, entity, attribute, value,
                                          source_msg_id, tags, summary, confidence, seq)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    new_id, user_id, channel, author_id, entity, attribute, value,
                    source_msg_id, json.dumps(tags or []), summary, confidence, seq
                ))
                return {"status": "superseded", "fact_id": new_id, "superseded_fact_id": row["id"]}
            
            # Нового факта нет
            new_id = f"fact-{uuid.uuid4().hex[:12]}"
            # seq — максимальный в этом канале + 1 (для сортировки новых вперёд)
            max_seq = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM l2_facts WHERE user_id = ? AND channel = ?",
                (user_id, channel)
            ).fetchone()[0]
            conn.execute("""
                INSERT INTO l2_facts (id, user_id, channel, author_id, entity, attribute, value,
                                      source_msg_id, tags, summary, confidence, seq)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                new_id, user_id, channel, author_id, entity, attribute, value,
                source_msg_id, json.dumps(tags or []), summary, confidence, max_seq + 1
            ))
            return {"status": "created", "fact_id": new_id}
    
    def get_facts(
        self,
        user_id: str,
        channel: str,
        entity: str | None = None,
        attribute: str | None = None,
        limit: int = 50
    ) -> list[dict]:
        """Получить факты канала (новые вперёд)."""
        with self._conn() as conn:
            query = """
                SELECT * FROM l2_facts
                WHERE user_id = ? AND channel = ? AND valid_until IS NULL
            """
            params: list[Any] = [user_id, channel]
            if entity:
                query += " AND entity = ?"
                params.append(entity)
            if attribute:
                query += " AND attribute = ?"
                params.append(attribute)
            query += " ORDER BY seq DESC LIMIT ?"
            params.append(limit)
            
            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]
    
    # === Tasks (persistence for graceful restart) ===
    
    def save_task(self, task: dict) -> None:
        """Сохранить/обновить задачу в БД (для persistence при рестарте).
        
        Сохраняет ВСЕ поля включая original_directive, waiting_question,
        waiting_since — чтобы после рестарта daemon полностью восстановить
        контекст задачи.
        """
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO tasks
                (id, user_id, channel_id, author_id, source, source_chat_id,
                 user_message, original_directive,
                 status, priority, complexity,
                 created_at, started_at, completed_at,
                 worker_id, current_step, tokens_used, cost_rub,
                 result, error, retry_count,
                 waiting_question, waiting_since)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    task["id"],
                    task["user_id"],
                    task["channel_id"],
                    task.get("author_id", ""),
                    task.get("source", ""),
                    task.get("source_chat_id", ""),
                    task["user_message"],
                    task.get("original_directive", ""),
                    task.get("status", "pending"),
                    task.get("priority", 2),
                    task.get("complexity", "simple"),
                    task.get("created_at"),
                    task.get("started_at"),
                    task.get("completed_at"),
                    task.get("worker_id"),
                    task.get("current_step", 0),
                    task.get("tokens_used", 0),
                    task.get("cost_rub", 0.0),
                    task.get("result"),
                    task.get("error"),
                    task.get("retry_count", 0),
                    task.get("waiting_question"),
                    task.get("waiting_since"),
                ),
            )
    
    def update_task_status(self, task_id: str, status: str, **extra) -> None:
        """Обновить статус задачи (и опционально другие поля)."""
        fields = ["status = ?"]
        values = [status]
        for k, v in extra.items():
            if k in ("started_at", "completed_at", "worker_id", "current_step",
                     "tokens_used", "result", "error", "retry_count", "paused"):
                fields.append(f"{k} = ?")
                values.append(v)
        values.append(task_id)
        
        with self._conn() as conn:
            conn.execute(
                f"UPDATE tasks SET {', '.join(fields)} WHERE id = ?",
                values,
            )
            conn.commit()
    
    def get_unfinished_tasks(self) -> list[dict]:
        """Получить незавершённые задачи (для restore после рестарта)."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM tasks
                WHERE status IN ('pending', 'running', 'waiting_for_user')
                ORDER BY created_at ASC""",
            ).fetchall()
            return [dict(r) for r in rows]
    
    def clear_unfinished_tasks(self) -> int:
        """Удалить незавершённые задачи (после restore). Возвращает count."""
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM tasks WHERE status IN ('pending', 'running', 'waiting_for_user')",
            )
            conn.commit()
            return cur.rowcount
    
    # === Task actions (лог) ===
    
    def log_action(
        self,
        task_id: str,
        step_number: int,
        action_type: str,
        tool_name: str | None = None,
        tool_args: dict | None = None,
        tool_result: Any | None = None,
        llm_thinking: str | None = None,
        tokens_used: int = 0,
        success: bool = True
    ) -> None:
        result_str = None
        if tool_result is not None:
            result_str = json.dumps(tool_result, ensure_ascii=False, default=str)[:10240]
        
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO task_actions (id, task_id, step_number, action_type, tool_name,
                                          tool_args, tool_result, llm_thinking, tokens_used, success)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                f"action-{uuid.uuid4().hex[:12]}",
                task_id, step_number, action_type, tool_name,
                json.dumps(tool_args, ensure_ascii=False, default=str) if tool_args else None,
                result_str, llm_thinking, tokens_used, 1 if success else 0
            ))
    
    def get_actions(self, task_id: str, limit: int = 100) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM task_actions WHERE task_id = ? ORDER BY step_number DESC LIMIT ?",
                (task_id, limit)
            ).fetchall()
            return [dict(r) for r in rows]
    
    # === Token usage ===
    
    def log_token_usage(
        self,
        task_id: str | None,
        step: int,
        llm_role: str,
        llm_model: str,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        cost_rub: float,
        reason: str
    ) -> None:
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO token_usage (id, task_id, step, llm_role, llm_model,
                                        prompt_tokens, completion_tokens, total_tokens, cost_rub, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                f"tu-{uuid.uuid4().hex[:12]}",
                task_id, step, llm_role, llm_model,
                prompt_tokens, completion_tokens, total_tokens, cost_rub, reason
            ))
    
    def get_token_stats(self, task_id: str | None = None) -> dict:
        with self._conn() as conn:
            if task_id:
                row = conn.execute(
                    """SELECT 
                        COUNT(*) as calls,
                        SUM(prompt_tokens) as prompt,
                        SUM(completion_tokens) as completion,
                        SUM(total_tokens) as total,
                        SUM(cost_rub) as cost
                    FROM token_usage WHERE task_id = ?""",
                    (task_id,),
                ).fetchone()
            else:
                row = conn.execute("""
                    SELECT 
                        COUNT(*) as calls,
                        SUM(prompt_tokens) as prompt,
                        SUM(completion_tokens) as completion,
                        SUM(total_tokens) as total,
                        SUM(cost_rub) as cost
                    FROM token_usage
                """).fetchone()
            return dict(row) if row else {}
    
    # === Cron tasks ===
    
    def add_cron_task(self, cron_task: dict) -> str:
        cron_id = cron_task.get("id") or f"cron-{uuid.uuid4().hex[:12]}"
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO cron_tasks (id, user_id, channel_id, schedule, schedule_human,
                                       task_to_execute, timezone, notify_on_success, notify_on_failure,
                                       next_run_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                cron_id, cron_task["user_id"], cron_task.get("channel_id"),
                cron_task["schedule"], cron_task.get("schedule_human"),
                cron_task["task_to_execute"], cron_task.get("timezone", "Europe/Moscow"),
                cron_task.get("notify_on_success", 0), cron_task.get("notify_on_failure", 1),
                cron_task.get("next_run_at")
            ))
        return cron_id
    
    def list_cron_tasks(self, user_id: str, only_enabled: bool = False) -> list[dict]:
        with self._conn() as conn:
            query = "SELECT * FROM cron_tasks WHERE user_id = ?"
            if only_enabled:
                query += " AND enabled = 1"
            rows = conn.execute(query, (user_id,)).fetchall()
            return [dict(r) for r in rows]
    
    def disable_cron_task(self, cron_id: str) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE cron_tasks SET enabled = 0 WHERE id = ?", (cron_id,))
    
    def update_cron_run(self, cron_id: str, success: bool, next_run_at: datetime | None = None) -> None:
        with self._conn() as conn:
            if success:
                conn.execute("""
                    UPDATE cron_tasks SET 
                        last_run_at = CURRENT_TIMESTAMP,
                        next_run_at = ?,
                        total_runs = total_runs + 1,
                        successful_runs = successful_runs + 1,
                        consecutive_failures = 0
                    WHERE id = ?
                """, (next_run_at, cron_id))
            else:
                conn.execute("""
                    UPDATE cron_tasks SET 
                        last_run_at = CURRENT_TIMESTAMP,
                        next_run_at = ?,
                        total_runs = total_runs + 1,
                        failed_runs = failed_runs + 1,
                        consecutive_failures = consecutive_failures + 1
                    WHERE id = ?
                """, (next_run_at, cron_id))
                
                # Если 3 неудачи подряд — отключаем
                row = conn.execute(
                    "SELECT consecutive_failures FROM cron_tasks WHERE id = ?", (cron_id,)
                ).fetchone()
                if row and row["consecutive_failures"] >= 3:
                    conn.execute("UPDATE cron_tasks SET enabled = 0 WHERE id = ?", (cron_id,))
    
    # === L4 Skills ===
    
    def upsert_skill(self, skill: dict) -> None:
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO l4_skills (name, trigger, prerequisites, exact_recipe, anti_patterns,
                                       pitfalls, example, notes, version, yaml_path)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    trigger = excluded.trigger,
                    prerequisites = excluded.prerequisites,
                    exact_recipe = excluded.exact_recipe,
                    anti_patterns = excluded.anti_patterns,
                    pitfalls = excluded.pitfalls,
                    example = excluded.example,
                    notes = excluded.notes,
                    version = l4_skills.version + 1,
                    yaml_path = excluded.yaml_path
            """, (
                skill["name"], skill.get("trigger", ""),
                json.dumps(skill.get("prerequisites", []), ensure_ascii=False),
                json.dumps(skill.get("exact_recipe", []), ensure_ascii=False),
                json.dumps(skill.get("anti_patterns", []), ensure_ascii=False),
                json.dumps(skill.get("pitfalls", []), ensure_ascii=False),
                skill.get("example", ""), skill.get("notes", ""),
                skill.get("version", 1), skill.get("yaml_path")
            ))
    
    def get_skill(self, name: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM l4_skills WHERE name = ?", (name,)).fetchone()
            if not row:
                return None
            d = dict(row)
            for k in ("prerequisites", "exact_recipe", "anti_patterns", "pitfalls"):
                if d.get(k):
                    try:
                        d[k] = json.loads(d[k])
                    except json.JSONDecodeError:
                        d[k] = []
            return d
    
    def list_skills(self, only_enabled: bool = False) -> list[dict]:
        with self._conn() as conn:
            query = "SELECT name, trigger, version, success_count, failure_count, needs_validation, broken FROM l4_skills"
            if only_enabled:
                query += " WHERE broken = 0"
            rows = conn.execute(query).fetchall()
            return [dict(r) for r in rows]
    
    # === Conversation history ===
    
    def save_message(
        self,
        channel_id: str,
        role: str,
        content: str,
        task_id: str | None = None,
    ) -> None:
        """Сохранить сообщение в историю диалога канала."""
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO conversation_messages (id, channel_id, role, content, task_id)
                VALUES (?, ?, ?, ?, ?)
            """, (
                f"msg-{uuid.uuid4().hex[:12]}",
                channel_id, role, content, task_id,
            ))
    
    def get_messages(
        self,
        channel_id: str,
        limit: int = 20,
    ) -> list[dict]:
        """Получить последние N сообщений канала (от старых к новым)."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM conversation_messages 
                   WHERE channel_id = ? 
                   ORDER BY timestamp DESC LIMIT ?""",
                (channel_id, limit),
            ).fetchall()
            # Разворачиваем — от старых к новым
            return list(reversed([dict(r) for r in rows]))
    
    def clear_conversation(self, channel_id: str) -> None:
        """Очистить историю диалога канала."""
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM conversation_messages WHERE channel_id = ?",
                (channel_id,),
            )
