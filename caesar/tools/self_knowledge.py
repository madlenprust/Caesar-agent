"""Инструменты категории 7: Self-knowledge.

См. roadmap раздел 11.8.

self_read: читать свои .py файлы и /etc/agent/self/ документы
self_edit: редактировать свой код (только в автономном/полном режимах)
self_install_package: ставить pip-пакеты в venv
self_scan: обновить CODE_MAP.md
self_test: прогнать тесты
"""

import asyncio
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from caesar.config import SELF_DIR, CODE_DIR
from caesar.tools.base import Tool, ToolResult


class SelfReadTool(Tool):
    """Читать свой код/документацию."""
    
    name = "self_read"
    description = (
        "Прочитать собственный код или документацию агента. "
        "Используй для понимания как ты устроен. "
        "Файлы: ARCHITECTURE.md, CODE_MAP.md, PRINCIPLES.md, ROADMAP.md, CHANGELOG.md, "
        "или любой .py файл из agent/."
    )
    category = "self_knowledge"
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Имя файла или путь (например: ARCHITECTURE.md, tools/shell_files.py)"},
            "section": {"type": "string", "description": "Конкретная секция для .md"},
        },
        "required": ["path"],
    }
    
    async def execute(self, path: str, section: str | None = None, **_) -> ToolResult:
        # Сначала ищем в SELF_DIR (документы)
        p = SELF_DIR / path
        if not p.exists():
            # Потом в CODE_DIR (исходники)
            p = CODE_DIR / path
        if not p.exists():
            # Может быть абсолютный путь
            p = Path(path).expanduser()
        
        if not p.exists():
            return ToolResult(success=False, error=f"File not found: {path}")
        if not p.is_file():
            return ToolResult(success=False, error=f"Not a file: {path}")
        
        try:
            content = p.read_text(encoding="utf-8", errors="replace")
            
            if section and p.suffix == ".md":
                # Извлекаем секцию
                # Ищем заголовок # или ## с этим именем
                pattern = re.compile(
                    rf"(^{'#' * 1,6}\s+{re.escape(section)}.*?)(?=^{'#' * 1,6}\s|\Z)",
                    re.MULTILINE | re.DOTALL,
                )
                m = pattern.search(content)
                if m:
                    content = m.group(1)
            
            # Обрезаем
            truncated = False
            if len(content) > 100 * 1024:
                content = content[:100 * 1024] + "\n... (truncated)"
                truncated = True
            
            return ToolResult(
                success=True,
                data={
                    "content": content,
                    "file_path": str(p),
                    "exists": True,
                    "truncated": truncated,
                },
            )
        except Exception as e:
            return ToolResult(success=False, error=str(e))


class SelfEditTool(Tool):
    """Редактировать свой код (только в автономном/полном режимах)."""
    
    name = "self_edit"
    description = (
        "Редактировать собственный .py файл или .md документ. "
        "PRINCIPLES.md — заблокирован всегда. "
        "Только в автономном или полном режиме доступа."
    )
    category = "self_knowledge"
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old_str": {"type": "string"},
            "new_str": {"type": "string"},
            "reason": {"type": "string", "description": "Обязательное — зачем меняешь"},
        },
        "required": ["path", "old_str", "new_str", "reason"],
    }
    
    BLOCKED_FILES = ["PRINCIPLES.md"]
    
    def __init__(self, access_mode: str = "sandboxed"):
        super().__init__()
        self.access_mode = access_mode  # sandboxed | autonomous | full
    
    async def execute(
        self,
        path: str,
        old_str: str,
        new_str: str,
        reason: str,
        **_,
    ) -> ToolResult:
        if self.access_mode == "sandboxed":
            return ToolResult(
                success=False,
                error="self_edit не доступен в обычном режиме. Переключи на автономный через `agent setup --mode autonomous`.",
            )
        
        # Проверка заблокированных файлов
        filename = Path(path).name
        if filename in self.BLOCKED_FILES:
            return ToolResult(
                success=False,
                error=f"Файл {filename} заблокирован от редактирования.",
            )
        
        # Ищем файл
        p = SELF_DIR / path
        if not p.exists():
            p = CODE_DIR / path
        if not p.exists():
            p = Path(path).expanduser()
        
        if not p.exists():
            return ToolResult(success=False, error=f"File not found: {path}")
        
        try:
            content = p.read_text(encoding="utf-8")
            matches = content.count(old_str)
            
            if matches == 0:
                return ToolResult(success=False, error="old_str not found")
            if matches > 1:
                return ToolResult(success=False, error=f"old_str found {matches} times, must be unique")
            
            # Backup (через git если есть, иначе .bak)
            import shutil
            backup_path = str(p) + ".bak"
            shutil.copy2(p, backup_path)
            
            # Замена
            new_content = content.replace(old_str, new_str, 1)
            p.write_text(new_content, encoding="utf-8")
            
            # Пытаемся git commit
            try:
                subprocess.run(
                    ["git", "add", str(p)],
                    cwd=str(p.parent),
                    capture_output=True,
                    timeout=5,
                )
                subprocess.run(
                    ["git", "commit", "-m", f"self_edit: {reason[:100]}"],
                    cwd=str(p.parent),
                    capture_output=True,
                    timeout=5,
                )
            except Exception:
                pass  # git может быть не настроен
            
            # Для .py — проверяем что импорт работает
            validation_passed = True
            validation_errors = []
            if p.suffix == ".py":
                try:
                    # Простой синтаксический чек
                    result = subprocess.run(
                        [sys.executable, "-c", f"import ast; ast.parse(open('{p}').read())"],
                        capture_output=True,
                        timeout=5,
                    )
                    if result.returncode != 0:
                        validation_passed = False
                        validation_errors.append(result.stderr.decode())
                except Exception as e:
                    validation_passed = False
                    validation_errors.append(str(e))
            
            return ToolResult(
                success=validation_passed,
                data={
                    "matches_found": matches,
                    "backup_path": backup_path,
                    "validation_passed": validation_passed,
                    "validation_errors": validation_errors,
                    "reason": reason,
                },
                error=None if validation_passed else "Validation failed, but file was edited. Rollback with .bak",
            )
        except Exception as e:
            return ToolResult(success=False, error=str(e))
    
    def requires_permission(self, path: str = "", **_) -> bool:
        """self_edit всегда требует подтверждения в автономном режиме."""
        return self.access_mode != "full"


class SelfInstallPackageTool(Tool):
    """Установить pip-пакет."""
    
    name = "self_install_package"
    description = "Установить Python-пакет в venv агента. Не может менять pip/setuptools/wheel."
    category = "self_knowledge"
    parameters_schema = {
        "type": "object",
        "properties": {
            "package": {"type": "string", "description": "Имя пакета (например: feedparser)"},
            "reason": {"type": "string"},
        },
        "required": ["package", "reason"],
    }
    
    BLOCKED_PACKAGES = {"pip", "setuptools", "wheel"}
    
    async def execute(self, package: str, reason: str, **_) -> ToolResult:
        # Проверка имени пакета
        pkg_name = package.split("=")[0].split(">")[0].split("<")[0].strip().lower()
        if pkg_name in self.BLOCKED_PACKAGES:
            return ToolResult(
                success=False,
                error=f"Нельзя менять {pkg_name}",
            )
        
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "pip", "install", package,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
            
            if proc.returncode != 0:
                return ToolResult(
                    success=False,
                    error=f"pip install failed: {stderr.decode()[:500]}",
                )
            
            return ToolResult(
                success=True,
                data={
                    "installed": package,
                    "pip_output": stdout.decode()[:1000],
                },
            )
        except asyncio.TimeoutError:
            return ToolResult(success=False, error="pip install timeout (120s)")
        except Exception as e:
            return ToolResult(success=False, error=str(e))


class SelfScanTool(Tool):
    """Обновить CODE_MAP.md через AST-парсинг."""
    
    name = "self_scan"
    description = "Пересканировать .py файлы агента, обновить CODE_MAP.md."
    category = "self_knowledge"
    parameters_schema = {"type": "object", "properties": {}}
    
    async def execute(self, **_) -> ToolResult:
        try:
            import ast
            files_scanned = 0
            classes_found = 0
            functions_found = 0
            
            output_lines = ["# Карта кода (авто-генерация)\n"]
            
            for py_file in CODE_DIR.rglob("*.py"):
                if "__pycache__" in str(py_file):
                    continue
                files_scanned += 1
                rel_path = py_file.relative_to(CODE_DIR.parent)
                output_lines.append(f"\n## {rel_path}\n")
                
                try:
                    tree = ast.parse(py_file.read_text(encoding="utf-8"))
                    for node in ast.walk(tree):
                        if isinstance(node, ast.ClassDef):
                            classes_found += 1
                            methods = [
                                n.name for n in node.body
                                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                            ]
                            output_lines.append(f"- class {node.name}")
                            if methods:
                                for m in methods:
                                    output_lines.append(f"  - {m}()")
                        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            if not isinstance(getattr(node, 'parent', None), ast.ClassDef):
                                functions_found += 1
                                output_lines.append(f"- function {node.name}()")
                except SyntaxError:
                    output_lines.append(f"  [parse error]")
            
            # Записываем
            SELF_DIR.mkdir(parents=True, exist_ok=True)
            code_map_path = SELF_DIR / "CODE_MAP.md"
            code_map_path.write_text("\n".join(output_lines), encoding="utf-8")
            
            return ToolResult(
                success=True,
                data={
                    "files_scanned": files_scanned,
                    "classes_found": classes_found,
                    "functions_found": functions_found,
                    "code_map_path": str(code_map_path),
                    "code_map_updated": True,
                },
            )
        except Exception as e:
            return ToolResult(success=False, error=str(e))


class SelfTestTool(Tool):
    """Прогнать тесты агента."""
    
    name = "self_test"
    description = "Прогнать pytest тесты агента."
    category = "self_knowledge"
    parameters_schema = {
        "type": "object",
        "properties": {
            "module": {"type": "string", "description": "Конкретный модуль или все"},
            "verbose": {"type": "boolean", "default": False},
        },
    }
    
    async def execute(self, module: str | None = None, verbose: bool = False, **_) -> ToolResult:
        try:
            cmd = [sys.executable, "-m", "pytest", str(CODE_DIR.parent / "tests")]
            if module:
                cmd.append(module)
            if verbose:
                cmd.append("-v")
            
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
            
            # Парсим pytest output
            output = stdout.decode() + stderr.decode()
            passed = len(re.findall(r"\bPASSED\b|\bpassed\b", output))
            failed = len(re.findall(r"\bFAILED\b|\bfailed\b", output))
            
            return ToolResult(
                success=proc.returncode == 0,
                data={
                    "tests_run": passed + failed,
                    "tests_passed": passed,
                    "tests_failed": failed,
                    "output": output[:5000],
                },
                error=None if proc.returncode == 0 else f"{failed} tests failed",
            )
        except asyncio.TimeoutError:
            return ToolResult(success=False, error="pytest timeout")
        except Exception as e:
            return ToolResult(success=False, error=str(e))


def get_self_knowledge_tools(access_mode: str = "sandboxed") -> list[Tool]:
    return [
        SelfReadTool(),
        SelfEditTool(access_mode),
        SelfInstallPackageTool(),
        SelfScanTool(),
        SelfTestTool(),
    ]
