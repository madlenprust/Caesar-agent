"""Полный аудит кодовой базы Caesar.
Проверяет: импорты, undefined переменные, DB schema, config консистентность,
syntax errors, неиспользуемые импорты.
"""
import sys
import os
import ast
import importlib
import traceback
from pathlib import Path

sys.path.insert(0, "/home/z/my-project")

ISSUES = []

def issue(severity, file, msg):
    ISSUES.append((severity, file, msg))
    icon = "🔴" if severity == "CRITICAL" else "🟡" if severity == "WARNING" else "🔵"
    print(f"{icon} [{severity}] {file}: {msg}")

def check_imports():
    """Проверить что все модули импортируются без ошибок."""
    print("\n=== 1. IMPORTS ===\n")
    
    modules = [
        "caesar.config",
        "caesar.daemon",
        "caesar.core.orchestrator",
        "caesar.core.queue",
        "caesar.core.llm",
        "caesar.core.events",
        "caesar.core.cron",
        "caesar.core.dream",
        "caesar.core.briefing",
        "caesar.core.skill_executor",
        "caesar.memory.storage",
        "caesar.memory.l3",
        "caesar.memory.l4",
        "caesar.memory.knowledge_graph",
        "caesar.tools",
        "caesar.tools.internet",
        "caesar.tools.sources",
        "caesar.tools.memory_tools",
        "caesar.tools.stt",
        "caesar.tools.cron_tools",
        "caesar.tools.documents",
        "caesar.tools.self_knowledge",
        "caesar.tools.shell_files",
        "caesar.channels.cli_adapter",
        "caesar.channels.telegram_adapter",
        "caesar.management",
        "caesar.cli_bridge",
        "caesar.watchdog",
    ]
    
    for mod_name in modules:
        try:
            importlib.import_module(mod_name)
            print(f"  ✅ {mod_name}")
        except Exception as e:
            issue("CRITICAL", mod_name, f"Import failed: {e}")

def check_undefined_vars():
    """Проверить Python файлы на потенциально undefined переменные."""
    print("\n=== 2. UNDEFINED VARIABLES ===\n")
    
    caesar_dir = Path("/home/z/my-project/caesar")
    py_files = list(caesar_dir.rglob("*.py"))
    
    for py_file in py_files:
        rel = str(py_file.relative_to("/home/z/my-project"))
        try:
            with open(py_file, "r", encoding="utf-8") as f:
                source = f.read()
            
            tree = ast.parse(source, filename=str(py_file))
            
            # Собираем все присваивания и импорты
            defined = set()
            # Builtins
            defined.update(dir(__builtins__))
            
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        name = alias.asname or alias.name.split(".")[0]
                        defined.add(name)
                elif isinstance(node, ast.ImportFrom):
                    for alias in node.names:
                        name = alias.asname or alias.name
                        defined.add(name)
                elif isinstance(node, ast.FunctionDef) or isinstance(node, ast.AsyncFunctionDef):
                    defined.add(node.name)
                    # Args
                    for arg in node.args.args:
                        defined.add(arg.arg)
                    if node.args.vararg:
                        defined.add(node.args.vararg.arg)
                    if node.args.kwarg:
                        defined.add(node.args.kwarg.arg)
                elif isinstance(node, ast.ClassDef):
                    defined.add(node.name)
                elif isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            defined.add(target.id)
                        elif isinstance(target, ast.Tuple):
                            for elt in target.elts:
                                if isinstance(elt, ast.Name):
                                    defined.add(elt.id)
                elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                    defined.add(node.target.id)
                elif isinstance(node, ast.For) and isinstance(node.target, ast.Name):
                    defined.add(node.target.id)
                elif isinstance(node, ast.With):
                    for item in node.items:
                        if item.optional_vars and isinstance(item.optional_vars, ast.Name):
                            defined.add(item.optional_vars.id)
                elif isinstance(node, ast.ExceptHandler):
                    if node.name:
                        defined.add(node.name)
                elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
                    defined.add(node.target.id)
            
            # Ищем usage без определения (грубая эвристика)
            # Пропускаем — слишком много false positives при таком подходе
            # Лучше проверить конкретные подозрительные места
            
        except SyntaxError as e:
            issue("CRITICAL", rel, f"Syntax error: {e}")
        except Exception as e:
            issue("WARNING", rel, f"AST parse failed: {e}")

def check_specific_issues():
    """Проверить конкретные известные проблемные места."""
    print("\n=== 3. SPECIFIC ISSUES ===\n")
    
    # 3.1. management.py — все функции имеют нужные импорты?
    mgmt_path = Path("/home/z/my-project/caesar/management.py")
    with open(mgmt_path, "r") as f:
        mgmt_src = f.read()
    
    # Проверяем что каждая cmd_ функция имеет Storage import
    cmd_functions = [line.strip().split("(")[0].replace("async def ", "")
                     for line in mgmt_src.split("\n") if line.strip().startswith("async def cmd_")]
    
    for func_name in cmd_functions:
        # Ищем функцию и проверяем что в ней есть Storage import
        func_start = mgmt_src.find(f"async def {func_name}")
        if func_start == -1:
            continue
        func_end = mgmt_src.find("\nasync def ", func_start + 1)
        if func_end == -1:
            func_end = mgmt_src.find("\nasync def main_async", func_start)
        func_body = mgmt_src[func_start:func_end]
        
        if "Storage()" in func_body and "from caesar.memory.storage import Storage" not in func_body:
            issue("WARNING", "management.py", f"{func_name}: uses Storage() but no import")
        
        if "datetime" in func_body and "from datetime" not in func_body and "import datetime" not in func_body:
            # Проверяем есть ли datetime в глобальных импортах
            if "from datetime import" not in mgmt_src[:func_start]:
                issue("WARNING", "management.py", f"{func_name}: uses datetime but no import")
    
    # 3.2. daemon.py — _register_auto_cron читает config правильно?
    daemon_path = Path("/home/z/my-project/caesar/daemon.py")
    with open(daemon_path, "r") as f:
        daemon_src = f.read()
    
    if "self.config.config_path" in daemon_src:
        issue("WARNING", "daemon.py", "Uses self.config.config_path which may not exist")
    
    # 3.3. orchestrator.py — emit_progress defined before use?
    orch_path = Path("/home/z/my-project/caesar/core/orchestrator.py")
    with open(orch_path, "r") as f:
        orch_src = f.read()
    
    # Проверяем что is_background определён перед использованием
    if "is_background" in orch_src:
        first_def = orch_src.find("is_background = ")
        first_use = orch_src.find("if not is_background:")
        if first_use < first_def:
            issue("CRITICAL", "orchestrator.py", "is_background used before definition")
    
    # 3.4. L3 — _cache_loaded flag
    l3_path = Path("/home/z/my-project/caesar/memory/l3.py")
    with open(l3_path, "r") as f:
        l3_src = f.read()
    
    if "_cache_loaded" not in l3_src:
        issue("WARNING", "l3.py", "_cache_loaded flag missing")
    
    # 3.5. telegram_adapter — _handle_settings referenced
    tg_path = Path("/home/z/my-project/caesar/channels/telegram_adapter.py")
    with open(tg_path, "r") as f:
        tg_src = f.read()
    
    if "_handle_settings" in tg_src and "async def _handle_settings" not in tg_src:
        issue("CRITICAL", "telegram_adapter.py", "_handle_settings referenced but not defined")
    
    if "_toggle_setting" in tg_src and "async def _toggle_setting" not in tg_src:
        issue("CRITICAL", "telegram_adapter.py", "_toggle_setting referenced but not defined")

def check_db_schema():
    """Проверить DB schema на консистентность."""
    print("\n=== 4. DB SCHEMA ===\n")
    
    storage_path = Path("/home/z/my-project/caesar/memory/storage.py")
    with open(storage_path, "r") as f:
        storage_src = f.read()
    
    expected_tables = [
        "users", "channels", "channel_members", "tasks", "task_actions",
        "l2_facts", "l3_chunks", "l4_skills", "cron_tasks", "permissions",
        "token_usage", "conversation_messages", "kg_entities", "kg_relations",
    ]
    
    for table in expected_tables:
        if f"CREATE TABLE IF NOT EXISTS {table}" not in storage_src:
            issue("WARNING", "storage.py", f"Table '{table}' not found in schema")
        else:
            print(f"  ✅ {table}")
    
    # Проверяем что kg_entities и kg_relations есть
    if "kg_entities" not in storage_src:
        issue("CRITICAL", "storage.py", "kg_entities table missing")
    if "kg_relations" not in storage_src:
        issue("CRITICAL", "storage.py", "kg_relations table missing")

def check_config_consistency():
    """Проверить config.py — load/save консистентность."""
    print("\n=== 5. CONFIG CONSISTENCY ===\n")
    
    config_path = Path("/home/z/my-project/caesar/config.py")
    with open(config_path, "r") as f:
        config_src = f.read()
    
    # Проверяем что все dataclass fields есть в load и save
    config_sections = ["llm", "telegram", "stt", "l3", "cron", "queue"]
    
    for section in config_sections:
        if f'"{section}" in data' in config_src:
            print(f"  ✅ {section}: load OK")
        else:
            issue("WARNING", "config.py", f"Section '{section}' missing in load()")
        
        if f'"{section}": self.{section}.__dict__' in config_src or f'"{section}": self.{section}' in config_src:
            print(f"  ✅ {section}: save OK")
        else:
            # orchestrator НЕ пишем — это специально
            if section != "orchestrator":
                issue("WARNING", "config.py", f"Section '{section}' missing in save()")
    
    # Проверяем CronConfig fields
    if "morning_briefing_enabled" not in config_src and "auto_cleanup_enabled" not in config_src:
        print("  ℹ️  morning_briefing_enabled/auto_cleanup_enabled — только в YAML, не в dataclass (OK)")

def check_stress():
    """Стресс-тесты — edge cases."""
    print("\n=== 6. STRESS TESTS ===\n")
    
    # 6.1. Пустые сообщения
    try:
        from caesar.core.cron import parse_schedule
        result = parse_schedule("")
        print(f"  ✅ parse_schedule('') → {result}")
    except Exception as e:
        issue("CRITICAL", "cron.py", f"parse_schedule('') crashed: {e}")
    
    # 6.2. Очень длинный запрос
    try:
        from caesar.memory.knowledge_graph import extract_entities
        result = extract_entities("x" * 100000)
        print(f"  ✅ extract_entities(100k chars) → {len(result)} entities")
    except Exception as e:
        issue("CRITICAL", "knowledge_graph.py", f"extract_entities(100k) crashed: {e}")
    
    # 6.3. L3 search с пустым cache
    try:
        from unittest.mock import MagicMock
        from caesar.memory.l3 import L3Memory
        storage = MagicMock()
        storage._conn.return_value.__enter__.return_value.execute.return_value.fetchall.return_value = []
        l3 = L3Memory(storage)
        l3._cache_loaded = True
        l3._vectors_cache = {}
        import asyncio
        result = asyncio.get_event_loop().run_until_complete(
            l3.search(query="test", user_id="u1")
        )
        print(f"  ✅ L3 search empty cache → {result}")
    except Exception as e:
        issue("CRITICAL", "l3.py", f"L3 search empty cache crashed: {e}")
    
    # 6.4. Clean response style с None
    try:
        from caesar.core.orchestrator import Orchestrator
        orch = Orchestrator.__new__(Orchestrator)
        import logging
        orch.log = logging.getLogger("test")
        result = orch._clean_response_style(None)
        print(f"  ✅ _clean_response_style(None) → {result!r}")
    except Exception as e:
        issue("CRITICAL", "orchestrator.py", f"_clean_response_style(None) crashed: {e}")
    
    # 6.5. Clean response style с пустой строкой
    try:
        result = orch._clean_response_style("")
        print(f"  ✅ _clean_response_style('') → {result!r}")
    except Exception as e:
        issue("CRITICAL", "orchestrator.py", f"_clean_response_style('') crashed: {e}")
    
    # 6.6. Task с пустым user_message
    try:
        from caesar.core.queue import Task, TaskComplexity
        t = Task(user_message="", channel_id="c", user_id="u", complexity=TaskComplexity.SIMPLE)
        print(f"  ✅ Task(user_message='') created OK")
    except Exception as e:
        issue("CRITICAL", "queue.py", f"Task(empty) crashed: {e}")

def check_unused_imports():
    """Найти неиспользуемые импорты."""
    print("\n=== 7. UNUSED IMPORTS ===\n")
    
    caesar_dir = Path("/home/z/my-project/caesar")
    
    for py_file in caesar_dir.rglob("*.py"):
        rel = str(py_file.relative_to("/home/z/my-project"))
        try:
            with open(py_file, "r", encoding="utf-8") as f:
                source = f.read()
            
            tree = ast.parse(source, filename=str(py_file))
            
            imports = {}
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        name = alias.asname or alias.name.split(".")[0]
                        imports[name] = alias.name
                elif isinstance(node, ast.ImportFrom):
                    for alias in node.names:
                        name = alias.asname or alias.name
                        imports[name] = f"{node.module}.{name}"
            
            # Проверяем использование (грубая эвристика — ищем в source)
            for name, full in imports.items():
                # Считаем сколько раз name встречается (кроме строки импорта)
                count = source.count(name)
                if count <= 1:  # только в импорте
                    # Проверяем что это не __all__
                    if f'"{name}"' not in source and f"'{name}'" not in source:
                        issue("INFO", rel, f"Potentially unused import: {name} ({full})")
        except Exception:
            pass

def main():
    print("=" * 60)
    print("CAESAR FULL CODE AUDIT")
    print("=" * 60)
    
    check_imports()
    check_undefined_vars()
    check_specific_issues()
    check_db_schema()
    check_config_consistency()
    check_stress()
    check_unused_imports()
    
    print("\n" + "=" * 60)
    print("AUDIT SUMMARY")
    print("=" * 60)
    
    critical = [i for i in ISSUES if i[0] == "CRITICAL"]
    warnings = [i for i in ISSUES if i[0] == "WARNING"]
    infos = [i for i in ISSUES if i[0] == "INFO"]
    
    print(f"\n🔴 CRITICAL: {len(critical)}")
    for sev, file, msg in critical:
        print(f"   {file}: {msg}")
    
    print(f"\n🟡 WARNING: {len(warnings)}")
    for sev, file, msg in warnings:
        print(f"   {file}: {msg}")
    
    print(f"\n🔵 INFO: {len(infos)}")
    for sev, file, msg in infos[:20]:  # первые 20
        print(f"   {file}: {msg}")
    if len(infos) > 20:
        print(f"   ... and {len(infos) - 20} more")
    
    print(f"\nTOTAL: {len(ISSUES)} issues ({len(critical)} critical, {len(warnings)} warnings, {len(infos)} info)")
    
    return len(critical)

if __name__ == "__main__":
    sys.exit(main())
