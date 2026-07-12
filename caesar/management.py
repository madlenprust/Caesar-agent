"""CLI-команды управления агентом.

caesar setup — setup wizard
caesar update — обновить до новой версии
caesar rollback — откатиться к предыдущей версии
caesar uninstall — удалить агента
caesar permissions — управление whitelist
caesar stats — статистика по токенам
caesar enable <feature> — включить фичу одной командой (ставит dep + правит config + рестарт)
"""

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from caesar.config import CONFIG_PATH, CODE_DIR, DATA_DIR, DB_PATH
from caesar.logging_setup import setup_logging, get_logger


def _systemd_user_active() -> bool:
    """Проверить есть ли systemd user service caesar-daemon."""
    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-enabled", "caesar-daemon"],
            capture_output=True, text=True, timeout=3,
        )
        return result.returncode == 0
    except Exception:
        return False


def _restart_daemon() -> bool:
    """Перезапустить daemon. Возвращает True если получилось."""
    # Пробуем через systemd
    try:
        result = subprocess.run(
            ["systemctl", "--user", "restart", "caesar-daemon"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            time.sleep(2)
            # Проверяем что реально запустился
            check = subprocess.run(
                ["systemctl", "--user", "is-active", "caesar-daemon"],
                capture_output=True, text=True, timeout=3,
            )
            if check.stdout.strip() == "active":
                return True
    except Exception:
        pass
    
    # Fallback: pkill + Popen
    subprocess.run(["pkill", "-f", "caesar.daemon"], capture_output=True)
    time.sleep(1)
    
    from caesar.config import CODE_DIR
    subprocess.Popen(
        ["python3", "-m", "caesar.daemon"],
        cwd=str(CODE_DIR.parent),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    time.sleep(3)
    
    result = subprocess.run(["pgrep", "-f", "caesar.daemon"], capture_output=True)
    return result.returncode == 0


def _stop_daemon() -> None:
    """Остановить daemon (force — для rollback/uninstall)."""
    try:
        subprocess.run(
            ["systemctl", "--user", "stop", "caesar-daemon"],
            capture_output=True, timeout=10,
        )
    except Exception:
        pass
    subprocess.run(["pkill", "-f", "caesar.daemon"], capture_output=True)
    time.sleep(1)


async def _stop_daemon_graceful(timeout: int = 180) -> None:
    """Остановить daemon gracefully — подождать активные задачи.
    
    1. Запрашиваем у daemon активные задачи через socket API
    2. Если есть — ждём их завершения (poll каждые 5 сек)
    3. Потом systemctl stop (daemon сам сделает graceful shutdown)
    
    Args:
        timeout: сколько максимум ждать (секунды)
    """
    from caesar.config import SOCKET_PATH
    
    # Проверяем активные задачи через socket
    import json as json_mod
    import socket as socket_mod
    
    active_count = 0
    if SOCKET_PATH.exists():
        try:
            sock = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
            sock.settimeout(2)
            sock.connect(str(SOCKET_PATH))
            sock.sendall((json_mod.dumps({"action": "list_tasks"}) + "\n").encode())
            response = sock.recv(65536).decode()
            sock.close()
            data = json_mod.loads(response)
            active_count = len(data.get("active", []))
            pending_count = len(data.get("pending", []))
        except Exception:
            active_count = 0
    
    if active_count > 0:
        print(f"   ⏳ {active_count} активных задач — жду завершения (до {timeout}s)...")
        print(f"      (daemon сам сделает graceful shutdown, незавершённые сохранятся в БД)")
        
        # Poll каждые 5 секунд
        start = time.time()
        while time.time() - start < timeout:
            await asyncio.sleep(5)
            try:
                if SOCKET_PATH.exists():
                    sock = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
                    sock.settimeout(2)
                    sock.connect(str(SOCKET_PATH))
                    sock.sendall((json_mod.dumps({"action": "list_tasks"}) + "\n").encode())
                    response = sock.recv(65536).decode()
                    sock.close()
                    data = json_mod.loads(response)
                    remaining = len(data.get("active", []))
                    if remaining == 0:
                        print(f"   ✅ Все задачи завершены ({int(time.time() - start)}s)")
                        break
                    print(f"   ... ещё {remaining} задач (прошло {int(time.time() - start)}s)")
            except Exception:
                # Socket закрылся — daemon уже остановился
                break
        else:
            print(f"   ⚠️ Timeout {timeout}s — останавливаю с активными задачами")
            print(f"      Незавершённые задачи сохранены в БД, подхватятся после рестарта")
    
    # Теперь останавливаем daemon (он сам сделает graceful shutdown + persist)
    try:
        subprocess.run(
            ["systemctl", "--user", "stop", "caesar-daemon"],
            capture_output=True, timeout=300,  # systemd TimeoutStopSec
        )
    except Exception:
        pass
    # Fallback: pkill если systemd не справился
    subprocess.run(["pkill", "-f", "caesar.daemon"], capture_output=True)
    time.sleep(1)


async def cmd_update(args) -> int:
    """Обновить агент. Всегда: fetch → reset → pip → restart."""
    log = get_logger("cli.update")
    
    # Ищем git-репозиторий
    repo_dir = CODE_DIR
    while repo_dir != repo_dir.parent:
        if (repo_dir / ".git").exists():
            break
        repo_dir = repo_dir.parent
    else:
        print(f"❌ Не нашёл git-репозиторий")
        return 1
    
    # 1. Backup БД (молча — выводим только если что-то не так)
    db_path = DATA_DIR / "caesar.db"
    if db_path.exists():
        try:
            from datetime import datetime
            backup_path = db_path.with_suffix(f".db.backup.{datetime.now().strftime('%Y%m%d_%H%M%S')}")
            shutil.copy2(db_path, backup_path)
        except Exception as e:
            print(f"⚠️ Backup failed: {e}")
    
    # 2. Останавливаем daemon (graceful — ждём активные задачи)
    if not args.no_restart:
        print("⏸  Останавливаю daemon...")
        await _stop_daemon_graceful(timeout=180)
    
    # 3. Git fetch + reset --hard — ВСЕГДА, без проверки
    print("🔄 Обновляю код...")
    
    # Определяем текущую ветку
    try:
        branch_result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(repo_dir),
            capture_output=True, text=True, timeout=5,
        )
        branch = branch_result.stdout.strip() if branch_result.returncode == 0 else "main"
    except Exception:
        branch = "main"
    
    # Запоминаем текущий HEAD чтобы потом показать что нового
    try:
        old_head_result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_dir),
            capture_output=True, text=True, timeout=5,
        )
        old_head = old_head_result.stdout.strip() if old_head_result.returncode == 0 else None
    except Exception:
        old_head = None
    
    try:
        subprocess.run(
            ["git", "fetch", "origin", branch],
            cwd=str(repo_dir),
            capture_output=True, timeout=30,
        )
        result = subprocess.run(
            ["git", "reset", "--hard", f"origin/{branch}"],
            cwd=str(repo_dir),
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            print(f"❌ git reset failed: {result.stderr}")
            return 1
        # НЕ печатаем commit здесь — он будет в "📋 Что нового" ниже.
        # Раньше печатали '✅ <commit>' → дублировалось с "📋 Что нового".
    except Exception as e:
        print(f"❌ Git error: {e}")
        return 1
    
    # 3a. Показываем что нового (changelog) — только если есть новые коммиты.
    # Это ЕДИНСТВЕННОЕ место где показываются коммиты — больше нигде.
    if old_head:
        try:
            log_result = subprocess.run(
                ["git", "log", "--oneline", "--no-decorate", f"{old_head}..HEAD"],
                cwd=str(repo_dir),
                capture_output=True, text=True, timeout=5,
            )
            if log_result.returncode == 0 and log_result.stdout.strip():
                commits = [line.strip() for line in log_result.stdout.strip().split("\n") if line.strip()]
                if commits:
                    print(f"\n📋 Что нового ({len(commits)}):")
                    # Показываем максимум 8 коммитов
                    for c in commits[:8]:
                        # Убираем hash, оставляем только сообщение
                        parts = c.split(" ", 1)
                        if len(parts) > 1:
                            print(f"   • {parts[1]}")
                        else:
                            print(f"   • {c}")
                    if len(commits) > 8:
                        print(f"   ... и ещё {len(commits) - 8}")
                    print()
            else:
                # Нет новых коммитов — просто подтверждаем
                print("✅ Уже актуальная версия")
        except Exception:
            print("✅ Обновлено")
    else:
        print("✅ Обновлено")
    
    # 4. systemd daemon-reload (молча)
    try:
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, timeout=5)
    except Exception:
        pass
    
    # 5. pip install — переустанавливаем пакет (молча)
    venv_dir = Path.home() / ".local/share/caesar/venv"
    if venv_dir.exists():
        pip = str(venv_dir / "bin" / "pip")
        result = subprocess.run(
            [pip, "install", "--quiet", "--force-reinstall", "--no-deps", "-e", str(repo_dir)],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            print(f"⚠️ pip warnings: {result.stderr[:200]}")
    
    # 6. Чистим orchestrator из config.yaml если он там есть (молча)
    # (старые max_tokens значения могут переопределять новые дефолты)
    try:
        import yaml as _yaml
        from caesar.config import CONFIG_PATH as _config_path
        if _config_path.exists():
            with open(_config_path, "r", encoding="utf-8") as f:
                cfg_data = _yaml.safe_load(f) or {}
            if "orchestrator" in cfg_data:
                print("🧹 Чищу orchestrator из config.yaml (используем новые дефолты)...")
                del cfg_data["orchestrator"]
                with open(_config_path, "w", encoding="utf-8") as f:
                    _yaml.dump(cfg_data, f, allow_unicode=True, default_flow_style=False)
                print("   ✅ orchestrator удалён, будут использоваться дефолты из кода")
    except Exception as e:
        print(f"   ⚠️ Не удалось почистить orchestrator: {e}")
    
    # 7. Перезапускаем daemon
    if not args.no_restart:
        print("▶️  Запускаю daemon...")
        running = _restart_daemon()
        if running:
            print("✅ Caesar обновлён и запущен")
        else:
            print("⚠️  Daemon не запустился. Проверь: journalctl --user -u caesar-daemon -e")
    else:
        # --no-restart режим (вызывается из TG adapter).
        # Нельзя вызывать _restart_daemon напрямую — это убило бы subprocess,
        # запущенный из daemon, и бот не успел бы отправить ответ "✅ Код обновлён".
        #
        # Поэтому рестарт делаем ОТВЯЗАННЫМ subprocess через setsid + bash:
        # setsid запускает bash в новой session, sleep 3 даёт боту отправить
        # ответ, затем systemctl --user restart caesar-daemon.
        #
        # 'Готово' после рестарта отправляет сам новый daemon через
        # _notify_restart_complete (читает /tmp/caesar-restart-chat-id, который
        # мы пишем ниже из env var CAESAR_TG_CHAT_ID).
        #
        # ВАЖНО: код выполняется ВНУТРИ subprocess 'python -m caesar.management',
        # а НЕ внутри daemon-процесса. subprocess импортирует модули заново
        # с диска (уже обновлённые git pull), поэтому использует НОВЫЙ код.
        import os as _os
        import sys as _sys
        
        tg_chat_id = _os.environ.get("CAESAR_TG_CHAT_ID", "")
        daemon_uid = _os.getuid()
        xdg_runtime_dir = f"/run/user/{daemon_uid}"
        
        # Сохраняем chat_id в файл — новый daemon при старте отправит
        # 'готово' через _notify_restart_complete.
        if tg_chat_id:
            try:
                with open("/tmp/caesar-restart-chat-id", "w") as f:
                    f.write(tg_chat_id)
            except Exception:
                pass
        
        venv_python = str(Path.home() / ".local/share/caesar/venv/bin/python")
        if not Path(venv_python).exists():
            venv_python = _sys.executable  # fallback на текущий python
        
        # Bash скрипт делает ТОЛЬКО рестарт. 'Готово' отправит новый daemon
        # через _notify_restart_complete (читает /tmp/caesar-restart-chat-id).
        log_file = "/tmp/caesar-tg-ready.log"
        
        # Пробуем разные методы рестарта — systemd --user, потом system,
        # потом kill -HUP. Логируем stderr каждого.
        restart_script = (
            f"echo '=== restart script starting at ' $(date) >> {log_file} 2>&1"
            f"; echo 'chat_id: {tg_chat_id}' >> {log_file} 2>&1"
            f"; echo 'uid: {daemon_uid}, xdg: {xdg_runtime_dir}' >> {log_file} 2>&1"
            f"; sleep 3"
            # Метод 1: systemctl --user restart
            f"; echo '--- trying systemctl --user restart ---' >> {log_file} 2>&1"
            f"; XDG_RUNTIME_DIR={xdg_runtime_dir} "
            f"DBUS_SESSION_BUS_ADDRESS=unix:path={xdg_runtime_dir}/bus "
            f"systemctl --user restart caesar-daemon >> {log_file} 2>&1"
            f"; USER_RC=$?"
            f"; echo 'systemctl --user exit code: '$USER_RC >> {log_file} 2>&1"
            # Если --user не сработал (exit != 0) — пробуем system
            f"; if [ $USER_RC -ne 0 ]; then"
            f" echo '--- trying systemctl (system) restart ---' >> {log_file} 2>&1"
            f"; systemctl restart caesar-daemon >> {log_file} 2>&1"
            f"; SYS_RC=$?"
            f"; echo 'systemctl (system) exit code: '$SYS_RC >> {log_file} 2>&1"
            # Если system тоже не сработал — kill -HUP
            f"; if [ $SYS_RC -ne 0 ]; then"
            f" echo '--- trying kill -HUP ---' >> {log_file} 2>&1"
            f"; pkill -HUP -f 'caesar.daemon' >> {log_file} 2>&1"
            f"; KILL_RC=$?"
            f"; echo 'kill -HUP exit code: '$KILL_RC >> {log_file} 2>&1"
            f"; fi"
            f"; fi"
            f"; echo '=== daemon restarted at ' $(date) >> {log_file} 2>&1"
        )
        
        # Пробуем setsid (предпочтительный — отвязывает subprocess)
        restart_scheduled = False
        try:
            subprocess.Popen(
                ["setsid", "bash", "-c", restart_script],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            restart_scheduled = True
        except FileNotFoundError:
            # setsid не найден — пробуем nohup
            try:
                subprocess.Popen(
                    ["nohup", "bash", "-c", restart_script],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                restart_scheduled = True
            except Exception as e:
                print(f"⚠️ nohup fallback failed: {e}")
        except Exception as e:
            print(f"⚠️ setsid failed: {e}")
        
        if not restart_scheduled:
            # Последний fallback — синхронный systemctl restart.
            # Это убьёт нас, но daemon перезапустится.
            try:
                subprocess.run(
                    ["systemctl", "--user", "restart", "caesar-daemon"],
                    capture_output=True, text=True, timeout=30,
                    env={
                        **_os.environ,
                        "XDG_RUNTIME_DIR": xdg_runtime_dir,
                        "DBUS_SESSION_BUS_ADDRESS": f"unix:path={xdg_runtime_dir}/bus",
                    },
                )
                restart_scheduled = True
            except Exception as e:
                print(f"❌ Рестарт не сработал: {e}")
                print(f"Выполни вручную: systemctl --user restart caesar-daemon")
    
    return 0


async def cmd_rollback(args) -> int:
    """Откатиться к предыдущей версии."""
    log = get_logger("cli.rollback")
    
    # Ищем git-репозиторий: от CODE_DIR вверх
    repo_dir = CODE_DIR
    while repo_dir != repo_dir.parent:
        if (repo_dir / ".git").exists():
            break
        repo_dir = repo_dir.parent
    else:
        print(f"❌ Не нашёл git-репозиторий")
        return 1
    
    # Получаем предыдущий коммит
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD~1"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            print(f"❌ Нет предыдущего коммита: {result.stderr}")
            return 1
        prev_commit = result.stdout.strip()[:8]
        
        result = subprocess.run(
            ["git", "log", "-1", "--oneline", "HEAD~1"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=10,
        )
        prev_msg = result.stdout.strip()
    except Exception as e:
        print(f"❌ Git error: {e}")
        return 1
    
    print(f"⏪ Откат к предыдущей версии:")
    print(f"   {prev_msg}")
    print()
    
    if not args.yes:
        answer = input("Подтвердить откат? [y/N]: ").strip().lower()
        if answer not in ("y", "yes", "д", "да"):
            print("Отмена")
            return 0
    
    # Останавливаем daemon
    print("⏸  Останавливаю daemon...")
    _stop_daemon()
    
    # Git reset
    print("⏪ Откатываю код...")
    try:
        result = subprocess.run(
            ["git", "reset", "--hard", "HEAD~1"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            print(f"❌ git reset failed: {result.stderr}")
            return 1
        print(f"   ✅ {result.stdout.strip()}")
    except Exception as e:
        print(f"❌ Git error: {e}")
        return 1
    
    # Запускаем daemon
    print("▶️  Запускаю daemon...")
    _restart_daemon()
    
    print(f"\n✅ Откат выполнен к {prev_commit}")
    return 0


async def cmd_uninstall(args) -> int:
    """Удалить агента."""
    print("Удалить Caesar?")
    print("  - код в репозитории (не удаляется автоматически)")
    print("  - systemd сервисы")
    print("  - данные и конфиги (опционально)")
    print()
    
    if not args.yes:
        save_data = input("Сохранить данные пользователя? [Y/n]: ").strip().lower()
        save_data = save_data in ("", "y", "yes", "д", "да")
        
        confirm = input("Продолжить? [y/N]: ").strip().lower()
        if confirm not in ("y", "yes", "д", "да"):
            print("Отмена")
            return 0
    else:
        save_data = args.keep_data
    
    # Останавливаем сервисы
    print("⏸  Останавливаю сервисы...")
    _stop_daemon()
    try:
        subprocess.run(
            ["systemctl", "--user", "stop", "caesar-watchdog"],
            capture_output=True, timeout=5,
        )
        subprocess.run(
            ["systemctl", "--user", "disable", "caesar-daemon", "caesar-watchdog"],
            capture_output=True, timeout=5,
        )
    except Exception:
        pass
    
    # Удаляем systemd-юниты (user-space)
    systemd_user_dir = Path.home() / ".config" / "systemd" / "user"
    for unit in ("caesar-daemon.service", "caesar-watchdog.service"):
        p = systemd_user_dir / unit
        if p.exists():
            p.unlink()
    try:
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, timeout=5)
    except Exception:
        pass
    print("✅ Сервисы удалены")
    
    # Данные
    if save_data:
        from datetime import datetime as dt
        backup_dir = Path.home() / f"caesar-backup-{dt.now().strftime('%Y-%m-%d')}"
        backup_dir.mkdir(parents=True, exist_ok=True)
        if DATA_DIR.exists():
            shutil.copytree(DATA_DIR, backup_dir / "data", dirs_exist_ok=True)
            print(f"✅ Данные сохранены в {backup_dir}")
        config_dir = Path.home() / ".config" / "caesar"
        if config_dir.exists():
            shutil.copytree(config_dir, backup_dir / "config", dirs_exist_ok=True)
            print(f"✅ Конфиг сохранён в {backup_dir}/config")
    else:
        if DATA_DIR.exists():
            shutil.rmtree(DATA_DIR)
            print("✅ Данные удалены")
        config_dir = Path.home() / ".config" / "caesar"
        if config_dir.exists():
            shutil.rmtree(config_dir)
            print("✅ Конфиг удалён")
    
    print()
    print("✅ Агент удалён")
    return 0


async def cmd_permissions(args) -> int:
    """Управление whitelist разрешений."""
    from caesar.memory.storage import Storage
    storage = Storage()
    user_id = f"cli-{os.getuid()}"
    
    if args.permissions_action == "list":
        with storage._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM permissions WHERE user_id = ? ORDER BY tool, pattern",
                (user_id,),
            ).fetchall()
        
        if not rows:
            print("Нет разрешений")
            return 0
        
        print(f"Разрешения для {user_id}:")
        for r in rows:
            print(f"  {r['tool']:15s} {r['permission_type']:15s} {r['pattern']}")
    
    elif args.permissions_action == "revoke":
        if not args.pattern:
            print("Укажи --pattern")
            return 1
        with storage._conn() as conn:
            conn.execute(
                "DELETE FROM permissions WHERE user_id = ? AND pattern = ?",
                (user_id, args.pattern),
            )
        print(f"✅ Отозвано: {args.pattern}")
    
    elif args.permissions_action == "reset":
        with storage._conn() as conn:
            conn.execute("DELETE FROM permissions WHERE user_id = ?", (user_id,))
        print("✅ Все разрешения сброшены")
    
    return 0


async def cmd_stats(args) -> int:
    """Статистика по токенам."""
    from caesar.memory.storage import Storage
    from datetime import datetime, timedelta
    storage = Storage()
    
    if args.task:
        # Статистика по конкретной задаче
        stats = storage.get_token_stats(args.task)
        print(f"Задача {args.task}:")
        print(f"  Вызовов LLM: {stats.get('calls') or 0}")
        print(f"  Prompt токенов: {stats.get('prompt') or 0}")
        print(f"  Completion токенов: {stats.get('completion') or 0}")
        print(f"  Всего токенов: {stats.get('total') or 0}")
        print(f"  Стоимость: {stats.get('cost') or 0:.4f} руб")
        return 0
    
    # Период
    if getattr(args, "today", False):
        period = "today"
    elif getattr(args, "week", False):
        period = "week"
    else:
        period = "all"
    if period == "today":
        time_filter = "datetime('now', 'start of day')"
        label = "Сегодня"
    elif period == "week":
        time_filter = "datetime('now', '-7 days')"
        label = "За неделю"
    else:
        time_filter = None
        label = "За всё время"
    
    print(f"📊 Статистика — {label}")
    print("=" * 50)
    
    try:
        with storage._conn() as conn:
            if time_filter:
                row = conn.execute(
                    f"""SELECT 
                        COUNT(*) as calls,
                        SUM(prompt_tokens) as prompt,
                        SUM(completion_tokens) as completion,
                        SUM(total_tokens) as total,
                        SUM(cost_rub) as cost
                       FROM token_usage
                       WHERE timestamp >= {time_filter}"""
                ).fetchone()
            else:
                row = conn.execute(
                    """SELECT 
                        COUNT(*) as calls,
                        SUM(prompt_tokens) as prompt,
                        SUM(completion_tokens) as completion,
                        SUM(total_tokens) as total,
                        SUM(cost_rub) as cost
                       FROM token_usage"""
                ).fetchone()
            
            calls = row["calls"] if row else 0
            prompt = row["prompt"] or 0
            completion = row["completion"] or 0
            total = row["total"] or 0
            cost = row["cost"] or 0
            
            print(f"\nВсего:")
            print(f"  Вызовов LLM: {calls:,}")
            print(f"  Токенов: {total:,} (prompt: {prompt:,} / completion: {completion:,})")
            print(f"  Стоимость: {cost:.4f} руб")
            
            # Разбивка по ролям
            if time_filter:
                rows = conn.execute(
                    f"""SELECT llm_role, llm_model,
                        COUNT(*) as calls,
                        SUM(total_tokens) as total
                       FROM token_usage
                       WHERE timestamp >= {time_filter}
                       GROUP BY llm_role, llm_model
                       ORDER BY total DESC"""
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT llm_role, llm_model,
                        COUNT(*) as calls,
                        SUM(total_tokens) as total
                       FROM token_usage
                       GROUP BY llm_role, llm_model
                       ORDER BY total DESC"""
                ).fetchall()
            
            if rows:
                print(f"\nРазбивка по ролям:")
                for r in rows:
                    role = r["llm_role"] or "unknown"
                    model = r["llm_model"] or "?"
                    print(f"  {role:8s} {model:30s} {r['calls']:>5} calls  {r['total'] or 0:>10,} tokens")
            
            # Разбивка по причинам
            if time_filter:
                rows = conn.execute(
                    f"""SELECT reason,
                        COUNT(*) as calls,
                        SUM(total_tokens) as total
                       FROM token_usage
                       WHERE timestamp >= {time_filter}
                       GROUP BY reason
                       ORDER BY total DESC"""
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT reason,
                        COUNT(*) as calls,
                        SUM(total_tokens) as total
                       FROM token_usage
                       GROUP BY reason
                       ORDER BY total DESC"""
                ).fetchall()
            
            if rows:
                print(f"\nПо причинам:")
                for r in rows:
                    reason = r["reason"] or "unknown"
                    print(f"  {reason:20s} {r['calls']:>5} calls  {r['total'] or 0:>10,} tokens")
    
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        return 1
    
    return 0


async def cmd_setup(args) -> int:
    """Запустить setup wizard."""
    from caesar.setup import run_setup
    return await run_setup()


# === Feature definitions for `caesar enable` ===
# Каждая фича: dep (что ставить в venv), config_section (куда писать),
# config_values (что писать), restart (нужен ли рестарт daemon).
FEATURES = {
    "stt": {
        "description": "Распознавание голосовых сообщений (faster-whisper, ~150MB модель)",
        "pip_dep": "faster-whisper>=1.0",
        "config_section": "stt",
        "config_values": {"enabled": True, "model": "base", "language": None},
        "needs_restart": True,
    },
    "voice": {  # alias
        "description": "Распознавание голосовых сообщений (faster-whisper, ~150MB модель)",
        "pip_dep": "faster-whisper>=1.0",
        "config_section": "stt",
        "config_values": {"enabled": True, "model": "base", "language": None},
        "needs_restart": True,
    },
    "l3": {
        "description": "Векторная память L3 — семантический поиск по прошлым диалогам (sentence-transformers, ~470MB модель)",
        "pip_dep": "sentence-transformers>=2.7",
        "config_section": "l3",
        "config_values": {"enabled": True, "model": "multilingual-minilm"},
        "needs_restart": True,
    },
    "memory": {  # alias для l3
        "description": "Векторная память L3 — семантический поиск по прошлым диалогам",
        "pip_dep": "sentence-transformers>=2.7",
        "config_section": "l3",
        "config_values": {"enabled": True, "model": "multilingual-minilm"},
        "needs_restart": True,
    },
    "cron": {
        "description": "Планировщик задач — cron (APScheduler). 'Каждый день в 9:00 делай дайджест'",
        "pip_dep": "APScheduler>=3.10",
        "config_section": "cron",
        "config_values": {"enabled": True},
        "needs_restart": True,
    },
    "scheduler": {  # alias для cron
        "description": "Планировщик задач — cron (APScheduler)",
        "pip_dep": "APScheduler>=3.10",
        "config_section": "cron",
        "config_values": {"enabled": True},
        "needs_restart": True,
    },
}


def _find_venv_pip() -> str | None:
    """Найти pip в venv Caesar (не system python)."""
    from pathlib import Path
    candidates = [
        Path.home() / ".local/share/caesar/venv/bin/pip",
        Path.home() / ".local/share/caesar/venv/bin/python",  # -m pip
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return None


def _update_config_yaml(section: str, values: dict) -> bool:
    """Обновить секцию в config.yaml (создать если нет)."""
    import yaml
    from caesar.config import CONFIG_PATH
    
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    
    data = {}
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except Exception:
            data = {}
    
    # Обновляем секцию
    if section not in data:
        data[section] = {}
    data[section].update(values)
    
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            yaml.dump(data, f, allow_unicode=True, default_flow_style=False)
        return True
    except Exception as e:
        print(f"❌ Не удалось записать config: {e}")
        return False


async def cmd_enable(args) -> int:
    """Включить фичу одной командой: pip install + config update + restart."""
    feature = args.feature
    if feature not in FEATURES:
        print(f"❌ Неизвестная фича: {feature}")
        print(f"Доступные: {', '.join(FEATURES.keys())}")
        return 1
    
    fdef = FEATURES[feature]
    print(f"📦 Включаю: {fdef['description']}")
    print()
    
    # 1. Находим venv pip
    venv_pip = _find_venv_pip()
    if not venv_pip:
        print("❌ Caesar venv не найден в ~/.local/share/caesar/venv/")
        print("   Установите Caesar сначала: curl -fsSL https://raw.githubusercontent.com/madlenprust/caesar/main/install.sh | bash")
        return 1
    
    # 2. pip install в venv
    print(f"1. Устанавливаю {fdef['pip_dep']} в venv...")
    if venv_pip.endswith("/python"):
        # python -m pip install
        cmd = [venv_pip, "-m", "pip", "install", fdef["pip_dep"]]
    else:
        cmd = [venv_pip, "install", fdef["pip_dep"]]
    
    try:
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=180,
        )
        if result.returncode == 0:
            print("   ✅ Установлено")
        else:
            print(f"   ❌ pip error: {result.stderr[:500]}")
            return 1
    except subprocess.TimeoutExpired:
        print("   ❌ Таймаут установки (180 сек)")
        return 1
    
    # 3. Обновляем config.yaml
    print(f"\n2. Обновляю config.yaml ({fdef['config_section']})...")
    if _update_config_yaml(fdef["config_section"], fdef["config_values"]):
        print("   ✅ Config обновлён")
    else:
        return 1
    
    # 4. Рестарт daemon если нужно
    if fdef["needs_restart"] and not args.no_restart:
        print("\n3. Перезапускаю daemon...")
        try:
            subprocess.run(["systemctl", "--user", "daemon-reload"],
                           capture_output=True, timeout=5)
            subprocess.run(["systemctl", "--user", "restart", "caesar-daemon"],
                           capture_output=True, timeout=10)
            print("   ✅ Daemon перезапущен")
        except Exception as e:
            print(f"   ⚠️ Не удалось перезапустить: {e}")
            print("   Перезапусти вручную: systemctl --user restart caesar-daemon")
    
    print(f"\n🎉 Готово! Фича '{feature}' включена.")
    if feature in ("stt", "voice"):
        print("\n📝 При первом голосовом сообщении скачается модель (~150MB).")
        print("   Потом работает офлайн.")
    elif feature in ("l3", "memory"):
        print("\n📝 При первом запросе в память скачается модель (~470MB).")
        print("   Потом работает офлайн.")
        print("   L3 индексирует все диалоги — поиск будет работать после того")
        print("   как пройдёт несколько диалогов (нужно что-то проиндексировать).")
    return 0


async def cmd_l3(args) -> int:
    """Управление L3 векторной памятью: status, search, reindex, clear."""
    action = args.l3_action
    
    # Lazy imports — чтобы не тащить sentence-transformers для status/clear
    from caesar.memory.storage import Storage
    from caesar.config import Config
    import os as _os
    
    config = Config.load()
    storage = Storage()
    
    # user_id по умолчанию — текущий CLI пользователь
    user_id = getattr(args, "user", "") or f"cli-{_os.getuid()}"
    # Ищем существующего по unix_uid
    existing = storage.get_user_by_uid(_os.getuid())
    if existing:
        user_id = existing["id"]
    
    if action == "status":
        print("📊 L3 Status")
        print("=" * 50)
        
        # Конфиг
        l3_enabled = getattr(config.l3, "enabled", False) if hasattr(config, "l3") else False
        l3_model = getattr(config.l3, "model", "multilingual-minilm") if hasattr(config, "l3") else "multilingual-minilm"
        print(f"Enabled: {'✅ yes' if l3_enabled else '❌ no (caesar enable l3)'}")
        print(f"Model: {l3_model}")
        
        # Чанки
        try:
            with storage._conn() as conn:
                total = conn.execute("SELECT COUNT(*) as cnt FROM l3_chunks").fetchone()
                print(f"Total chunks: {total['cnt'] if total else 0}")
                
                # По пользователям
                rows = conn.execute(
                    "SELECT user_id, COUNT(*) as cnt FROM l3_chunks GROUP BY user_id"
                ).fetchall()
                for row in rows:
                    marker = " ← you" if row["user_id"] == user_id else ""
                    print(f"  user {row['user_id']}: {row['cnt']} chunks{marker}")
                
                # По каналам
                rows = conn.execute(
                    "SELECT channel, COUNT(*) as cnt FROM l3_chunks GROUP BY channel"
                ).fetchall()
                print(f"\nBy channel:")
                for row in rows:
                    print(f"  {row['channel']}: {row['cnt']} chunks")
                
                # Последние чанки
                rows = conn.execute(
                    "SELECT id, channel, content, chunk_metadata FROM l3_chunks "
                    "ORDER BY rowid DESC LIMIT 5"
                ).fetchall()
                print(f"\nLast 5 chunks:")
                for row in rows:
                    meta = row["chunk_metadata"] or "{}"
                    has_emb = "yes" if "embedding" in meta else "no"
                    content_preview = row['content'][:120]
                    # Проверяем на кракозябры (нет ли непечатных символов)
                    has_garbage = any(ord(c) > 0xFFFF or (ord(c) < 32 and c not in "\n\t\r") for c in content_preview)
                    garbage_marker = " ⚠️ GARBLED" if has_garbage else ""
                    print(f"  - {row['id'][:20]}... ch={row['channel']} emb={has_emb}{garbage_marker}")
                    print(f"    {content_preview}...")
        except Exception as e:
            print(f"❌ DB error: {e}")
            return 1
        
        # Проверка sentence-transformers
        try:
            import sentence_transformers
            print(f"\nsentence-transformers: ✅ {sentence_transformers.__version__}")
        except ImportError:
            print(f"\nsentence-transformers: ❌ not installed")
            print(f"  Install: caesar enable l3")
        
        return 0
    
    elif action == "search":
        query = args.query
        limit = args.limit
        # Если --user не указан, ищем по ВСЕМ пользователям (для дебага)
        user_filter = getattr(args, "user", "")
        print(f"🔍 L3 Search: '{query}'")
        if user_filter:
            print(f"   user_id: {user_filter}")
        else:
            print(f"   user_id: ALL (не указан --user)")
        print("=" * 50)
        
        try:
            from caesar.memory.l3 import L3Memory, _get_embedding_model
            
            model = _get_embedding_model(getattr(config.l3, "model", "multilingual-minilm"))
            if model is None:
                print("❌ Модель не загружена. Возможно sentence-transformers не установлен.")
                print("   Установи: caesar enable l3")
                return 1
            
            l3 = L3Memory(storage, model_key=getattr(config.l3, "model", "multilingual-minilm"))
            print(f"L3 cache: {len(l3._vectors_cache)} vectors")
            
            # Если user указан — ищем только его чанки
            if user_filter:
                results = await l3.search(
                    query=query,
                    user_id=user_filter,
                    final_k=limit,
                    min_similarity=0.1,  # ниже порог для дебага
                )
            else:
                # Ищем по всем — собираем всех user_id из БД
                with storage._conn() as conn:
                    user_rows = conn.execute(
                        "SELECT DISTINCT user_id FROM l3_chunks"
                    ).fetchall()
                
                if not user_rows:
                    print("❌ В L3 нет ни одного чанка")
                    return 0
                
                print(f"Searching across {len(user_rows)} user(s):")
                for ur in user_rows:
                    print(f"  - {ur['user_id']}")
                print()
                
                results = []
                for ur in user_rows:
                    uid = ur["user_id"]
                    user_results = await l3.search(
                        query=query,
                        user_id=uid,
                        final_k=limit,
                        min_similarity=0.1,
                    )
                    for r in user_results:
                        r.metadata["_user_id"] = uid  # помечаем чей чанк
                    results.extend(user_results)
                
                # Сортируем по score
                results.sort(key=lambda r: r.score, reverse=True)
                results = results[:limit]
            
            if not results:
                print("❌ Ничего не найдено (даже с порогом 0.1)")
                # Дополнительная диагностика
                with storage._conn() as conn:
                    row = conn.execute("SELECT COUNT(*) as cnt FROM l3_chunks").fetchone()
                    print(f"   Всего чанков в L3: {row['cnt'] if row else 0}")
                return 0
            
            print(f"\nFound {len(results)} results:")
            for i, r in enumerate(results):
                source = r.metadata.get("file_name", "")
                uid = r.metadata.get("_user_id", user_filter or "?")
                source_label = f" [doc: {source}]" if source else f" [ch: {r.channel}]"
                print(f"\n[{i+1}] score={r.score:.3f}{source_label} user={uid}")
                print(f"    {r.content[:200]}")
        
        except Exception as e:
            print(f"❌ Error: {e}")
            import traceback
            traceback.print_exc()
            return 1
        
        return 0
    
    elif action == "show":
        """Показать содержимое чанков — для дебага кодировки."""
        limit = args.limit
        show_full = args.full
        print(f"📄 L3 Chunks (last {limit})")
        print("=" * 60)
        
        try:
            with storage._conn() as conn:
                rows = conn.execute(
                    "SELECT id, user_id, channel, content, chunk_metadata "
                    "FROM l3_chunks ORDER BY rowid DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            
            if not rows:
                print("Нет чанков в L3.")
                return 0
            
            for i, row in enumerate(rows):
                d = dict(row)
                import json as _json
                try:
                    meta = _json.loads(d.get("chunk_metadata") or "{}")
                except Exception:
                    meta = {}
                
                file_name = meta.get("file_name", "")
                source = meta.get("source", "")
                
                content = d["content"]
                # Проверяем на кракозябры
                garbage_chars = sum(1 for c in content[:200] if ord(c) > 0xFFFF or (ord(c) < 32 and c not in "\n\t\r"))
                is_garbled = garbage_chars > len(content[:200]) * 0.05
                
                print(f"\n[{i+1}] {d['id'][:20]}...")
                print(f"    user: {d['user_id']}")
                print(f"    channel: {d['channel']}")
                if file_name:
                    print(f"    file: {file_name}")
                if source:
                    print(f"    source: {source}")
                print(f"    length: {len(content)} chars")
                print(f"    garbled: {'⚠️ YES' if is_garbled else '✓ no'}")
                
                if show_full:
                    print(f"    content:")
                    print(f"    ---")
                    for line in content.split("\n"):
                        print(f"    {line}")
                    print(f"    ---")
                else:
                    preview = content[:200].replace("\n", " ↵ ")
                    print(f"    preview: {preview}")
        
        except Exception as e:
            print(f"❌ DB error: {e}")
            return 1
        
        return 0
    
    elif action == "reindex":
        print("🔄 Переиндексация L3...")
        print("Это пересчитает эмбеддинги для всех чанков с текущим CHUNK_SIZE.")
        print()
        
        # Подтверждение
        if not args.yes if hasattr(args, "yes") else True:
            try:
                answer = input("Продолжить? [y/N] ")
                if answer.lower() not in ("y", "yes", "д", "да"):
                    print("Отменено.")
                    return 0
            except EOFError:
                pass
        
        try:
            from caesar.memory.l3 import L3Memory, _get_embedding_model, _chunk_text
            
            model = _get_embedding_model(getattr(config.l3, "model", "multilingual-minilm"))
            if model is None:
                print("❌ Модель не загружена. Установи: caesar enable l3")
                return 1
            
            # Загружаем все чанки
            with storage._conn() as conn:
                rows = conn.execute(
                    "SELECT id, user_id, channel, author_id, content, chunk_metadata, task_id "
                    "FROM l3_chunks"
                ).fetchall()
            
            print(f"Найдено {len(rows)} чанков для переиндексации")
            
            if not rows:
                print("Нечего переиндексировать.")
                return 0
            
            # Удаляем все старые чанки
            with storage._conn() as conn:
                conn.execute("DELETE FROM l3_chunks")
                conn.commit()
            print("Старые чанки удалены")
            
            # Переиндексируем
            l3 = L3Memory(storage, model_key=getattr(config.l3, "model", "multilingual-minilm"))
            total_new = 0
            for row in rows:
                d = dict(row)
                try:
                    meta = json.loads(d.get("chunk_metadata") or "{}")
                    meta.pop("embedding", None)  # старый эмбеддинг не нужен
                    
                    chunk_ids = await l3.add(
                        user_id=d["user_id"],
                        channel=d["channel"],
                        content=d["content"],
                        author_id=d.get("author_id"),
                        task_id=d.get("task_id"),
                        metadata=meta,
                    )
                    total_new += len(chunk_ids)
                except Exception as e:
                    print(f"⚠️ Failed to reindex {d['id']}: {e}")
            
            print(f"\n✅ Переиндексировано: {len(rows)} → {total_new} чанков")
            print(f"   (CHUNK_SIZE стал меньше, чанков больше, но точнее)")
        
        except Exception as e:
            print(f"❌ Error: {e}")
            import traceback
            traceback.print_exc()
            return 1
        
        return 0
    
    elif action == "clear":
        print("⚠️  Удаление ВСЕХ чанков L3!")
        print("Это удалит все загруженные документы и индексированные диалоги.")
        
        try:
            answer = input("Точно удалить? Type 'DELETE' to confirm: ")
            if answer != "DELETE":
                print("Отменено.")
                return 0
        except EOFError:
            pass
        
        try:
            with storage._conn() as conn:
                cur = conn.execute("DELETE FROM l3_chunks")
                conn.commit()
                print(f"✅ Удалено {cur.rowcount} чанков")
        except Exception as e:
            print(f"❌ Error: {e}")
            return 1
        
        return 0
    
    elif action == "fix-encoding":
        """Попробовать восстановить кракозябры в чанках.
        
        Если чанк был сохранён с неправильной кодировкой (cp1251 прочитан
        как utf-8 или наоборот), пробуем разные комбинации encode/decode
        чтобы восстановить оригинальный текст.
        """
        dry_run = args.dry_run
        print("🔧 Fix encoding — пытаемся восстановить кракозябры")
        if dry_run:
            print("   (dry-run: только показываем, не сохраняем)")
        print("=" * 60)
        
        try:
            with storage._conn() as conn:
                rows = conn.execute(
                    "SELECT id, content FROM l3_chunks"
                ).fetchall()
            
            print(f"Всего чанков: {len(rows)}")
            
            # Комбинации кодировок для восстановления
            fix_combos = [
                # (описание, encode_enc, decode_enc, [intermediate_enc])
                ("koi8-r→cp1251", "koi8-r", "cp1251", None),
                ("utf-8→latin-1→cp1251", "utf-8", "cp1251", "latin-1"),
                ("utf-8→latin-1→koi8-r", "utf-8", "koi8-r", "latin-1"),
                ("cp1251→utf-8", "cp1251", "utf-8", None),
                ("koi8-r→utf-8", "koi8-r", "utf-8", None),
            ]
            
            fixed_count = 0
            unchanged_count = 0
            failed_count = 0
            
            for row in rows:
                content = row["content"]
                chunk_id = row["id"]
                
                # Проверяем — может уже чистый?
                def is_clean(text):
                    if not text or len(text) < 10:
                        return False
                    sample = text[:500]
                    # Control chars и >0xFFFF
                    garbage = sum(1 for c in sample if ord(c) > 0xFFFF or (ord(c) < 32 and c not in "\n\t\r"))
                    if garbage > len(sample) * 0.05:
                        return False
                    # Проверка на осмысленность: в русском тексте должны быть
                    # гласные (аеиоуыэюя) и пробелы. Кракозябры часто не содержат их.
                    vowels = "аеиоуыэюяАЕИОУЫЭЮЯ"
                    vowel_count = sum(1 for c in sample if c in vowels)
                    space_count = sum(1 for c in sample if c == " ")
                    total = len(sample)
                    # В нормальном русском тексте гласные = ~15-25% от букв
                    # В кракозябрах из koi8-r→utf-8 — почти 0%
                    if total > 50 and vowel_count < total * 0.02:
                        return False  # почти нет гласных — кракозябры
                    if total > 50 and space_count < total * 0.05:
                        return False  # почти нет пробелов — кракозябры
                    return True
                
                if is_clean(content):
                    unchanged_count += 1
                    continue
                
                # Пробуем восстановить
                best_fix = None
                best_desc = ""
                for desc, *encs in fix_combos:
                    try:
                        # encs = [encode_enc, decode_enc, intermediate_enc]
                        encode_enc = encs[0]
                        decode_enc = encs[1]
                        intermediate_enc = encs[2] if len(encs) > 2 else None
                        
                        if intermediate_enc:
                            # triple: encode → intermediate decode → intermediate encode → final decode
                            intermediate = content.encode(encode_enc).decode(intermediate_enc).encode(intermediate_enc).decode(decode_enc)
                        else:
                            # simple: encode → decode
                            intermediate = content.encode(encode_enc).decode(decode_enc)
                        
                        if is_clean(intermediate) and len(intermediate) > 10:
                            best_fix = intermediate
                            best_desc = desc
                            break
                    except Exception:
                        continue
                
                if best_fix:
                    fixed_count += 1
                    if dry_run:
                        print(f"\n✓ {chunk_id[:20]}... ({best_desc})")
                        print(f"  WAS: {content[:80]}")
                        print(f"  NOW: {best_fix[:80]}")
                    else:
                        with storage._conn() as conn:
                            conn.execute(
                                "UPDATE l3_chunks SET content = ? WHERE id = ?",
                                (best_fix, chunk_id),
                            )
                            conn.commit()
                        if fixed_count <= 5:  # показываем первые 5
                            print(f"\n✓ Fixed {chunk_id[:20]}... ({best_desc})")
                            print(f"  WAS: {content[:80]}")
                            print(f"  NOW: {best_fix[:80]}")
                else:
                    failed_count += 1
                    if failed_count <= 3:
                        print(f"\n✗ Cannot fix {chunk_id[:20]}...")
                        print(f"  Content: {content[:80]}")
            
            print(f"\n{'='*60}")
            print(f"✅ Fixed: {fixed_count}")
            print(f"⏭️  Already clean: {unchanged_count}")
            print(f"❌ Cannot fix: {failed_count}")
            
            if not dry_run and fixed_count > 0:
                print(f"\n💡 Чанки обновлены. Теперь переиндексируй embeddings:")
                print(f"   caesar l3 reindex")
        
        except Exception as e:
            print(f"❌ Error: {e}")
            import traceback
            traceback.print_exc()
            return 1
        
        return 0
    
    return 1


async def _cmd_models_list(config) -> int:
    """Показать текущие модели и доступные от провайдера."""
    import httpx
    from caesar.config import Config
    
    print("📋 LLM Models")
    print("=" * 50)
    
    # Текущие настройки
    print(f"\nТекущие настройки:")
    print(f"  Smart: {config.llm.smart_provider} / {config.llm.smart_model}")
    print(f"  Cheap: {config.llm.cheap_provider} / {config.llm.cheap_model}")
    if config.llm.smart_base_url:
        print(f"  Smart base_url: {config.llm.smart_base_url}")
    if config.llm.cheap_base_url and config.llm.cheap_base_url != config.llm.smart_base_url:
        print(f"  Cheap base_url: {config.llm.cheap_base_url}")
    print(f"  Smart API key: {'✅ установлен' if config.llm.smart_api_key else '❌ пустой'}")
    print(f"  Cheap API key: {'✅ установлен' if config.llm.cheap_api_key else '❌ пустой'}")
    
    # Получаем список доступных моделей
    provider = config.llm.smart_provider
    base_url = config.llm.smart_base_url
    api_key = config.llm.smart_api_key
    
    print(f"\n📡 Доступные модели от {provider}:")
    
    models_list = []
    
    if provider == "anthropic":
        models_list = [
            "claude-sonnet-4-20250514",
            "claude-opus-4-20250514",
            "claude-3-7-sonnet-20250219",
            "claude-3-5-haiku-20241022",
        ]
        print(f"  (предзаготовленный список — Anthropic не имеет /models endpoint)")
    else:
        try:
            headers = {}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            
            url = f"{base_url}/models" if base_url else "https://api.openai.com/v1/models"
            
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(url, headers=headers)
            
            if resp.status_code == 200:
                data = resp.json()
                if "data" in data:
                    models_list = [m["id"] for m in data["data"] if "id" in m]
                elif "models" in data:
                    models_list = [m.get("name", m.get("id", "")) for m in data["models"]]
                models_list.sort()
            else:
                print(f"  ❌ HTTP {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            print(f"  ❌ Ошибка: {e}")
    
    if models_list:
        for i, m in enumerate(models_list, 1):
            tags = []
            if m == config.llm.smart_model:
                tags.append("← SMART")
            if m == config.llm.cheap_model:
                tags.append("← CHEAP")
            tag_str = f" ({', '.join(tags)})" if tags else ""
            print(f"  {i:3d}. {m}{tag_str}")
        print(f"\nВсего: {len(models_list)} моделей")
    else:
        print("  (не удалось получить список)")
    
    print(f"\n💡 Для изменения: caesar models")
    return 0


async def cmd_models(args) -> int:
    """Настройка smart и cheap моделей.
    
    caesar models        — интерактивная настройка
    caesar models list   — показать текущие и доступные модели
    """
    import httpx
    import yaml as _yaml
    from caesar.config import CONFIG_PATH, Config
    
    config = Config.load(CONFIG_PATH)
    
    # === caesar models list ===
    if getattr(args, "models_action", None) == "list":
        return await _cmd_models_list(config)
    
    # === caesar models (без subcommand) — интерактивная настройка ===
    print("🤖 Настройка LLM моделей")
    print("=" * 50)
    
    # Текущие настройки
    print(f"\nТекущие настройки:")
    print(f"  Smart:  {config.llm.smart_provider} / {config.llm.smart_model}")
    print(f"  Cheap:  {config.llm.cheap_provider} / {config.llm.cheap_model}")
    if config.llm.smart_base_url:
        print(f"  Smart base_url: {config.llm.smart_base_url}")
    
    # === 1. ВЫБОР PROVIDER ===
    print("\nПровайдеры:")
    providers = {
        "1": ("openai", "OpenAI (api.openai.com)", "https://api.openai.com/v1", "gpt-4o"),
        "2": ("anthropic", "Anthropic (Claude)", "https://api.anthropic.com", "claude-sonnet-4-20250514"),
        "3": ("zai", "Z.ai (api.z.ai/api/paas/v4)", "https://api.z.ai/api/paas/v4", "glm-4.6"),
        "4": ("ollama", "Ollama (локально, localhost:11434)", "http://localhost:11434/v1", "llama3.2"),
        "5": ("custom", "Свой OpenAI-совместимый endpoint", "", ""),
    }
    
    for k, (name, desc, _, _) in providers.items():
        marker = " ← текущий" if name == config.llm.smart_provider else ""
        print(f"  {k}. {desc}{marker}")
    
    choice = input("\nВыбери провайдера [1-5] (Enter = оставить): ").strip()
    if not choice:
        provider_name = config.llm.smart_provider
        base_url = config.llm.smart_base_url
    elif choice in providers:
        provider_name, _, base_url, _ = providers[choice]
    else:
        print(f"❌ Неверный выбор: {choice}")
        return 1
    
    # Custom base_url
    if provider_name == "custom" or choice == "5":
        custom_url = input(f"Base URL (Enter = '{base_url or ''}'): ").strip()
        if custom_url:
            base_url = custom_url
    
    if not base_url and provider_name != "openai":
        # Для не-openai провайдеров base_url обязателен
        if provider_name == "anthropic":
            base_url = "https://api.anthropic.com"
        elif provider_name == "ollama":
            base_url = "http://localhost:11434/v1"
    
    # === 2. API КЛЮЧ ===
    current_key = config.llm.smart_api_key
    key_prompt = "API ключ"
    if provider_name == "ollama":
        print("\nℹ️  Ollama не требует API ключа, оставь пустым")
        key_prompt = "API ключ (для Ollama — пусто)"
    
    key_masked = "***" + current_key[-4:] if current_key else "пусто"
    api_key = input(f"\n{key_prompt} (Enter = '{key_masked}'): ").strip()
    if not api_key:
        api_key = current_key  # оставляем текущий
    
    # === 3. ПОЛУЧАЕМ СПИСОК МОДЕЛЕЙ ===
    print(f"\n📡 Получаю список моделей от {provider_name}...")
    
    models_list = []
    
    if provider_name == "anthropic":
        # Anthropic не имеет /models endpoint в OpenAI-стиле.
        # Используем предзаготовленный список.
        models_list = [
            "claude-sonnet-4-20250514",
            "claude-opus-4-20250514",
            "claude-3-7-sonnet-20250219",
            "claude-3-5-haiku-20241022",
        ]
        print(f"  ✅ Anthropic: {len(models_list)} моделей (предзаготовленный список)")
    else:
        # OpenAI-совместимый /models endpoint
        try:
            headers = {}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            
            url = f"{base_url}/models" if base_url else "https://api.openai.com/v1/models"
            
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(url, headers=headers)
            
            if resp.status_code == 200:
                data = resp.json()
                # OpenAI: {data: [{id: "gpt-4o", ...}, ...]}
                # Ollama: {models: [{name: "llama3.2", ...}, ...]}
                if "data" in data:
                    models_list = [m["id"] for m in data["data"] if "id" in m]
                elif "models" in data:
                    models_list = [m.get("name", m.get("id", "")) for m in data["models"]]
                
                models_list.sort()
                print(f"  ✅ Получено {len(models_list)} моделей")
            else:
                print(f"  ❌ HTTP {resp.status_code}: {resp.text[:200]}")
                print(f"  Буду использовать дефолтные модели")
        except Exception as e:
            print(f"  ❌ Ошибка: {e}")
            print(f"  Буду использовать дефолтные модели")
    
    # Дефолтные модели если список пуст
    if not models_list:
        if provider_name == "openai":
            models_list = ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-3.5-turbo"]
        elif provider_name == "zai":
            models_list = ["glm-4.6", "glm-4-flash", "glm-4-air"]
        elif provider_name == "ollama":
            models_list = ["llama3.2", "qwen2.5", "mistral"]
        else:
            models_list = ["gpt-4o", "gpt-4o-mini"]
    
    # === 4. ВЫБОР SMART МОДЕЛИ ===
    print(f"\n📋 Доступные модели:")
    for i, m in enumerate(models_list, 1):
        marker = " ← текущая smart" if m == config.llm.smart_model else ""
        print(f"  {i}. {m}{marker}")
    
    # Текущий smart
    current_smart_idx = None
    if config.llm.smart_model in models_list:
        current_smart_idx = models_list.index(config.llm.smart_model) + 1
    
    prompt = f"\nВыбери SMART модель (1-{len(models_list)}"
    if current_smart_idx:
        prompt += f", Enter = {current_smart_idx}"
    prompt += "): "
    smart_choice = input(prompt).strip()
    
    if not smart_choice and current_smart_idx:
        smart_model = config.llm.smart_model
    elif smart_choice.isdigit() and 1 <= int(smart_choice) <= len(models_list):
        smart_model = models_list[int(smart_choice) - 1]
    else:
        # Может пользователь ввёл имя модели напрямую
        smart_model = smart_choice if smart_choice else config.llm.smart_model
    
    print(f"  ✅ Smart: {smart_model}")
    
    # === 5. ВЫБОР CHEAP МОДЕЛИ ===
    # Текущий cheap
    current_cheap_idx = None
    if config.llm.cheap_model in models_list:
        current_cheap_idx = models_list.index(config.llm.cheap_model) + 1
    
    prompt = f"Выбери CHEAP модель (1-{len(models_list)}"
    if current_cheap_idx:
        prompt += f", Enter = {current_cheap_idx}"
    prompt += "): "
    cheap_choice = input(prompt).strip()
    
    if not cheap_choice and current_cheap_idx:
        cheap_model = config.llm.cheap_model
    elif cheap_choice.isdigit() and 1 <= int(cheap_choice) <= len(models_list):
        cheap_model = models_list[int(cheap_choice) - 1]
    else:
        cheap_model = cheap_choice if cheap_choice else config.llm.cheap_model
    
    print(f"  ✅ Cheap: {cheap_model}")
    
    # === 6. ОДИНАКОВЫЙ API КЛЮЧ ДЛЯ SMART И CHEAP? ===
    # Большинство провайдеров: один ключ на все модели
    # Спрашиваем только если пользователь хочет разделить
    same_key = input(
        f"\nИспользовать тот же API ключ для cheap модели? [Y/n]: "
    ).strip().lower()
    
    if same_key in ("n", "no", "н", "нет"):
        cheap_key = input(f"API ключ для cheap модели: ").strip()
    else:
        cheap_key = api_key  # тот же ключ
    
    # === 7. СОХРАНЯЕМ ===
    config.llm.smart_provider = provider_name
    config.llm.smart_model = smart_model
    config.llm.smart_api_key = api_key
    config.llm.smart_base_url = base_url if provider_name != "openai" else None
    
    config.llm.cheap_provider = provider_name
    config.llm.cheap_model = cheap_model
    config.llm.cheap_api_key = cheap_key
    config.llm.cheap_base_url = base_url if provider_name != "openai" else None
    
    config.save(CONFIG_PATH)
    
    # Проверяем что сохранилось
    verify = Config.load(CONFIG_PATH)
    print(f"\n✅ Конфигурация сохранена в {CONFIG_PATH}")
    print(f"   Smart: {verify.llm.smart_provider} / {verify.llm.smart_model}")
    print(f"   Cheap: {verify.llm.cheap_provider} / {verify.llm.cheap_model}")
    print(f"   Smart API key: {'✅ установлен' if verify.llm.smart_api_key else '❌ пустой'}")
    print(f"   Cheap API key: {'✅ установлен' if verify.llm.cheap_api_key else '❌ пустой'}")
    
    # Перезапуск daemon
    print("\n♻️  Перезапускаю daemon...")
    try:
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, timeout=5)
        subprocess.run(["systemctl", "--user", "restart", "caesar-daemon"], capture_output=True, timeout=10)
        print("✅ Daemon перезапущен")
    except Exception as e:
        print(f"⚠️  Не удалось перезапустить: {e}")
        print(f"   Перезапусти вручную: systemctl --user restart caesar-daemon")
    
    return 0


async def cmd_cron(args) -> int:
    """Управление cron задачами: list, add, remove."""
    from caesar.memory.storage import Storage
    from caesar.config import Config
    from datetime import datetime
    import os as _os
    
    config = Config.load()
    storage = Storage()
    
    # user_id — текущий CLI пользователь
    user_id = f"cli-{_os.getuid()}"
    existing = storage.get_user_by_uid(_os.getuid())
    if existing:
        user_id = existing["id"]
    
    action = args.cron_action
    
    if action == "list":
        print("⏰ Cron Tasks")
        print("=" * 50)
        
        try:
            tasks = storage.list_cron_tasks(user_id)
        except Exception as e:
            print(f"❌ DB error: {e}")
            return 1
        
        if not tasks:
            print("Нет cron задач.")
            print("\n💡 Добавить: caesar cron add 'каждый день в 9:00' 'найди новости про AI'")
            return 0
        
        for i, t in enumerate(tasks, 1):
            enabled = "✅" if t.get("enabled", 1) else "❌"
            schedule = t.get("schedule_human") or t.get("schedule", "?")
            task_text = t.get("task_to_execute", "?")
            next_run = t.get("next_run_at", "")
            failures = t.get("consecutive_failures", 0)
            
            print(f"\n[{i}] {enabled} {t['id'][:20]}...")
            print(f"    Schedule: {schedule}")
            print(f"    Task: {task_text[:80]}")
            if next_run:
                print(f"    Next run: {next_run[:19]}")
            if failures > 0:
                print(f"    Failures: {failures}")
        
        print(f"\nВсего: {len(tasks)} задач")
        return 0
    
    elif action == "add":
        schedule_text = args.schedule
        task_text = args.task
        
        print(f"⏰ Добавляю cron задачу:")
        print(f"   Расписание: {schedule_text}")
        print(f"   Задача: {task_text}")
        
        # Проверяем APScheduler
        try:
            from apscheduler.triggers.cron import CronTrigger
        except ImportError:
            print("\n❌ APScheduler не установлен. Установи: caesar enable cron")
            return 1
        
        # Парсим расписание
        from caesar.core.cron import parse_schedule
        
        # Сначала пробуем как русский текст
        parsed = parse_schedule(schedule_text)
        if parsed:
            cron_expr, human_readable = parsed
        else:
            # Может быть уже cron формат "0 9 * * *"
            parts = schedule_text.split()
            if len(parts) == 5:
                cron_expr = schedule_text
                human_readable = schedule_text
            else:
                print(f"\n❌ Не удалось распознать расписание '{schedule_text}'")
                print(f"   Примеры: 'каждый день в 9:00', 'по будням в 18:00', '0 9 * * *'")
                return 1
        
        print(f"   Cron: {cron_expr}")
        print(f"   Human: {human_readable}")
        
        # Добавляем
        try:
            from apscheduler.triggers.cron import CronTrigger
            trigger = CronTrigger.from_crontab(cron_expr, timezone=config.timezone)
            next_run = trigger.get_next_fire_time(None, datetime.now(trigger.timezone))
            next_run_at = next_run.isoformat() if next_run else None
        except Exception as e:
            print(f"\n❌ Invalid cron: {e}")
            return 1
        
        channel_id = f"channel:{user_id}:main"
        
        cron_id = storage.add_cron_task({
            "user_id": user_id,
            "channel_id": channel_id,
            "schedule": cron_expr,
            "schedule_human": human_readable,
            "task_to_execute": task_text,
            "timezone": config.timezone,
            "notify_on_success": 0,
            "notify_on_failure": 1,
            "next_run_at": next_run_at,
        })
        
        print(f"\n✅ Задача добавлена: {cron_id}")
        if next_run:
            print(f"   Следующий запуск: {next_run.strftime('%Y-%m-%d %H:%M')}")
        
        # Перезапуск daemon чтобы подхватил новую задачу
        print("\n♻️  Перезапускаю daemon...")
        try:
            subprocess.run(["systemctl", "--user", "restart", "caesar-daemon"],
                           capture_output=True, timeout=10)
            print("✅ Daemon перезапущен")
        except Exception:
            print("⚠️  Перезапусти вручную: systemctl --user restart caesar-daemon")
        
        return 0
    
    elif action == "remove":
        cron_id = args.cron_id
        
        print(f"🗑️  Удаляю cron задачу: {cron_id}")
        
        try:
            with storage._conn() as conn:
                conn.execute("DELETE FROM cron_tasks WHERE id = ?", (cron_id,))
                conn.commit()
            print(f"✅ Удалено")
        except Exception as e:
            print(f"❌ Error: {e}")
            return 1
        
        # Перезапуск daemon
        print("\n♻️  Перезапускаю daemon...")
        try:
            subprocess.run(["systemctl", "--user", "restart", "caesar-daemon"],
                           capture_output=True, timeout=10)
            print("✅ Daemon перезапущен")
        except Exception:
            pass
        
        return 0
    
    return 1


async def cmd_skill(args) -> int:
    """Управление скиллами (L4)."""
    from caesar.memory.storage import Storage
    from caesar.memory.l4 import L4Skills
    from caesar.config import SKILLS_DIR
    
    storage = Storage()
    l4 = L4Skills(storage, skills_dir=SKILLS_DIR)
    action = args.skill_action
    
    if action == "list":
        print("📚 Skills (L4)")
        print("=" * 50)
        skills = l4.list_skills(only_enabled=False)
        if not skills:
            print("Нет скиллов.")
            print("\n💡 Скиллы создаются агентом через tool-call (skill_save)")
            print("   или вручную: YAML файлы в ~/.local/share/caesar/skills/")
            return 0
        
        for i, s in enumerate(skills, 1):
            enabled = "✅" if not s.get("broken", 0) else "❌"
            trigger = s.get("trigger", "?")
            version = s.get("version", 1)
            success = s.get("success_count", 0)
            fail = s.get("failure_count", 0)
            print(f"\n[{i}] {enabled} {s['name']} v{version}")
            print(f"    Trigger: {trigger}")
            print(f"    Stats: ✅{success} ❌{fail}")
        
        print(f"\nВсего: {len(skills)} скиллов")
        return 0
    
    elif action == "show":
        name = args.name
        skill = l4.get_skill(name)
        if not skill:
            print(f"❌ Скилл '{name}' не найден")
            return 1
        
        print(f"📚 Skill: {skill.name} v{skill.version}")
        print("=" * 50)
        print(f"Trigger: {skill.trigger}")
        print(f"Success: {skill.success_count}, Failures: {skill.failure_count}")
        print(f"Broken: {'YES' if skill.broken else 'no'}")
        
        if skill.exact_recipe:
            print(f"\nRecipe ({len(skill.exact_recipe)} steps):")
            for i, step in enumerate(skill.exact_recipe, 1):
                step_type = step.get("type", "script")
                desc = step.get("description", step.get("command", step.get("prompt", "?")))
                print(f"  {i}. [{step_type}] {desc[:80]}")
        
        if skill.anti_patterns:
            print(f"\nAnti-patterns ({len(skill.anti_patterns)}):")
            for ap in skill.anti_patterns:
                if isinstance(ap, dict):
                    print(f"  - {ap.get('error', ap.get('step', '?'))[:80]}")
                else:
                    print(f"  - {str(ap)[:80]}")
        
        if skill.pitfalls:
            print(f"\nPitfalls:")
            for p in skill.pitfalls:
                print(f"  - {p}")
        
        if skill.yaml_path:
            print(f"\nYAML: {skill.yaml_path}")
        
        return 0
    
    elif action == "remove":
        name = args.name
        print(f"🗑️  Удаляю скилл: {name}")
        
        skill = l4.get_skill(name)
        if not skill:
            print(f"❌ Скилл '{name}' не найден")
            return 1
        
        # Удаляем YAML
        if skill.yaml_path:
            from pathlib import Path
            yaml_path = Path(skill.yaml_path)
            if yaml_path.exists():
                yaml_path.unlink()
                print(f"  ✅ YAML удалён: {yaml_path}")
        
        # Удаляем из БД
        with storage._conn() as conn:
            conn.execute("DELETE FROM l4_skills WHERE name = ?", (name,))
            conn.commit()
        
        print(f"  ✅ Скилл '{name}' удалён из БД")
        return 0
    
    return 1


async def cmd_doctor(args) -> int:
    """Health check — проверить систему."""
    from caesar.memory.storage import Storage
    from caesar.config import Config, SOCKET_PATH, DB_PATH, SKILLS_DIR
    from pathlib import Path
    import os as _os
    
    print("🩺 Caesar Health Check")
    print("=" * 50)
    
    issues = []
    ok_count = 0
    
    # 1. Daemon
    print("\n1. Daemon:")
    if SOCKET_PATH.exists():
        print("   ✅ Socket существует")
        ok_count += 1
    else:
        print("   ❌ Socket не найден — daemon не запущен?")
        issues.append("Daemon: socket не найден")
    
    # systemd
    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-active", "caesar-daemon"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            print("   ✅ systemd: active")
            ok_count += 1
        else:
            print(f"   ❌ systemd: {result.stdout.strip()}")
            issues.append("Daemon: systemd не active")
    except Exception:
        print("   ⚠️ systemd не доступен")
    
    # 2. Config
    print("\n2. Config:")
    config = Config.load()
    if config.llm.smart_api_key:
        print(f"   ✅ Smart LLM: {config.llm.smart_provider} / {config.llm.smart_model}")
        ok_count += 1
    else:
        print("   ❌ Smart LLM API key не установлен")
        issues.append("Config: smart_api_key пустой")
    
    if config.llm.cheap_api_key:
        print(f"   ✅ Cheap LLM: {config.llm.cheap_provider} / {config.llm.cheap_model}")
        ok_count += 1
    elif config.llm.smart_api_key:
        print(f"   ✅ Cheap LLM: авто (same as smart: {config.llm.smart_provider} / {config.llm.smart_model})")
        ok_count += 1
    else:
        print("   ⚠️ Cheap LLM не настроен (caesar models)")
        issues.append("Config: cheap_api_key пустой — analyzer не работает")
    
    if config.telegram.bot_token:
        print("   ✅ Telegram: настроен")
        ok_count += 1
    else:
        print("   ❌ Telegram bot token не установлен")
        issues.append("Config: telegram bot_token пустой")
    
    # 3. Features
    print("\n3. Features:")
    if getattr(config.stt, "enabled", False):
        try:
            import faster_whisper
            print(f"   ✅ STT: включён (faster-whisper {faster_whisper.__version__})")
            ok_count += 1
        except ImportError:
            print("   ⚠️ STT: включён в config но faster-whisper не установлен")
            issues.append("STT: faster-whisper не установлен (caesar enable stt)")
    else:
        print("   ℹ️  STT: отключён")
    
    if getattr(config.l3, "enabled", False):
        try:
            import sentence_transformers
            print(f"   ✅ L3: включён (sentence-transformers {sentence_transformers.__version__})")
            ok_count += 1
        except ImportError:
            print("   ⚠️ L3: включён в config но sentence-transformers не установлен")
            issues.append("L3: sentence-transformers не установлен (caesar enable l3)")
    else:
        print("   ℹ️  L3: отключён")
    
    if getattr(config.cron, "enabled", False):
        try:
            import apscheduler
            print(f"   ✅ Cron: включён (APScheduler {apscheduler.__version__})")
            ok_count += 1
        except ImportError:
            print("   ⚠️ Cron: включён в config но APScheduler не установлен")
            issues.append("Cron: APScheduler не установлен (caesar enable cron)")
    else:
        print("   ℹ️  Cron: отключён")
    
    # 4. Database
    print("\n4. Database:")
    if DB_PATH.exists():
        size_mb = DB_PATH.stat().st_size / (1024 * 1024)
        print(f"   ✅ DB: {DB_PATH} ({size_mb:.1f} MB)")
        ok_count += 1
        
        # Проверяем размер
        if size_mb > 500:
            print(f"   ⚠️ DB большая ({size_mb:.0f} MB) — caesar db vacuum")
            issues.append(f"DB: {size_mb:.0f}MB — рекомендуется vacuum")
    else:
        print("   ❌ DB не найдена")
        issues.append("DB: файл не существует")
    
    # 5. Disk space
    print("\n5. Disk space:")
    try:
        stat = os.statvfs(str(DB_PATH.parent))
        free_gb = (stat.f_bavail * stat.f_frsize) / (1024 ** 3)
        if free_gb > 1:
            print(f"   ✅ Свободно: {free_gb:.1f} GB")
            ok_count += 1
        else:
            print(f"   ❌ Мало места: {free_gb:.1f} GB")
            issues.append(f"Disk: только {free_gb:.1f}GB свободно")
    except Exception:
        pass
    
    # 6. Skills
    print("\n6. Skills:")
    storage = Storage()
    skills = storage.list_skills()
    broken = [s for s in skills if s.get("broken", 0)]
    if skills:
        print(f"   ✅ {len(skills)} скиллов ({len(broken)} broken)")
        ok_count += 1
    else:
        print("   ℹ️  Нет скиллов")
    
    # Summary
    print("\n" + "=" * 50)
    if not issues:
        print(f"✅ Всё хорошо! ({ok_count} checks passed)")
    else:
        print(f"⚠️ {len(issues)} issue(s) found:")
        for issue in issues:
            print(f"   - {issue}")
    
    return 0 if not issues else 1


async def cmd_db(args) -> int:
    """Обслуживание БД."""
    from caesar.config import DB_PATH
    from caesar.memory.storage import Storage
    from datetime import datetime
    import shutil
    
    action = args.db_action
    
    if action == "cleanup":
        print("🧹 Cleanup — удаляю старые completed tasks (>30 дней)...")
        storage = Storage()
        
        with storage._conn() as conn:
            cur = conn.execute(
                """DELETE FROM tasks 
                   WHERE status IN ('completed', 'failed') 
                   AND completed_at < datetime('now', '-30 days')"""
            )
            conn.commit()
            deleted = cur.rowcount
        
        # Также чистим старые token_usage
        with storage._conn() as conn:
            cur = conn.execute(
                """DELETE FROM token_usage 
                   WHERE timestamp < datetime('now', '-30 days')"""
            )
            conn.commit()
            deleted_tokens = cur.rowcount
        
        print(f"   ✅ Удалено: {deleted} tasks, {deleted_tokens} token_usage records")
        return 0
    
    elif action == "vacuum":
        print("🗜️  VACUUM — оптимизирую SQLite...")
        storage = Storage()
        
        size_before = DB_PATH.stat().st_size / (1024 * 1024)
        print(f"   Размер до: {size_before:.1f} MB")
        
        with storage._conn() as conn:
            conn.execute("VACUUM")
            conn.commit()
        
        size_after = DB_PATH.stat().st_size / (1024 * 1024)
        saved = size_before - size_after
        print(f"   Размер после: {size_after:.1f} MB")
        if saved > 0.1:
            print(f"   ✅ Сэкономлено: {saved:.1f} MB")
        else:
            print(f"   ℹ️  БД уже оптимизирована")
        return 0
    
    elif action == "backup":
        print("💾 Backup — создаю резервную копию...")
        backup_path = DB_PATH.with_suffix(f".db.backup.{datetime.now().strftime('%Y%m%d_%H%M%S')}")
        shutil.copy2(DB_PATH, backup_path)
        size_mb = backup_path.stat().st_size / (1024 * 1024)
        print(f"   ✅ Backup: {backup_path} ({size_mb:.1f} MB)")
        return 0
    
    elif action == "stats":
        print("📊 DB Statistics")
        print("=" * 50)
        storage = Storage()
        
        size_mb = DB_PATH.stat().st_size / (1024 * 1024)
        print(f"DB file: {DB_PATH}")
        print(f"Size: {size_mb:.1f} MB")
        
        with storage._conn() as conn:
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
            
            print(f"\nTables ({len(tables)}):")
            for t in tables:
                count = conn.execute(f"SELECT COUNT(*) FROM {t['name']}").fetchone()
                cnt = count[0] if count else 0
                if cnt > 0:
                    print(f"  {t['name']:30s} {cnt:>8} rows")
        
        return 0
    
    elif action == "audit":
        """Умная ревизия — найти дубликаты, stale, устаревшие данные."""
        storage = Storage()
        fix = getattr(args, "fix", False)
        print("🔍 DB Audit — умная ревизия")
        print("=" * 60)
        
        issues_found = 0
        issues_fixed = 0
        
        # 1. Дубликаты L3 чанков (одинаковый hash)
        print("\n1. Дубликаты L3 чанков (одинаковый hash):")
        try:
            with storage._conn() as conn:
                rows = conn.execute(
                    """SELECT json_extract(chunk_metadata, '$.hash') as hash, 
                              COUNT(*) as cnt, 
                              GROUP_CONCAT(id) as ids,
                              MIN(content) as sample
                       FROM l3_chunks 
                       WHERE chunk_metadata LIKE '%hash%'
                       GROUP BY hash
                       HAVING cnt > 1
                       ORDER BY cnt DESC
                       LIMIT 20"""
                ).fetchall()
            
            if rows:
                for r in rows:
                    d = dict(r)
                    ids = d["ids"].split(",")
                    print(f"   ⚠️ hash={d['hash']}: {d['cnt']} дубликатов")
                    print(f"      sample: {d['sample'][:80]}...")
                    if fix:
                        # Оставляем первый, удаляем остальные
                        keep = ids[0]
                        delete_ids = ids[1:]
                        placeholders = ",".join("?" * len(delete_ids))
                        with storage._conn() as conn:
                            conn.execute(
                                f"DELETE FROM l3_chunks WHERE id IN ({placeholders})",
                                delete_ids,
                            )
                            conn.commit()
                        print(f"      ✅ Удалено {len(delete_ids)} дубликатов (оставлен {keep})")
                        issues_fixed += len(delete_ids)
                        issues_found += len(delete_ids)
                    else:
                        issues_found += len(ids) - 1
            else:
                print("   ✅ Дубликатов не найдено")
        except Exception as e:
            print(f"   ❌ Ошибка: {e}")
        
        # 2. L3 чанки без embedding (битые)
        print("\n2. L3 чанки без embedding (битые):")
        try:
            with storage._conn() as conn:
                row = conn.execute(
                    """SELECT COUNT(*) as cnt FROM l3_chunks 
                       WHERE chunk_metadata NOT LIKE '%embedding%' 
                       OR json_extract(chunk_metadata, '$.embedding') IS NULL"""
                ).fetchone()
                broken = row["cnt"] if row else 0
            
            if broken > 0:
                print(f"   ⚠️ {broken} чанков без embedding (поиск не найдёт)")
                if fix:
                    with storage._conn() as conn:
                        conn.execute(
                            """DELETE FROM l3_chunks 
                               WHERE chunk_metadata NOT LIKE '%embedding%'
                               OR json_extract(chunk_metadata, '$.embedding') IS NULL"""
                        )
                        conn.commit()
                    print(f"   ✅ Удалено {broken} битых чанков")
                    issues_fixed += broken
                issues_found += broken
            else:
                print("   ✅ Все чанки имеют embedding")
        except Exception as e:
            print(f"   ❌ Ошибка: {e}")
        
        # 3. Старые completed tasks (>7 дней)
        print("\n3. Старые completed tasks (>7 дней):")
        try:
            with storage._conn() as conn:
                row = conn.execute(
                    """SELECT COUNT(*) as cnt FROM tasks 
                       WHERE status IN ('completed', 'failed') 
                       AND completed_at < datetime('now', '-7 days')"""
                ).fetchone()
                old = row["cnt"] if row else 0
            
            if old > 0:
                print(f"   ⚠️ {old} старых tasks (>7 дней) — занимают место")
                if fix:
                    with storage._conn() as conn:
                        conn.execute(
                            """DELETE FROM tasks 
                               WHERE status IN ('completed', 'failed') 
                               AND completed_at < datetime('now', '-7 days')"""
                        )
                        conn.commit()
                    print(f"   ✅ Удалено {old} старых tasks")
                    issues_fixed += old
                issues_found += old
            else:
                print("   ✅ Нет старых tasks")
        except Exception as e:
            print(f"   ❌ Ошибка: {e}")
        
        # 4. Старые task_actions (>7 дней)
        print("\n4. Старые task_actions (>7 дней):")
        try:
            with storage._conn() as conn:
                row = conn.execute(
                    """SELECT COUNT(*) as cnt FROM task_actions 
                       WHERE timestamp < datetime('now', '-7 days')"""
                ).fetchone()
                old_actions = row["cnt"] if row else 0
            
            if old_actions > 0:
                print(f"   ⚠️ {old_actions} старых task_actions (>7 дней)")
                if fix:
                    with storage._conn() as conn:
                        conn.execute(
                            """DELETE FROM task_actions 
                               WHERE timestamp < datetime('now', '-7 days')"""
                        )
                        conn.commit()
                    print(f"   ✅ Удалено {old_actions} старых task_actions")
                    issues_fixed += old_actions
                issues_found += old_actions
            else:
                print("   ✅ Нет старых task_actions")
        except Exception as e:
            print(f"   ❌ Ошибка: {e}")
        
        # 5. Старые token_usage (>7 дней)
        print("\n5. Старые token_usage (>7 дней):")
        try:
            with storage._conn() as conn:
                row = conn.execute(
                    """SELECT COUNT(*) as cnt FROM token_usage 
                       WHERE timestamp < datetime('now', '-7 days')"""
                ).fetchone()
                old_tokens = row["cnt"] if row else 0
            
            if old_tokens > 0:
                print(f"   ⚠️ {old_tokens} старых token_usage (>7 дней)")
                if fix:
                    with storage._conn() as conn:
                        conn.execute(
                            """DELETE FROM token_usage 
                               WHERE timestamp < datetime('now', '-7 days')"""
                        )
                        conn.commit()
                    print(f"   ✅ Удалено {old_tokens} старых token_usage")
                    issues_fixed += old_tokens
                issues_found += old_tokens
            else:
                print("   ✅ Нет старых token_usage")
        except Exception as e:
            print(f"   ❌ Ошибка: {e}")
        
        # 6. Устаревшие skills (broken=True)
        print("\n6. Скиллы помеченные broken:")
        try:
            with storage._conn() as conn:
                rows = conn.execute(
                    """SELECT name, version, failure_count FROM l4_skills 
                       WHERE broken = 1"""
                ).fetchall()
            
            if rows:
                for r in rows:
                    print(f"   ⚠️ '{r['name']}' v{r['version']} — broken (failures: {r['failure_count']})")
                if fix:
                    print("   ℹ️  Broken skills не удаляются автоматически.")
                    print("      Используй: caesar skill remove <name>")
                    print("      Или обнови: агент создаст новую версию через skill_save")
                issues_found += len(rows)
            else:
                print("   ✅ Нет broken skills")
        except Exception as e:
            print(f"   ❌ Ошибка: {e}")
        
        # 7. Дубликаты entities (case-insensitive)
        print("\n7. Дубликаты entities (case-insensitive):")
        try:
            with storage._conn() as conn:
                rows = conn.execute(
                    """SELECT LOWER(name) as lower_name, entity_type,
                              COUNT(*) as cnt,
                              GROUP_CONCAT(id) as ids,
                              SUM(mention_count) as total
                       FROM kg_entities
                       GROUP BY LOWER(name), entity_type
                       HAVING cnt > 1
                       LIMIT 20"""
                ).fetchall()
            
            if rows:
                for r in rows:
                    d = dict(r)
                    print(f"   ⚠️ '{d['lower_name']}' ({d['entity_type']}): {d['cnt']} дубликатов, {d['total']} mentions total")
                    if fix:
                        ids = d["ids"].split(",")
                        keep_id = ids[0]
                        delete_ids = ids[1:]
                        # Переносим relations
                        for did in delete_ids:
                            del_row = conn.execute(
                                "SELECT name FROM kg_entities WHERE id = ?", (did,)
                            ).fetchone()
                            if del_row:
                                del_name = del_row["name"]
                                with storage._conn() as conn2:
                                    conn2.execute(
                                        "UPDATE kg_relations SET from_entity = ? WHERE from_entity = ?",
                                        (d["lower_name"], del_name),
                                    )
                                    conn2.execute(
                                        "UPDATE kg_relations SET to_entity = ? WHERE to_entity = ?",
                                        (d["lower_name"], del_name),
                                    )
                                    conn2.execute("DELETE FROM kg_entities WHERE id = ?", (did,))
                                    conn2.commit()
                        # Обновляем mention_count
                        with storage._conn() as conn2:
                            conn2.execute(
                                "UPDATE kg_entities SET mention_count = ? WHERE id = ?",
                                (d["total"], keep_id),
                            )
                            conn2.commit()
                        print(f"      ✅ Объединено, удалено {len(delete_ids)} дубликатов")
                        issues_fixed += len(delete_ids)
                        issues_found += len(delete_ids)
                    else:
                        issues_found += d["cnt"] - 1
            else:
                print("   ✅ Дубликатов entities не найдено")
        except Exception as e:
            print(f"   ❌ Ошибка: {e}")
        
        # 8. Stale entities (>60 дней без упоминаний)
        print("\n8. Stale entities (>60 дней без упоминаний):")
        try:
            with storage._conn() as conn:
                rows = conn.execute(
                    """SELECT name, entity_type, last_seen, mention_count 
                       FROM kg_entities 
                       WHERE last_seen < datetime('now', '-60 days')
                       ORDER BY last_seen ASC
                       LIMIT 20"""
                ).fetchall()
            
            if rows:
                for r in rows:
                    d = dict(r)
                    print(f"   ⚠️ '{d['name']}' ({d['entity_type']}): last seen {d['last_seen'][:10]}, mentions={d['mention_count']}")
                if fix:
                    print("   ℹ️  Stale entities не удаляются автоматически.")
                    print("      Это могут быть полезные старые данные.")
                    print("      Для удаления: caesar l3 delete_by_query 'старая тема'")
                issues_found += len(rows)
            else:
                print("   ✅ Нет stale entities")
        except Exception as e:
            print(f"   ❌ Ошибка: {e}")
        
        # Summary
        print("\n" + "=" * 60)
        if issues_found == 0:
            print("✅ БД чистая — проблем не найдено!")
        else:
            print(f"Найдено проблем: {issues_found}")
            if fix:
                print(f"Исправлено: {issues_fixed}")
                print("\n💡 Рекомендуется: caesar db vacuum (оптимизировать размер)")
            else:
                print(f"\n💡 Для исправления: caesar db audit --fix")
        
        return 0
    
    elif action == "archive-logs":
        """Архивировать логи старше 10 дней в .gz файл."""
        import gzip
        import shutil as _shutil
        from pathlib import Path
        from datetime import timedelta
        from caesar.config import LOG_DIR
        
        print("📦 Архивирую логи старше 10 дней...")
        
        cutoff = datetime.now() - timedelta(days=10)
        archive_dir = LOG_DIR / "archives"
        archive_dir.mkdir(parents=True, exist_ok=True)
        
        archived = 0
        total_saved = 0
        
        for log_file in LOG_DIR.glob("*.log"):
            if log_file.stat().st_mtime < cutoff.timestamp():
                # Читаем старые строки
                with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
                
                # Разделяем: старые (<cutoff) и новые
                old_lines = []
                new_lines = []
                for line in lines:
                    try:
                        # Парсим timestamp из начала строки
                        ts_str = line[:23].strip()
                        if ts_str:
                            ts = datetime.strptime(ts_str[:19], "%Y-%m-%d %H:%M:%S")
                            if ts < cutoff:
                                old_lines.append(line)
                            else:
                                new_lines.append(line)
                        else:
                            new_lines.append(line)
                    except (ValueError, IndexError):
                        new_lines.append(line)
                
                if old_lines:
                    # Архивируем старые
                    archive_name = f"{log_file.stem}_{cutoff.strftime('%Y%m%d')}.log.gz"
                    archive_path = archive_dir / archive_name
                    with gzip.open(archive_path, "at", encoding="utf-8") as gz:
                        gz.writelines(old_lines)
                    
                    # Перезаписываем лог только новыми строками
                    with open(log_file, "w", encoding="utf-8") as f:
                        f.writelines(new_lines)
                    
                    old_size = sum(len(l) for l in old_lines)
                    new_size = sum(len(l) for l in new_lines)
                    total_saved += old_size
                    archived += 1
                    
                    print(f"   ✅ {log_file.name}: {len(old_lines)} строк → {archive_path.name}")
                    print(f"      Осталось: {len(new_lines)} строк")
        
        if archived == 0:
            print("   ℹ️  Нет логов старше 10 дней")
        else:
            print(f"\n✅ Архивировано: {archived} файлов, сэкономлено {total_saved / 1024:.0f} KB")
            print(f"   Архивы в: {archive_dir}")
        
        return 0
    
    return 1


async def cmd_config(args) -> int:
    """Проверить валидность config.yaml."""
    from caesar.config import Config, CONFIG_PATH
    import yaml as _yaml
    
    print("🔧 Config Check")
    print("=" * 50)
    
    issues = []
    
    # 1. Существует ли файл
    if not CONFIG_PATH.exists():
        print(f"❌ Config файл не найден: {CONFIG_PATH}")
        print("   Запусти: caesar setup")
        return 1
    
    print(f"📁 Файл: {CONFIG_PATH}")
    
    # 2. Парсится ли YAML
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = _yaml.safe_load(f) or {}
        print("   ✅ YAML валиден")
    except _yaml.YAMLError as e:
        print(f"   ❌ YAML ошибка: {e}")
        return 1
    
    # 3. Загружаем через Config.load
    config = Config.load()
    
    # 4. LLM
    print("\n🤖 LLM:")
    if not config.llm.smart_api_key:
        print("   ❌ Smart API key пустой")
        issues.append("smart_api_key пустой — агент не будет работать")
    else:
        print(f"   ✅ Smart: {config.llm.smart_provider} / {config.llm.smart_model}")
    
    if not config.llm.cheap_api_key:
        print("   ⚠️ Cheap API key пустой — analyzer не работает")
        issues.append("cheap_api_key пустой — cheap analyzer отключён")
    else:
        print(f"   ✅ Cheap: {config.llm.cheap_provider} / {config.llm.cheap_model}")
    
    # 5. Telegram
    print("\n📡 Telegram:")
    if not config.telegram.bot_token:
        print("   ❌ Bot token пустой")
        issues.append("telegram.bot_token пустой — TG не работает")
    else:
        print("   ✅ Bot token установлен")
    
    # 6. Features
    print("\n🔌 Features:")
    if getattr(config.stt, "enabled", False):
        try:
            import faster_whisper
            print(f"   ✅ STT: включён")
        except ImportError:
            print("   ⚠️ STT: включён в config но faster-whisper не установлен")
            issues.append("STT: caesar enable stt")
    else:
        print("   ℹ️  STT: отключён")
    
    if getattr(config.l3, "enabled", False):
        try:
            import sentence_transformers
            print(f"   ✅ L3: включён")
        except ImportError:
            print("   ⚠️ L3: включён но sentence-transformers не установлен")
            issues.append("L3: caesar enable l3")
    else:
        print("   ℹ️  L3: отключён")
    
    if getattr(config.cron, "enabled", False):
        try:
            import apscheduler
            print(f"   ✅ Cron: включён")
        except ImportError:
            print("   ⚠️ Cron: включён но APScheduler не установлен")
            issues.append("Cron: caesar enable cron")
    else:
        print("   ℹ️  Cron: отключён")
    
    # 7. Противоречия
    print("\n🔍 Проверка противоречий:")
    if config.llm.smart_provider != config.llm.cheap_provider:
        if config.llm.smart_base_url != config.llm.cheap_base_url:
            if config.llm.smart_api_key == config.llm.cheap_api_key and config.llm.smart_api_key:
                print("   ⚠️ Different providers но одинаковый API key — может не работать")
                issues.append("smart и cheap имеют разные провайдеры но один ключ")
    
    if config.llm.smart_model == config.llm.cheap_model:
        print("   ℹ️  Smart и cheap модели одинаковые — нет экономии токенов")
        issues.append("smart_model == cheap_model — нет экономии через cheap analyzer")
    
    # Summary
    print("\n" + "=" * 50)
    if not issues:
        print("✅ Конфигурация валидна!")
    else:
        print(f"⚠️ {len(issues)} issue(s):")
        for i in issues:
            print(f"   - {i}")
    
    return 0 if not issues else 1


async def cmd_kg(args) -> int:
    """Управление Knowledge Graph."""
    from caesar.memory.storage import Storage
    from caesar.memory.knowledge_graph import KnowledgeGraph
    import os as _os
    
    storage = Storage()
    kg = KnowledgeGraph(storage)
    
    # user_id
    user_id = f"cli-{_os.getuid()}"
    existing = storage.get_user_by_uid(_os.getuid())
    if existing:
        user_id = existing["id"]
    
    action = args.kg_action
    
    if action == "stats":
        print("🧠 Knowledge Graph Stats")
        print("=" * 50)
        stats = kg.get_stats(user_id)
        print(f"Entities: {stats['total_entities']}")
        print(f"Relations: {stats['total_relations']}")
        if stats.get("by_type"):
            print("\nBy type:")
            for t, c in sorted(stats["by_type"].items(), key=lambda x: -x[1]):
                print(f"  {t:15s} {c:>5}")
        return 0
    
    elif action == "search":
        name = args.name
        print(f"🔍 KG Search: '{name}'")
        print("=" * 50)
        entities = kg.search_entities(user_id, name, limit=10)
        if not entities:
            print("Не найдено.")
            return 0
        for i, e in enumerate(entities, 1):
            print(f"\n[{i}] {e['name']} ({e['entity_type']})")
            print(f"    mentions: {e.get('mention_count', 0)}")
            print(f"    first seen: {e.get('first_seen', '')[:10]}")
            print(f"    last seen: {e.get('last_seen', '')[:10]}")
            # Relations
            rels = kg.get_relations(user_id, e["name"], "both")
            if rels:
                print(f"    relations ({len(rels)}):")
                for r in rels[:5]:
                    direction = "→" if r["from_entity"] == e["name"] else "←"
                    other = r["to_entity"] if r["from_entity"] == e["name"] else r["from_entity"]
                    print(f"      {direction} {r['relation_type']} → {other}")
        return 0
    
    elif action == "graph":
        name = args.name
        depth = args.depth
        print(f"🕸️  KG Graph: '{name}' (depth={depth})")
        print("=" * 50)
        result = kg.traverse_graph(user_id, name, depth=depth)
        if not result["nodes"]:
            print(f"Entity '{name}' не найден.")
            return 0
        print(f"\nNodes ({len(result['nodes'])}):")
        for n in result["nodes"]:
            indent = "  " * n["distance"]
            print(f"  {indent}[{n['distance']}] {n['name']} ({n['type']})")
        print(f"\nEdges ({len(result['edges'])}):")
        for e in result["edges"]:
            print(f"  {e['from']} --{e['type']}--> {e['to']}")
        return 0
    
    elif action == "stale":
        print("⏰ Stale entities (>30 дней без упоминаний)")
        print("=" * 50)
        stale = kg.get_stale_entities(user_id, days=30)
        if not stale:
            print("Нет stale entities.")
            return 0
        for i, e in enumerate(stale, 1):
            print(f"  [{i}] {e['name']} ({e['entity_type']})")
            print(f"      last seen: {e.get('last_seen', '')[:10]}")
            print(f"      mentions: {e.get('mention_count', 0)}")
        print(f"\nВсего: {len(stale)} stale entities")
        print("💡 Для удаления: caesar kg cleanup")
        return 0
    
    elif action == "cleanup":
        print("🧹 Cleanup — удаляю stale entities (>60 дней)")
        stale = kg.get_stale_entities(user_id, days=60)
        if not stale:
            print("Нет stale entities для удаления.")
            return 0
        deleted = 0
        for e in stale:
            try:
                with storage._conn() as conn:
                    # Удаляем relations
                    conn.execute(
                        "DELETE FROM kg_relations WHERE from_entity = ? OR to_entity = ?",
                        (e["name"], e["name"]),
                    )
                    # Удаляем entity
                    conn.execute("DELETE FROM kg_entities WHERE id = ?", (e["id"],))
                    conn.commit()
                deleted += 1
            except Exception as ex:
                print(f"  ❌ Failed to delete {e['name']}: {ex}")
        print(f"✅ Удалено: {deleted} stale entities")
        return 0
    
    return 1


async def main_async() -> int:
    parser = argparse.ArgumentParser(description="Caesar management commands")
    subparsers = parser.add_subparsers(dest="command")
    
    # setup
    p_setup = subparsers.add_parser("setup", help="Запустить setup wizard (LLM, TG, режим)")
    
    # update
    p_update = subparsers.add_parser("update", help="Обновить агент через git pull")
    p_update.add_argument("-y", "--yes", action="store_true", help="Без подтверждения")
    p_update.add_argument("--no-restart", action="store_true", help="Не перезапускать daemon (для TG update)")
    
    # rollback
    p_rollback = subparsers.add_parser("rollback", help="Откатиться к предыдущей версии")
    p_rollback.add_argument("-y", "--yes", action="store_true", help="Без подтверждения")
    
    # uninstall
    p_uninstall = subparsers.add_parser("uninstall", help="Удалить агент")
    p_uninstall.add_argument("-y", "--yes", action="store_true", help="Без подтверждения")
    p_uninstall.add_argument("--keep-data", action="store_true", help="Сохранить данные")
    
    # permissions
    p_perms = subparsers.add_parser("permissions", help="Управление разрешениями")
    p_perms.add_argument("permissions_action", choices=["list", "revoke", "reset"])
    p_perms.add_argument("--pattern", help="Шаблон для отзыва")
    
    # stats
    p_stats = subparsers.add_parser("stats", help="Статистика по токенам")
    p_stats.add_argument("--task", help="ID конкретной задачи")
    p_stats.add_argument("--today", action="store_true", help="Только за сегодня")
    p_stats.add_argument("--week", action="store_true", help="За последние 7 дней")
    
    # config check
    p_config = subparsers.add_parser("config", help="Проверить валидность config.yaml")
    p_config.add_argument("config_action", choices=["check"], help="Проверить конфигурацию")
    
    # enable
    p_enable = subparsers.add_parser("enable", help="Включить фичу одной командой (stt/voice)")
    p_enable.add_argument("feature", help="Фича для включения: stt (или voice — alias)")
    p_enable.add_argument("--no-restart", action="store_true", help="Не перезапускать daemon")
    
    # l3 — управление векторной памятью
    p_l3 = subparsers.add_parser("l3", help="Управление L3 векторной памятью")
    p_l3_sub = p_l3.add_subparsers(dest="l3_action", required=True)
    p_l3_sub.add_parser("status", help="Статус L3: чанки, модель, работает ли")
    p_l3_search = p_l3_sub.add_parser("search", help="Ручной поиск (дебаг)")
    p_l3_search.add_argument("query", help="Что искать")
    p_l3_search.add_argument("--user", default="", help="user_id (по умолчанию cli-{uid})")
    p_l3_search.add_argument("--limit", type=int, default=5, help="Сколько результатов")
    p_l3_show = p_l3_sub.add_parser("show", help="Показать содержимое чанков (дебаг кодировки)")
    p_l3_show.add_argument("--limit", type=int, default=10, help="Сколько чанков показать")
    p_l3_show.add_argument("--full", action="store_true", help="Показать полный текст (не обрезать)")
    p_l3_sub.add_parser("reindex", help="Переиндексировать все чанки (новый chunk_size)")
    p_l3_sub.add_parser("clear", help="Удалить ВСЕ чанки L3 (осторожно!)")
    p_l3_fix = p_l3_sub.add_parser("fix-encoding", help="Попробовать восстановить кракозябры в чанках")
    p_l3_fix.add_argument("--dry-run", action="store_true", help="Только показать, не сохранять")
    
    # models — настройка LLM моделей
    p_models = subparsers.add_parser("models", help="Настроить smart и cheap модели")
    p_models_sub = p_models.add_subparsers(dest="models_action")
    p_models_sub.add_parser("list", help="Показать текущие модели и доступные")
    # без subcommand — интерактивная настройка (как раньше)
    
    # cron — управление планировщиком
    p_cron = subparsers.add_parser("cron", help="Управление cron задачами")
    p_cron_sub = p_cron.add_subparsers(dest="cron_action", required=True)
    p_cron_sub.add_parser("list", help="Список cron задач")
    p_cron_add = p_cron_sub.add_parser("add", help="Добавить cron задачу")
    p_cron_add.add_argument("schedule", help="Расписание: 'каждый день в 9:00' или cron '0 9 * * *'")
    p_cron_add.add_argument("task", help="Что делать: 'найди новости про AI'")
    p_cron_remove = p_cron_sub.add_parser("remove", help="Удалить cron задачу")
    p_cron_remove.add_argument("cron_id", help="ID задачи")
    
    # skill — управление скиллами
    p_skill = subparsers.add_parser("skill", help="Управление скиллами (L4)")
    p_skill_sub = p_skill.add_subparsers(dest="skill_action", required=True)
    p_skill_sub.add_parser("list", help="Список скиллов")
    p_skill_show = p_skill_sub.add_parser("show", help="Показать скилл")
    p_skill_show.add_argument("name", help="Имя скилла")
    p_skill_remove = p_skill_sub.add_parser("remove", help="Удалить скилл")
    p_skill_remove.add_argument("name", help="Имя скилла")
    
    # doctor — health check
    p_doctor = subparsers.add_parser("doctor", help="Health check — проверить систему")
    
    # db — database maintenance
    p_db = subparsers.add_parser("db", help="Обслуживание БД")
    p_db_sub = p_db.add_subparsers(dest="db_action", required=True)
    p_db_sub.add_parser("cleanup", help="Удалить старые completed tasks (>30 дней)")
    p_db_sub.add_parser("vacuum", help="VACUUM SQLite (оптимизация)")
    p_db_sub.add_parser("backup", help="Резервная копия БД")
    p_db_sub.add_parser("stats", help="Размер и статистика БД")
    p_db_audit = p_db_sub.add_parser("audit", help="Умная ревизия: дубликаты, stale, устаревшие данные")
    p_db_audit.add_argument("--fix", action="store_true", help="Применить фиксы (без --fix только показать)")
    p_db_sub.add_parser("archive-logs", help="Архивировать логи старше 10 дней в .gz")
    
    # kg — Knowledge Graph
    p_kg = subparsers.add_parser("kg", help="Управление Knowledge Graph")
    p_kg_sub = p_kg.add_subparsers(dest="kg_action", required=True)
    p_kg_sub.add_parser("stats", help="Статистика KG: entities, relations")
    p_kg_search = p_kg_sub.add_parser("search", help="Поиск entity")
    p_kg_search.add_argument("name", help="Имя entity")
    p_kg_graph = p_kg_sub.add_parser("graph", help="Обход графа от entity")
    p_kg_graph.add_argument("name", help="Имя entity")
    p_kg_graph.add_argument("--depth", type=int, default=2, help="Глубина обхода")
    p_kg_sub.add_parser("stale", help="Stale entities (>30 дней)")
    p_kg_sub.add_parser("cleanup", help="Удалить stale entities (>60 дней)")
    
    args = parser.parse_args()
    
    if args.command == "setup":
        return await cmd_setup(args)
    elif args.command == "update":
        return await cmd_update(args)
    elif args.command == "rollback":
        return await cmd_rollback(args)
    elif args.command == "uninstall":
        return await cmd_uninstall(args)
    elif args.command == "permissions":
        setup_logging()
        return await cmd_permissions(args)
    elif args.command == "stats":
        setup_logging()
        return await cmd_stats(args)
    elif args.command == "enable":
        return await cmd_enable(args)
    elif args.command == "l3":
        return await cmd_l3(args)
    elif args.command == "models":
        return await cmd_models(args)
    elif args.command == "cron":
        return await cmd_cron(args)
    elif args.command == "skill":
        return await cmd_skill(args)
    elif args.command == "doctor":
        setup_logging()
        return await cmd_doctor(args)
    elif args.command == "db":
        return await cmd_db(args)
    elif args.command == "kg":
        return await cmd_kg(args)
    elif args.command == "config":
        return await cmd_config(args)
    else:
        parser.print_help()
        return 0


def main():
    setup_logging()
    sys.exit(asyncio.run(main_async()))


if __name__ == "__main__":
    main()
