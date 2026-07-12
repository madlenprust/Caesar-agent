"""Генератор расширенного статуса системы.

Используется:
- `caesar --status` (CLI) — через socket action 'get_status' к daemon
- Telegram /status — через тот же socket action
- `caesar doctor` — частично использует эти данные для health check

Возвращает dict с разделами:
- daemon: uptime, version, workers
- memory: L3 chunks, L4 skills, KG entities
- cron: active tasks + last_run info
- tokens: today / week totals
- recent: последние 5 диалогов (user_message + status + time)

Этот модуль НЕ зависит от daemon — работает напрямую с Storage.
Может вызываться как из daemon (для socket API), так и из CLI
(для офлайн-статуса если daemon упал).
"""

from datetime import datetime, timedelta
from typing import Any

from caesar.logging_setup import get_logger
from caesar.memory.storage import Storage


def generate_status_report(
    storage: Storage,
    queue: Any = None,
    version: str = "",
    uptime_seconds: float | None = None,
    user_id: str = "",
) -> dict:
    """Собрать полный отчёт о состоянии системы.
    
    Args:
        storage: Storage instance
        queue: TaskQueue instance (optional — для workers stats)
        version: версия Caesar
        uptime_seconds: сколько daemon уже работает (опционально)
        user_id: фильтровать recent dialogs по user_id (пустая строка = все)
    
    Returns:
        {
            "daemon": {...},
            "memory": {...},
            "cron": {...},
            "tokens": {...},
            "recent": [...],
        }
    
    Оптимизация: используем ОДИН SQLite connection на весь отчёт вместо
    отдельных connections на каждый раздел. На больших БД экономит ~100ms
    на setup/teardown connections.
    """
    log = get_logger("status")
    report = {}
    
    # === daemon ===
    report["daemon"] = {
        "version": version,
        "uptime_seconds": uptime_seconds,
        "uptime_human": _format_uptime(uptime_seconds) if uptime_seconds else None,
    }
    
    if queue:
        report["daemon"]["workers"] = {
            "interactive_active": queue.get_active_count("interactive"),
            "interactive_max": 5,
            "background_active": queue.get_active_count("background"),
            "background_max": 10,
            "interactive_pending": queue.get_pending_count("interactive"),
            "background_pending": queue.get_pending_count("background"),
        }
    
    # Все SQL запросы делаем в ОДНОМ connection — переиспользуем
    import sqlite3
    db_path = str(storage.db_path)
    conn = sqlite3.connect(db_path, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        # === memory ===
        try:
            report["memory"] = _collect_memory_stats_conn(conn, user_id)
        except Exception as e:
            log.warning(f"Memory stats failed: {e}")
            report["memory"] = {"l3": {"total_chunks": 0, "consolidated_chunks": 0},
                               "l4": {"total_skills": 0, "needs_validation": 0},
                               "kg": {"entities": 0, "relations": 0, "stale_entities": 0},
                               "error": str(e)}
        
        # === cron ===
        try:
            report["cron"] = _collect_cron_stats_conn(conn)
        except Exception as e:
            log.warning(f"Cron stats failed: {e}")
            report["cron"] = {"active": 0, "tasks": [], "error": str(e)}
        
        # === tokens ===
        try:
            report["tokens"] = _collect_token_stats_conn(conn)
        except Exception as e:
            log.warning(f"Token stats failed: {e}")
            report["tokens"] = {
                "today": {"total": 0, "smart": 0, "cheap": 0, "calls": 0, "cost_rub": 0.0},
                "week": {"total": 0, "smart": 0, "cheap": 0, "calls": 0, "cost_rub": 0.0},
                "error": str(e),
            }
        
        # === recent dialogs ===
        try:
            report["recent"] = _collect_recent_dialogs_conn(conn, user_id, limit=5)
        except Exception as e:
            log.warning(f"Recent dialogs failed: {e}")
            report["recent"] = [{"error": str(e)}]
    finally:
        conn.close()
    
    return report


def _format_uptime(seconds: float) -> str:
    """Превратить секунды в '2d 3h 15m'."""
    if seconds is None or seconds < 0:
        return "unknown"
    s = int(seconds)
    days = s // 86400
    s %= 86400
    hours = s // 3600
    s %= 3600
    minutes = s // 60
    
    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


def _collect_memory_stats(storage: Storage, user_id: str) -> dict:
    """Legacy wrapper — открывает свой connection. Используется в тестах."""
    import sqlite3
    conn = sqlite3.connect(str(storage.db_path), timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        return _collect_memory_stats_conn(conn, user_id)
    finally:
        conn.close()


def _collect_memory_stats_conn(conn, user_id: str) -> dict:
    """Статистика памяти: L3, L4, KG.
    
    Оптимизация: используем json_extract() вместо LIKE по JSON полю.
    LIKE '%pattern%' делает full table scan, json_extract парсит JSON быстро.
    На больших БД (1000+ chunks) разница в десятки раз.
    """
    stats = {
        "l3": {"total_chunks": 0, "consolidated_chunks": 0},
        "l4": {"total_skills": 0, "needs_validation": 0},
        "kg": {"entities": 0, "relations": 0, "stale_entities": 0},
    }
    
    try:
        # L3 chunks — total + consolidated
        if user_id:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM l3_chunks WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            consolidated_row = conn.execute(
                """SELECT COUNT(*) as cnt FROM l3_chunks 
                   WHERE user_id = ? 
                   AND json_extract(chunk_metadata, '$.type') = 'consolidated'""",
                (user_id,),
            ).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) as cnt FROM l3_chunks").fetchone()
            consolidated_row = conn.execute(
                """SELECT COUNT(*) as cnt FROM l3_chunks 
                   WHERE json_extract(chunk_metadata, '$.type') = 'consolidated'"""
            ).fetchone()
        
        stats["l3"]["total_chunks"] = row["cnt"] if row else 0
        stats["l3"]["consolidated_chunks"] = consolidated_row["cnt"] if consolidated_row else 0
        
        # L4 skills — total + needs_validation (один запрос вместо двух)
        skills_row = conn.execute(
            """SELECT 
                COUNT(*) as total,
                SUM(CASE WHEN needs_validation = 1 AND broken = 0 THEN 1 ELSE 0 END) as needs_val
               FROM l4_skills WHERE broken = 0"""
        ).fetchone()
        stats["l4"]["total_skills"] = skills_row["total"] if skills_row else 0
        stats["l4"]["needs_validation"] = skills_row["needs_val"] if skills_row else 0
        
        # KG entities & relations (один запрос entities вместо 3 отдельных)
        if user_id:
            ent_row = conn.execute(
                """SELECT 
                    COUNT(*) as total,
                    SUM(CASE WHEN json_extract(metadata, '$.stale') = 1 THEN 1 ELSE 0 END) as stale
                   FROM kg_entities WHERE user_id = ?""",
                (user_id,),
            ).fetchone()
            rel_row = conn.execute(
                "SELECT COUNT(*) as cnt FROM kg_relations WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        else:
            ent_row = conn.execute(
                """SELECT 
                    COUNT(*) as total,
                    SUM(CASE WHEN json_extract(metadata, '$.stale') = 1 THEN 1 ELSE 0 END) as stale
                   FROM kg_entities"""
            ).fetchone()
            rel_row = conn.execute("SELECT COUNT(*) as cnt FROM kg_relations").fetchone()
        
        stats["kg"]["entities"] = ent_row["total"] if ent_row else 0
        stats["kg"]["relations"] = rel_row["cnt"] if rel_row else 0
        stats["kg"]["stale_entities"] = ent_row["stale"] if ent_row else 0
    except Exception as e:
        stats["error"] = str(e)
    
    return stats


def _collect_cron_stats(storage: Storage) -> dict:
    """Legacy wrapper — открывает свой connection."""
    import sqlite3
    conn = sqlite3.connect(str(storage.db_path), timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        return _collect_cron_stats_conn(conn)
    finally:
        conn.close()


def _collect_cron_stats_conn(conn) -> dict:
    """Статистика cron задач — принимает conn (переиспользуется)."""
    stats = {"active": 0, "tasks": []}
    
    try:
        rows = conn.execute(
            """SELECT id, schedule_human, task_to_execute, enabled,
                      last_run_at, next_run_at, total_runs,
                      successful_runs, failed_runs, consecutive_failures
               FROM cron_tasks WHERE enabled = 1
               ORDER BY next_run_at ASC"""
        ).fetchall()
        
        stats["active"] = len(rows)
        for row in rows:
            d = dict(row)
            # Готовим human-readable last_run
            last_run_str = ""
            if d.get("last_run_at"):
                try:
                    last_dt = datetime.fromisoformat(
                        str(d["last_run_at"]).replace("T", " ").split(".")[0]
                    )
                    last_run_str = _format_relative_time(last_dt)
                except (ValueError, TypeError):
                    last_run_str = str(d["last_run_at"])
            
            next_run_str = ""
            if d.get("next_run_at"):
                try:
                    next_dt = datetime.fromisoformat(
                        str(d["next_run_at"]).replace("T", " ").split(".")[0]
                    )
                    next_run_str = _format_relative_time(next_dt, future=True)
                except (ValueError, TypeError):
                    next_run_str = str(d["next_run_at"])
            
            stats["tasks"].append({
                "id": d["id"],
                "schedule": d.get("schedule_human") or "",
                "task_preview": (d.get("task_to_execute") or "")[:60],
                "last_run": last_run_str,
                "next_run": next_run_str,
                "total_runs": d.get("total_runs", 0),
                "successful_runs": d.get("successful_runs", 0),
                "failed_runs": d.get("failed_runs", 0),
                "consecutive_failures": d.get("consecutive_failures", 0),
            })
    except Exception as e:
        stats["error"] = str(e)
    
    return stats


def _collect_token_stats(storage: Storage) -> dict:
    """Legacy wrapper — открывает свой connection."""
    import sqlite3
    conn = sqlite3.connect(str(storage.db_path), timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        return _collect_token_stats_conn(conn)
    finally:
        conn.close()


def _collect_token_stats_conn(conn) -> dict:
    """Статистика токенов за сегодня и за неделю — принимает conn.
    
    Оптимизация: один запрос с CASE вместо 4 отдельных.
    SQLite сам агрегирует — не нужно два раза сканировать таблицу.
    """
    stats = {
        "today": {"total": 0, "smart": 0, "cheap": 0, "calls": 0, "cost_rub": 0.0},
        "week": {"total": 0, "smart": 0, "cheap": 0, "calls": 0, "cost_rub": 0.0},
    }
    
    try:
        # Один запрос с агрегацией today/week + breakdown по ролям
        # SQLite позволяет посчитать всё за один pass по таблице.
        row = conn.execute(
            """SELECT
                SUM(CASE WHEN timestamp >= datetime('now', 'start of day') 
                         THEN 1 ELSE 0 END) as today_calls,
                SUM(CASE WHEN timestamp >= datetime('now', 'start of day') 
                         THEN COALESCE(total_tokens, 0) ELSE 0 END) as today_total,
                SUM(CASE WHEN timestamp >= datetime('now', 'start of day') 
                         THEN COALESCE(cost_rub, 0) ELSE 0 END) as today_cost,
                SUM(CASE WHEN timestamp >= datetime('now', 'start of day') 
                          AND llm_role = 'smart'
                         THEN COALESCE(total_tokens, 0) ELSE 0 END) as today_smart,
                SUM(CASE WHEN timestamp >= datetime('now', 'start of day') 
                          AND llm_role = 'cheap'
                         THEN COALESCE(total_tokens, 0) ELSE 0 END) as today_cheap,
                SUM(CASE WHEN timestamp >= datetime('now', '-7 days') 
                         THEN 1 ELSE 0 END) as week_calls,
                SUM(CASE WHEN timestamp >= datetime('now', '-7 days') 
                         THEN COALESCE(total_tokens, 0) ELSE 0 END) as week_total,
                SUM(CASE WHEN timestamp >= datetime('now', '-7 days') 
                         THEN COALESCE(cost_rub, 0) ELSE 0 END) as week_cost,
                SUM(CASE WHEN timestamp >= datetime('now', '-7 days') 
                          AND llm_role = 'smart'
                         THEN COALESCE(total_tokens, 0) ELSE 0 END) as week_smart,
                SUM(CASE WHEN timestamp >= datetime('now', '-7 days') 
                          AND llm_role = 'cheap'
                         THEN COALESCE(total_tokens, 0) ELSE 0 END) as week_cheap
               FROM token_usage
               WHERE timestamp >= datetime('now', '-7 days')"""
        ).fetchone()
        
        if row:
            stats["today"]["calls"] = row["today_calls"] or 0
            stats["today"]["total"] = row["today_total"] or 0
            stats["today"]["cost_rub"] = row["today_cost"] or 0.0
            stats["today"]["smart"] = row["today_smart"] or 0
            stats["today"]["cheap"] = row["today_cheap"] or 0
            
            stats["week"]["calls"] = row["week_calls"] or 0
            stats["week"]["total"] = row["week_total"] or 0
            stats["week"]["cost_rub"] = row["week_cost"] or 0.0
            stats["week"]["smart"] = row["week_smart"] or 0
            stats["week"]["cheap"] = row["week_cheap"] or 0
    except Exception as e:
        stats["error"] = str(e)
    
    return stats


def _collect_recent_dialogs(storage: Storage, user_id: str, limit: int = 5) -> list[dict]:
    """Legacy wrapper — открывает свой connection."""
    import sqlite3
    conn = sqlite3.connect(str(storage.db_path), timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        return _collect_recent_dialogs_conn(conn, user_id, limit)
    finally:
        conn.close()


def _collect_recent_dialogs_conn(conn, user_id: str, limit: int = 5) -> list[dict]:
    """Последние N диалогов (tasks) — принимает conn."""
    dialogs = []
    
    try:
        if user_id:
            # Фильтр по user_id + пустые (system tasks)
            rows = conn.execute(
                """SELECT id, user_message, status, complexity,
                          created_at, completed_at, current_step,
                          tokens_used, cost_rub, error
                   FROM tasks
                   WHERE user_id = ? OR user_id = ''
                   ORDER BY created_at DESC LIMIT ?""",
                (user_id, limit),
            ).fetchall()
        else:
            # Без фильтра — все tasks
            rows = conn.execute(
                """SELECT id, user_message, status, complexity,
                          created_at, completed_at, current_step,
                          tokens_used, cost_rub, error
                   FROM tasks
                   ORDER BY created_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        
        for row in rows:
            d = dict(row)
            # Готовим human-readable time
            time_str = ""
            if d.get("created_at"):
                try:
                    created_dt = datetime.fromisoformat(
                        str(d["created_at"]).replace("T", " ").split(".")[0]
                    )
                    time_str = _format_relative_time(created_dt)
                except (ValueError, TypeError):
                    time_str = str(d["created_at"])
            
            # Статус на русском
            status_ru = {
                "completed": "✅",
                "running": "🔄",
                "pending": "⏳",
                "failed": "❌",
                "waiting_for_user": "❓",
                "cancelled": "🚫",
            }.get(d.get("status"), d.get("status", "?"))
            
            dialogs.append({
                "id": d["id"],
                "message_preview": (d.get("user_message") or "")[:60],
                "status": d.get("status", ""),
                "status_icon": status_ru,
                "time": time_str,
                "steps": d.get("current_step", 0),
                "tokens": d.get("tokens_used", 0),
                "complexity": d.get("complexity", ""),
                "error": (d.get("error") or "")[:100] if d.get("status") == "failed" else "",
            })
    except Exception as e:
        # Логируем полную ошибку с типом исключения
        err_msg = f"{type(e).__name__}: {e}" if e else f"{type(e).__name__} (empty message)"
        dialogs = [{"error": err_msg}]
    
    return dialogs


def _format_relative_time(dt: datetime, future: bool = False) -> str:
    """Превратить datetime в '5 мин назад' или 'через 5 мин'.
    
    Args:
        dt: datetime (без tz — local time)
        future: True если это future time (next_run), False если past (last_run)
    """
    now = datetime.now()
    try:
        diff = (dt - now).total_seconds()
    except Exception:
        return str(dt)
    
    if future:
        if diff < 60:
            return "сейчас"
        if diff < 3600:
            return f"через {int(diff / 60)} мин"
        if diff < 86400:
            hours = int(diff / 3600)
            minutes = int((diff % 3600) / 60)
            return f"через {hours}ч {minutes}м"
        days = int(diff / 86400)
        return f"через {days} дн"
    else:
        if diff > -60:
            return "только что"
        diff = -diff
        if diff < 3600:
            return f"{int(diff / 60)} мин назад"
        if diff < 86400:
            hours = int(diff / 3600)
            minutes = int((diff % 3600) / 60)
            return f"{hours}ч {minutes}м назад"
        days = int(diff / 86400)
        return f"{days} дн назад"


def format_status_text(report: dict) -> str:
    """Превратить report dict в красивый текст для вывода.
    
    Используется в CLI (caesar --status) и в Telegram /status.
    """
    lines = []
    
    # === Header ===
    daemon = report.get("daemon", {})
    version = daemon.get("version", "?")
    uptime = daemon.get("uptime_human")
    header = f"🤖 Caesar v{version}"
    if uptime:
        header += f" — uptime {uptime}"
    lines.append(header)
    
    # Модели
    smart_model = daemon.get("smart_model")
    cheap_model = daemon.get("cheap_model")
    if smart_model:
        model_line = f"🧠 Smart: {smart_model}"
        if cheap_model:
            model_line += f" | 💰 Cheap: {cheap_model}"
        lines.append(model_line)
    
    # Контекст сессии
    ctx = daemon.get("context")
    if ctx:
        msgs = ctx.get("messages", 0)
        hist_tok = ctx.get("history_tokens", 0)
        total_tok = ctx.get("total_tokens", 0)
        lines.append(
            f"📝 Контекст: {msgs} сообщений, ~{total_tok:,} токенов "
            f"(история: {hist_tok:,})"
        )
    
    lines.append("")
    
    # === Memory ===
    mem = report.get("memory", {})
    if mem:
        l3 = mem.get("l3", {}) or {}
        l4 = mem.get("l4", {}) or {}
        kg = mem.get("kg", {}) or {}
        
        lines.append("📊 Память:")
        l3_total = l3.get("total_chunks") or 0
        l3_cons = l3.get("consolidated_chunks") or 0
        l3_parts = [f"L3: {l3_total} чанков"]
        if l3_cons > 0:
            l3_parts.append(f"({l3_cons} consolidated)")
        lines.append("  " + " ".join(l3_parts))
        
        l4_total = l4.get("total_skills") or 0
        l4_val = l4.get("needs_validation") or 0
        l4_parts = [f"L4: {l4_total} скиллов"]
        if l4_val > 0:
            l4_parts.append(f"({l4_val} needs validation)")
        lines.append("  " + " ".join(l4_parts))
        
        kg_ent = kg.get("entities") or 0
        kg_rel = kg.get("relations") or 0
        kg_stale = kg.get("stale_entities") or 0
        kg_parts = [f"KG: {kg_ent} entities"]
        if kg_rel > 0:
            kg_parts.append(f", {kg_rel} relations")
        if kg_stale > 0:
            kg_parts.append(f"({kg_stale} stale)")
        lines.append("  " + " ".join(kg_parts))
        lines.append("")
    
    # === Cron ===
    cron = report.get("cron", {})
    if cron:
        active = cron.get("active", 0)
        if active > 0:
            lines.append(f"⏰ Cron: {active} активных задач")
            for task in cron.get("tasks", [])[:5]:  # показываем первые 5
                schedule = task.get("schedule", "")
                preview = task.get("task_preview", "")
                last_run = task.get("last_run", "")
                failures = task.get("consecutive_failures", 0)
                
                line = f"  • {schedule} — «{preview}»"
                if last_run:
                    line += f"\n    последний: {last_run}"
                if failures > 0:
                    line += f" ⚠️ {failures} неудач подряд"
                lines.append(line)
        else:
            lines.append("⏰ Cron: нет активных задач")
        lines.append("")
    
    # === Tokens ===
    tokens = report.get("tokens", {})
    if tokens:
        today = tokens.get("today", {})
        week = tokens.get("week", {})
        
        today_total = today.get("total", 0)
        week_total = week.get("total", 0)
        
        if today_total > 0 or week_total > 0:
            today_smart = today.get("smart", 0)
            today_cheap = today.get("cheap", 0)
            today_cost = today.get("cost_rub", 0.0)
            week_cost = week.get("cost_rub", 0.0)
            
            today_total_fmt = _format_tokens(today_total)
            week_total_fmt = _format_tokens(week_total)
            
            lines.append("💰 Токены:")
            today_parts = [f"Сегодня: {today_total_fmt}"]
            if today_smart > 0 or today_cheap > 0:
                today_parts.append(
                    f"(smart: {_format_tokens(today_smart)}, "
                    f"cheap: {_format_tokens(today_cheap)})"
                )
            if today_cost > 0:
                today_parts.append(f"~{today_cost:.4f} руб")
            lines.append("  " + " ".join(today_parts))
            
            week_parts = [f"За неделю: {week_total_fmt}"]
            if week_cost > 0:
                week_parts.append(f"(~{week_cost:.4f} руб)")
            lines.append("  " + " ".join(week_parts))
            lines.append("")
    
    # === Recent dialogs ===
    recent = report.get("recent", [])
    if recent:
        lines.append("💬 Последние диалоги:")
        for d in recent:
            # Показываем ошибку ТОЛЬКО если она непустая
            if d.get("error"):
                lines.append(f"  ⚠️ Ошибка: {d['error']}")
                continue
            
            time = d.get("time", "")
            icon = d.get("status_icon", "?")
            preview = d.get("message_preview", "")
            status = d.get("status", "")
            steps = d.get("steps", 0)
            complexity = d.get("complexity", "")
            
            line = f"  • [{time}] {icon} «{preview}»"
            extra_parts = []
            if status == "completed" and steps > 0:
                extra_parts.append(f"{steps} шагов")
            if complexity:
                extra_parts.append(complexity)
            if extra_parts:
                line += f" ({', '.join(extra_parts)})"
            lines.append(line)
        lines.append("")
    
    # === Workers ===
    workers = daemon.get("workers", {})
    if workers:
        ia = workers.get("interactive_active", 0) or 0
        im = workers.get("interactive_max", 5)
        ba = workers.get("background_active", 0) or 0
        bm = workers.get("background_max", 10)
        ip = workers.get("interactive_pending", 0) or 0
        bp = workers.get("background_pending", 0) or 0
        lines.append(
            f"🎛  Workers: {ia}/{im} interactive, {ba}/{bm} background"
        )
        if ip > 0 or bp > 0:
            lines.append(f"   Pending: {ip}+{bp}")
    
    # TG sessions
    tg_sessions = daemon.get("tg_sessions", 0) or 0
    if tg_sessions > 0:
        lines.append(f"💬 TG сессий: {tg_sessions}")
    
    return "\n".join(lines).rstrip()


def _format_tokens(n: int) -> str:
    """Превратить 47234 → '47.2K', 1234567 → '1.2M'."""
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}K"
    return f"{n / 1_000_000:.1f}M"
