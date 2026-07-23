"""Инструменты категории 1: Shell + Files.

См. roadmap раздел 11.2.
"""

import asyncio
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from caesar.tools.base import Tool, ToolResult, is_dangerous_command, references_secret_path


class ShellExecTool(Tool):
    """Выполнить shell-команду."""
    
    name = "shell_exec"
    description = (
        "Выполнить команду в shell (bash). Возвращает stdout, stderr, exit_code. "
        "Используй для: ls, grep, systemctl, find, curl, и т.д. "
        "В full mode (config.mode=full) выполняет ЛЮБЫЕ команды без ограничений."
    )
    category = "shell_files"
    access_mode: str = "sandboxed"  # устанавливается ToolRegistry при регистрации
    god_mode: bool = False  # устанавливается orchestrator-ом при выполнении
    parameters_schema = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Команда для выполнения",
            },
            "timeout": {
                "type": "integer",
                "description": "Таймаут в секундах (макс 300)",
                "default": 30,
            },
            "cwd": {
                "type": "string",
                "description": "Рабочая директория",
            },
        },
        "required": ["command"],
    }
    
    async def execute(self, command: str, timeout: int = 30, cwd: str | None = None, **_) -> ToolResult:
        # exact_deny (снос ЛОКАЛЬНОЙ системы: rm -rf /, fork bomb, chmod/chown -R /,
        # disable sshd/networking, self-uninstall pip/npm) — ВСЕГДА, даже в
        # full/god_mode. Единственное, что ограничивает владельца, и стоит 0 в
        # реальных возможностях (агенту это никогда не нужно).
        # Форматирование диска (mkfs/dd of=/dev) и снос на СОСЕДНЕЙ машине
        # (remote_exec) — разрешены (не в списке; в sandboxed спросит requires_permission).
        is_dangerous, pattern = is_dangerous_command(command)
        if is_dangerous:
            return ToolResult(
                success=False,
                error=f"BLOCKED (exact_deny): снос локальной системы. {pattern}. "
                      f"Не отключается даже в god/full. "
                      f"На удалённой машине — используй remote_exec.",
            )
        # Эксфильтрация секретов (~/.ssh, config с ключами, /etc/shadow, .env) —
        # мягкий гейт, только sandboxed (god/full обходит: владелец берёт ответственность).
        if self.access_mode != "full" and not self.god_mode:
            if references_secret_path(command):
                return ToolResult(
                    success=False,
                    error="BLOCKED (sandboxed): команда обращается к секретному пути "
                          "(~/.ssh, ~/.config/caesar, /etc/shadow, .env). "
                          "В god/full — можно.",
                )
        
        # Проверка таймаута
        timeout = min(max(timeout, 1), 300)
        
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env={**os.environ, "TERM": "dumb"},  # не интерактивный
            )
            
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return ToolResult(
                    success=False,
                    error=f"Timeout after {timeout}s",
                    data={"timeout": True},
                )
            
            stdout_text = stdout.decode("utf-8", errors="replace")
            stderr_text = stderr.decode("utf-8", errors="replace")
            
            # Обрезаем вывод
            if len(stdout_text) > 50000:
                stdout_text = stdout_text[:50000] + f"\n... (truncated, {len(stdout_text)} total)"
            if len(stderr_text) > 50000:
                stderr_text = stderr_text[:50000] + f"\n... (truncated)"
            
            success = proc.returncode == 0
            return ToolResult(
                success=success,
                data={
                    "stdout": stdout_text,
                    "stderr": stderr_text,
                    "exit_code": proc.returncode,
                    "duration_sec": 0,  # TODO: измерить
                },
                error=None if success else f"Exit code {proc.returncode}",
            )
        
        except Exception as e:
            return ToolResult(success=False, error=str(e))
    
    def requires_permission(self, command: str = "", **_) -> bool:
        """Опасные команды требуют подтверждения."""
        # Запретные уже заблокированы, проверяем только "потенциально опасные"
        cmd = command.strip()
        dangerous_patterns = [
            r"\brm\s+-rf?\b",
            r"\bsudo\b",
            r"\bchmod\s+-R\b",
            r"\bchown\s+-R\b",
            r"\bmkfs\b",
            r"\bdd\s+if=",
            r">\s*/dev/",
            # systemctl restart/stop/disable — ЛОВИТ 'systemctl restart' И
            # 'systemctl --user restart' И 'systemctl --user stop caesar-daemon'
            r"\bsystemctl\b.*\b(restart|stop|disable)\b",
            r"\bapt\s+(remove|purge)\b",
            r"\bpip\s+uninstall\b",
            # Дополнительно: kill/pkill процессов caesar
            r"\bp?kill\b.*\bcaesar\b",
            # shutdown/reboot/halt
            r"\bshutdown\b",
            r"\breboot\b",
            r"\bhalt\b",
        ]
        for pattern in dangerous_patterns:
            if re.search(pattern, cmd):
                return True
        return False


class ReadFileTool(Tool):
    """Прочитать файл."""
    
    name = "read_file"
    description = "Прочитать текстовый файл. Можно указать диапазон строк. Максимум 2000 строк или 100KB за вызов."
    category = "shell_files"
    access_mode: str = "sandboxed"  # устанавливается ToolRegistry
    god_mode: bool = False  # устанавливается orchestrator-ом
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Путь к файлу"},
            "start": {"type": "integer", "description": "Начальная строка (1-индексация)", "default": 1},
            "end": {"type": "integer", "description": "Конечная строка (None = до конца)"},
        },
        "required": ["path"],
    }
    
    async def execute(self, path: str, start: int = 1, end: int | None = None, **_) -> ToolResult:
        # В sandboxed не даём читать секреты (~/.ssh, config с ключами, /etc/shadow).
        if self.access_mode != "full" and not self.god_mode and references_secret_path(path):
            return ToolResult(
                success=False,
                error="BLOCKED (sandboxed): чтение секретного пути запрещено. В god/full — можно.",
            )
        try:
            p = Path(path).expanduser()
            if not p.exists():
                return ToolResult(success=False, error=f"File not found: {path}")
            if not p.is_file():
                return ToolResult(success=False, error=f"Not a file: {path}")
            if p.stat().st_size > 10 * 1024 * 1024:  # 10 MB
                return ToolResult(
                    success=False,
                    error=f"File too large ({p.stat().st_size} bytes). Use shell_exec with head/tail.",
                )
            
            # Проверяем бинарный файл
            with open(p, "rb") as f:
                first_chunk = f.read(1024)
                if b"\x00" in first_chunk:
                    return ToolResult(
                        success=False,
                        error=f"Binary file. Use parse_pdf/parse_docx/parse_xlsx for documents.",
                    )
            
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            
            total_lines = len(lines)
            start_idx = max(start - 1, 0)
            end_idx = end if end else total_lines
            selected = lines[start_idx:end_idx]
            
            content = "".join(selected)
            if len(content) > 100 * 1024:  # 100 KB
                content = content[:100 * 1024] + "\n... (truncated)"
            
            return ToolResult(
                success=True,
                data={
                    "content": content,
                    "lines_total": total_lines,
                    "lines_shown": len(selected),
                    "bytes_total": p.stat().st_size,
                },
            )
        except Exception as e:
            return ToolResult(success=False, error=str(e))


class WriteFileTool(Tool):
    """Записать файл."""
    
    name = "write_file"
    description = "Записать содержимое в файл. Если файл существует — перезаписывает (с backup)."
    category = "shell_files"
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Путь к файлу"},
            "content": {"type": "string", "description": "Содержимое"},
            "backup": {"type": "boolean", "default": True},
            "create_dirs": {"type": "boolean", "default": True},
        },
        "required": ["path", "content"],
    }
    
    async def execute(
        self,
        path: str,
        content: str,
        backup: bool = True,
        create_dirs: bool = True,
        **_,
    ) -> ToolResult:
        try:
            p = Path(path).expanduser()
            
            if create_dirs:
                p.parent.mkdir(parents=True, exist_ok=True)
            
            backup_path = None
            if backup and p.exists() and "/tmp/" not in str(p):
                backup_path = str(p) + ".bak"
                shutil.copy2(p, backup_path)
            
            with open(p, "w", encoding="utf-8") as f:
                f.write(content)
            
            return ToolResult(
                success=True,
                data={
                    "bytes_written": len(content.encode("utf-8")),
                    "backup_path": backup_path,
                },
            )
        except Exception as e:
            return ToolResult(success=False, error=str(e))
    
    def requires_permission(self, path: str = "", **_) -> bool:
        """Запись в системные папки требует подтверждения."""
        dangerous_paths = ["/etc/", "/var/", "/usr/", "/boot/", "/sys/", "/proc/"]
        return any(path.startswith(p) for p in dangerous_paths)


class EditFileTool(Tool):
    """Точечно отредактировать файл."""
    
    name = "edit_file"
    description = (
        "Заменить old_str на new_str в файле. old_str должен встречаться ровно 1 раз. "
        "Экономит токены: не нужно тащить весь файл в контекст."
    )
    category = "shell_files"
    parameters_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Путь к файлу"},
            "old_str": {"type": "string", "description": "Что заменить (должно быть уникально)"},
            "new_str": {"type": "string", "description": "На что заменить"},
            "create_if_missing": {"type": "boolean", "default": False},
        },
        "required": ["path", "old_str", "new_str"],
    }
    
    async def execute(
        self,
        path: str,
        old_str: str,
        new_str: str,
        create_if_missing: bool = False,
        **_,
    ) -> ToolResult:
        try:
            p = Path(path).expanduser()
            
            if not p.exists():
                if create_if_missing:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(new_str, encoding="utf-8")
                    return ToolResult(
                        success=True,
                        data={"matches_found": 0, "created": True, "backup_path": None},
                    )
                return ToolResult(success=False, error=f"File not found: {path}")
            
            content = p.read_text(encoding="utf-8")
            matches = content.count(old_str)
            
            if matches == 0:
                return ToolResult(
                    success=False,
                    error=f"old_str not found in {path}",
                )
            if matches > 1:
                return ToolResult(
                    success=False,
                    error=f"old_str found {matches} times in {path}. Must be unique. Use more context.",
                )
            
            # Backup
            backup_path = str(p) + ".bak"
            shutil.copy2(p, backup_path)
            
            # Замена
            new_content = content.replace(old_str, new_str, 1)
            p.write_text(new_content, encoding="utf-8")
            
            return ToolResult(
                success=True,
                data={
                    "matches_found": matches,
                    "backup_path": backup_path,
                },
            )
        except Exception as e:
            return ToolResult(success=False, error=str(e))


class FindFilesTool(Tool):
    """Найти файлы по шаблону."""
    
    name = "find_files"
    description = "Найти файлы по glob-паттерну. Возвращает список путей."
    category = "shell_files"
    parameters_schema = {
        "type": "object",
        "properties": {
            "directory": {"type": "string", "description": "Директория поиска"},
            "pattern": {"type": "string", "default": "*", "description": "Glob паттерн"},
            "recursive": {"type": "boolean", "default": True},
            "max_results": {"type": "integer", "default": 100},
        },
        "required": ["directory"],
    }
    
    async def execute(
        self,
        directory: str,
        pattern: str = "*",
        recursive: bool = True,
        max_results: int = 100,
        **_,
    ) -> ToolResult:
        try:
            d = Path(directory).expanduser()
            if not d.exists():
                return ToolResult(success=False, error=f"Directory not found: {directory}")
            if not d.is_dir():
                return ToolResult(success=False, error=f"Not a directory: {directory}")
            
            if recursive:
                glob_pattern = f"**/{pattern}"
            else:
                glob_pattern = pattern
            
            files = []
            for p in d.glob(glob_pattern):
                if p.is_file() and not p.name.startswith("."):
                    files.append({
                        "path": str(p),
                        "size_bytes": p.stat().st_size,
                        "modified": p.stat().st_mtime,
                    })
                    if len(files) >= max_results:
                        break
            
            return ToolResult(
                success=True,
                data={
                    "files": files,
                    "total_found": len(files),
                    "truncated": len(files) >= max_results,
                },
            )
        except Exception as e:
            return ToolResult(success=False, error=str(e))


class GrepTool(Tool):
    """Поиск по содержимому файлов."""
    
    name = "grep"
    description = "Поиск по содержимому файлов. Использует ripgrep если доступен."
    category = "shell_files"
    parameters_schema = {
        "type": "object",
        "properties": {
            "directory": {"type": "string"},
            "pattern": {"type": "string", "description": "Regex паттерн"},
            "file_pattern": {"type": "string", "default": "*"},
            "case_insensitive": {"type": "boolean", "default": False},
            "context_lines": {"type": "integer", "default": 2},
            "max_matches": {"type": "integer", "default": 50},
        },
        "required": ["directory", "pattern"],
    }
    
    async def execute(
        self,
        directory: str,
        pattern: str,
        file_pattern: str = "*",
        case_insensitive: bool = False,
        context_lines: int = 2,
        max_matches: int = 50,
        **_,
    ) -> ToolResult:
        try:
            # Используем rg если доступен, иначе Python
            rg_path = shutil.which("rg")
            
            if rg_path:
                cmd = [
                    rg_path,
                    "--json",
                    "--max-count", str(max_matches),
                    "-C", str(context_lines),
                ]
                if case_insensitive:
                    cmd.append("-i")
                cmd.extend(["-g", file_pattern])
                cmd.extend([pattern, directory])
                
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await proc.communicate()
                
                # Парсим JSON output rg
                matches = []
                for line in stdout.decode("utf-8", errors="replace").splitlines():
                    try:
                        import json
                        data = json.loads(line)
                        if data.get("type") == "match":
                            d = data.get("data", {})
                            matches.append({
                                "file": d.get("path", {}).get("text", ""),
                                "line_number": d.get("line_number", 0),
                                "line": d.get("lines", {}).get("text", "").strip(),
                            })
                    except (json.JSONDecodeError, KeyError):
                        continue
                
                return ToolResult(
                    success=True,
                    data={
                        "matches": matches[:max_matches],
                        "total_matches": len(matches),
                    },
                )
            
            # Fallback на Python
            regex = re.compile(pattern, re.IGNORECASE if case_insensitive else 0)
            matches = []
            d = Path(directory).expanduser()
            
            for p in d.rglob(file_pattern if "*" in file_pattern else f"**/{file_pattern}"):
                if not p.is_file() or p.name.startswith("."):
                    continue
                try:
                    with open(p, "r", encoding="utf-8", errors="replace") as f:
                        for i, line in enumerate(f, 1):
                            if regex.search(line):
                                matches.append({
                                    "file": str(p),
                                    "line_number": i,
                                    "line": line.strip(),
                                })
                                if len(matches) >= max_matches:
                                    return ToolResult(
                                        success=True,
                                        data={"matches": matches, "total_matches": len(matches)},
                                    )
                except (PermissionError, UnicodeDecodeError):
                    continue
            
            return ToolResult(
                success=True,
                data={"matches": matches, "total_matches": len(matches)},
            )
        except Exception as e:
            return ToolResult(success=False, error=str(e))


class RemoteExecTool(Tool):
    """Выполнить команду на соседней машине через SSH.

    В god mode: агент может чинить/диагностировать другие машины по сети (как
    OpenClaw чинил Caesar с соседней машины). Если есть SSH-ключ — использует
    его. Если ключа нет — СПРАШИВАЕТ у пользователя пароль (через чат) и ходит
    по паролю (paramiko — пароль не светится в `ps`, в отличие от sshpass -p).
    """

    name = "remote_exec"
    description = (
        "Выполнить команду на удалённой машине через SSH. Если есть SSH-ключ — "
        "не передавай password. Если ключа НЕТ — спроси у пользователя пароль от "
        "машины и передай в password. Возвращает stdout/stderr/exit_code. "
        "В sandboxed запрещён; в god/full — выполняет. Пароль не логируется."
    )
    category = "shell_files"
    access_mode: str = "sandboxed"
    god_mode: bool = False
    parameters_schema = {
        "type": "object",
        "properties": {
            "host": {"type": "string", "description": "Хост (ip или имя)"},
            "command": {"type": "string", "description": "Команда на удалённой машине"},
            "user": {"type": "string", "description": "SSH user (по умолч. текущий)", "default": ""},
            "password": {"type": "string", "description": "Пароль (только если нет SSH-ключа; спроси у пользователя)", "default": ""},
            "timeout": {"type": "integer", "description": "Таймаут сек (макс 300)", "default": 60},
        },
        "required": ["host", "command"],
    }

    async def execute(self, host: str, command: str, user: str = "",
                      password: str = "", timeout: int = 60, **_) -> ToolResult:
        if not host or not command:
            return ToolResult(success=False, error="host и command обязательны")
        # В sandboxed (без god) — не выполняем удалённые команды.
        if self.access_mode != "full" and not self.god_mode:
            return ToolResult(
                success=False,
                error="remote_exec требует god mode ('газ в пол') или mode: full. "
                      "В sandboxed удалённое выполнение запрещено.",
            )
        timeout = min(max(timeout, 1), 300)
        try:
            if password:
                # password-SSH через paramiko (в потоке — не блокирует event loop).
                # Пароль НЕ логируется и не попадает в process args (в отличие от sshpass -p).
                rc, out, err = await asyncio.to_thread(
                    _paramiko_run, host, user, password, command, timeout
                )
            else:
                # key-based через subprocess ssh.
                target = f"{user}@{host}" if user else host
                ssh_cmd = [
                    "ssh",
                    "-o", "BatchMode=yes",                       # без интерактивного пароля
                    "-o", "StrictHostKeyChecking=accept-new",
                    "-o", f"ConnectTimeout={min(timeout, 15)}",
                    target,
                    command,
                ]
                proc = await asyncio.create_subprocess_exec(
                    *ssh_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
                    return ToolResult(success=False, error=f"Timeout after {timeout}s",
                                       data={"timeout": True, "host": host})
                out = stdout.decode("utf-8", errors="replace")
                err = stderr.decode("utf-8", errors="replace")
                rc = proc.returncode
            if len(out) > 50000:
                out = out[:50000] + f"\n... (truncated, {len(out)} total)"
            return ToolResult(
                success=rc == 0,
                data={"stdout": out, "stderr": err, "exit_code": rc,
                      "host": host, "user": user or None},
                error=None if rc == 0 else f"Exit code {rc}",
            )
        except ImportError:
            return ToolResult(success=False, error="password-SSH требует paramiko: pip install paramiko")
        except Exception as e:
            # Пароль может оказаться в сообщении исключения — маскируем его.
            msg = str(e)
            if password and password in msg:
                msg = msg.replace(password, "***")
            return ToolResult(success=False, error=f"remote_exec failed: {type(e).__name__}: {msg[:200]}")

    def requires_permission(self, host: str = "", command: str = "", **_) -> bool:
        # удалённое выполнение всегда потенциально опасно — в sandboxed спросим
        return True


def _paramiko_run(host: str, username: str, password: str, command: str, timeout: int) -> tuple[int, str, str]:
    """Синхронный SSH с паролем через paramiko. Запускается в потоке (asyncio.to_thread)."""
    import paramiko
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=host,
        username=username or None,
        password=password,
        timeout=min(timeout, 15),
        look_for_keys=False,
        allow_agent=False,
    )
    try:
        _stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        rc = stdout.channel.recv_exit_status()
        return rc, out, err
    finally:
        client.close()


class NetworkScanTool(Tool):
    """Быстрый поиск машин в сети (ip neigh/arp + nmap -sn)."""

    name = "network_scan"
    description = (
        "Быстро найти машины в локальной сети. Возвращает ip neigh/arp (известные хосты) "
        "+ опц. nmap -sn (ping-sweep по подсети). Используй ЭТО для discovery, а не "
        "медленный nmap-портскан. Если уже знаешь хост и есть креды — подключайся "
        "remote_exec, НЕ сканируй сеть заново."
    )
    category = "shell_files"
    parameters_schema = {
        "type": "object",
        "properties": {
            "subnet": {"type": "string", "description": "Подсеть для nmap -sn, напр. 10.42.0.0/24 (опц.; без неё — только ip neigh/arp)", "default": ""},
            "timeout": {"type": "integer", "description": "Таймаут сек (макс 120)", "default": 30},
        },
    }

    async def execute(self, subnet: str = "", timeout: int = 30, **_) -> ToolResult:
        # Валидация подсети — первой, чтобы инъекция отбивалась без subprocess.
        if subnet and not re.fullmatch(r"[0-9./]+", subnet):
            return ToolResult(success=False, error=f"invalid subnet: {subnet!r}")
        timeout = min(max(timeout, 1), 120)
        results: dict = {}

        # 1) ARP-таблица — мгновенно, известные хосты (без сканирования).
        try:
            proc = await asyncio.create_subprocess_shell(
                "(ip neigh || arp -a) 2>/dev/null",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            results["arp"] = out.decode("utf-8", errors="replace")
        except Exception as e:
            results["arp_error"] = str(e)

        # 2) nmap -sn (ping-sweep) если задана валидная подсеть.
        if subnet:
            try:
                proc = await asyncio.create_subprocess_shell(
                    f"nmap -sn -T4 --max-retries 1 {subnet}",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
                results["nmap_sn"] = out.decode("utf-8", errors="replace")
            except Exception as e:
                results["nmap_sn_error"] = str(e)

        return ToolResult(
            success=True,
            data={"scan": results, "hint": "arp = известные хосты; nmap -sn = ping-sweep по подсети"},
        )


def get_shell_files_tools() -> list[Tool]:
    """Все инструменты категории 1."""
    return [
        ShellExecTool(),
        RemoteExecTool(),
        NetworkScanTool(),
        ReadFileTool(),
        WriteFileTool(),
        EditFileTool(),
        FindFilesTool(),
        GrepTool(),
    ]
