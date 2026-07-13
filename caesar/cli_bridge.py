"""CLI bridge — выбор CLI-клиента или management-команды.

Когда пользователь запускает `caesar ...`, этот модуль решает:
- `caesar setup` → setup wizard
- `caesar update` → update
- `caesar status` → статус daemon
- `caesar "сообщение"` → CLI-клиент (one-shot)
- `caesar` (без аргументов) → CLI-клиент (REPL)
- `caesar permissions ...` → management
- `caesar stats ...` → management
- `caesar uninstall` → uninstall
"""

import asyncio
import os
import sys
from pathlib import Path

# Добавляем путь к caesar
SCRIPT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

from caesar.config import SOCKET_PATH


# Команды management (не требуют daemon)
MGMT_COMMANDS = {
    "setup", "update", "rollback", "uninstall", "pair",
    "permissions", "stats", "self-scan", "enable", "l3", "models", "cron",
    "skill", "doctor", "db", "kg", "config",
}


async def send_to_daemon(request: dict) -> dict | None:
    """Отправить запрос в daemon."""
    if not SOCKET_PATH.exists():
        return None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(SOCKET_PATH)),
            timeout=2,
        )
    except Exception:
        return None
    
    try:
        import json
        line = json.dumps(request, ensure_ascii=False) + "\n"
        writer.write(line.encode("utf-8"))
        await writer.drain()
        
        response_line = await reader.readline()
        if not response_line:
            return None
        return json.loads(response_line.decode("utf-8"))
    except Exception:
        return None
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


def is_daemon_running() -> bool:
    """Быстрая проверка что daemon запущен."""
    return SOCKET_PATH.exists()


async def cli_main(args: list[str]) -> int:
    """CLI-клиент (thin client)."""
    import json
    
    if not is_daemon_running():
        print("❌ Caesar daemon не запущен.", file=sys.stderr)
        print("   Запусти: systemctl start caesar-daemon", file=sys.stderr)
        print("   Или dev: python -m caesar.daemon &", file=sys.stderr)
        return 1
    
    user_id = f"cli-{os.getuid()}"
    
    # One-shot mode (есть сообщение)
    if args:
        message = " ".join(args)
        return await one_shot(message, user_id)
    
    # REPL mode
    return await repl(user_id)


async def one_shot(message: str, user_id: str) -> int:
    """One-shot: отправить сообщение, ждать ответ."""
    import json
    
    # Спец-команды
    if message in ("--status", "status"):
        response = await send_to_daemon({"action": "get_status"})
        if response:
            print(json.dumps(response, indent=2, ensure_ascii=False))
            return 0
        return 1
    
    # Отправляем сообщение
    request = {
        "action": "send_message",
        "user_id": user_id,
        "message": message,
        "channel_name": "main",
    }
    
    response = await send_to_daemon(request)
    if not response:
        print("❌ Нет ответа от daemon", file=sys.stderr)
        return 1
    
    if "error" in response:
        print(f"❌ {response.get('message', response['error'])}", file=sys.stderr)
        return 1
    
    session_id = response.get("session_id")
    if not session_id:
        print("❌ Daemon не вернул session_id", file=sys.stderr)
        return 1
    
    # Опрашиваем events
    return await poll_events(session_id)


async def poll_events(session_id: str, timeout: int = 300) -> int:
    """Опрашивать events у daemon и показывать прогресс.
    
    timeout: 300 сек (5 минут) — для долгих задач.
    """
    import json
    
    deadline = asyncio.get_event_loop().time() + timeout
    
    while asyncio.get_event_loop().time() < deadline:
        request = {
            "action": "get_events",
            "session_id": session_id,
            "timeout": 30,
        }
        
        response = await send_to_daemon(request)
        if not response:
            print("❌ Daemon не отвечает", file=sys.stderr)
            return 1
        
        if "error" in response:
            print(f"❌ {response.get('message', response['error'])}", file=sys.stderr)
            return 1
        
        events = response.get("events", [])
        
        for event in events:
            event_type = event["type"]
            data = event.get("data", {})
            
            if event_type == "progress_start":
                print("🧠", end="", flush=True)
            elif event_type == "progress_update":
                icon = data.get("icon", "")
                print(f" {icon}", end="", flush=True)
            elif event_type == "answer_ready":
                content = data.get("content", "")
                print()
                print("─── готово ───")
                print(content)
                return 0
            elif event_type == "error_occurred":
                print()
                print("─── не получилось ───")
                print(data.get("message", "Неизвестная ошибка"))
                return 1
            elif event_type == "info_notification":
                print(f"ℹ️ {data.get('message', '')}")
            elif event_type == "warning_notification":
                print(f"⚠️ {data.get('message', '')}")
            elif event_type == "question_asked":
                print()
                question = data.get("question", "")
                options = data.get("options", [])
                print(f"❓ {question}")
                if options:
                    for i, opt in enumerate(options, 1):
                        print(f"   {i}. {opt.get('label', '')}")
                    answer = input("> ")
                    # TODO: отправить ответ обратно
                return 0
    
    print("\n⏱ Превышен таймаут ожидания", file=sys.stderr)
    return 1


async def repl(user_id: str) -> int:
    """REPL режим."""
    print()
    print(" ██████╗ █████╗ ███████╗███████╗ █████╗ ██████╗")
    print(" ██╔════╝██╔══██╗██╔════╝██╔════╝██╔══██╗██╔══██╗")
    print(" ██║     ███████║█████╗  ███████╗███████║██████╔╝")
    print(" ██║     ██╔══██║██╔══╝  ╚════██║██╔══██║██╔══██╗")
    print(" ╚██████╗██║  ██║███████╗███████║██║  ██║██║  ██║")
    print("  ╚═════╝╚═╝  ╚═╝╚══════╝╚══════╝╚═╝  ╚═╝╚═╝  ╚═╝")
    print()
    print(" /help — помощь, /exit — выход")
    print()
    
    session_id = None
    
    while True:
        try:
            message = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        
        if not message:
            continue
        
        if message == "/exit":
            break
        elif message == "/help":
            print("Команды:")
            print("  /exit — выйти")
            print("  /status — статус daemon")
            print("  /tasks — активные задачи")
            print("  /clear — очистить контекст диалога")
            print("  Любой другой текст — сообщение агенту")
            continue
        elif message == "/status":
            import json
            response = await send_to_daemon({"action": "get_status"})
            if response:
                print(json.dumps(response, indent=2, ensure_ascii=False))
            continue
        elif message == "/tasks":
            import json
            response = await send_to_daemon({"action": "list_tasks"})
            if response:
                print(json.dumps(response, indent=2, ensure_ascii=False))
            continue
        elif message == "/clear":
            # Очищаем контекст — новый session_id
            session_id = None
            print("🧹 Контекст очищен. Начинаю новую сессию.")
            continue
        
        # Отправляем сообщение
        import json
        request = {
            "action": "send_message",
            "user_id": user_id,
            "message": message,
            "channel_name": "main",
        }
        
        if session_id:
            request["session_id"] = session_id
        
        response = await send_to_daemon(request)
        if not response:
            print("❌ Daemon не отвечает")
            continue
        
        if "error" in response:
            print(f"❌ {response.get('message', response['error'])}")
            continue
        
        session_id = response.get("session_id", session_id)
        await poll_events(session_id)
        print()
    
    return 0


def main():
    """Точка входа."""
    args = sys.argv[1:]
    
    # --help и -h — показываем справку всегда (даже без daemon)
    if args and args[0] in ("--help", "-h"):
        print_help()
        return
    
    # --version
    if args and args[0] in ("--version", "-V"):
        from caesar import __version__
        print(f"Caesar v{__version__}")
        return
    
    # Спец-команды management (не требуют daemon)
    if args and args[0] in MGMT_COMMANDS:
        from caesar.management import main as mgmt_main
        mgmt_main()
        return
    
    # --status — требует daemon
    if args and args[0] in ("--status",):
        asyncio.run(one_shot("--status", f"cli-{os.getuid()}"))
        return
    
    # CLI (one-shot с аргументами, или REPL без аргументов) — требует daemon
    sys.exit(asyncio.run(cli_main(args)))


def print_help() -> None:
    """Показать справку."""
    print("""
Caesar — автономный AI-агент для Ubuntu

Использование:
  caesar                          REPL режим (интерактивный)
  caesar "сообщение"              One-shot: отправить сообщение
  caesar --status                 Статус daemon
  caesar --version                Версия
  caesar --help                   Эта справка

Management команды (не требуют запущенного daemon):
  caesar setup                    Setup wizard (LLM ключ, TG-бот, режим)
  caesar update                   Обновить через git pull
  caesar rollback                 Откатиться к предыдущей версии
  caesar uninstall                Удалить Caesar
  caesar pair                     Привязать бота к твоему Telegram (одноразовый код)
  caesar permissions list         Список разрешений
  caesar permissions revoke --pattern "..."   Отозвать разрешение
  caesar permissions reset        Сбросить все разрешения
  caesar stats                    Статистика по токенам
  caesar stats --task <id>        Статистика по задаче
  caesar enable stt               Включить распознавание голосовых (ставит faster-whisper)
  caesar enable voice             Alias для 'stt'
  caesar enable l3                Включить векторную память (ставит sentence-transformers)
  caesar enable memory            Alias для 'l3'
  caesar models                   Настроить smart и cheap LLM модели (интерактивно)
  caesar models list              Показать текущие и доступные модели
  caesar cron list                Список cron задач
  caesar cron add "расписание" "задача"  Добавить cron задачу
  caesar cron remove <id>         Удалить cron задачу
  caesar l3 status                Статус L3: чанки, модель, работает ли
  caesar l3 search "запрос"       Ручной поиск по L3 (для дебага)
  caesar l3 show                  Показать содержимое чанков (дебаг кодировки)
  caesar l3 fix-encoding          Восстановить кракозябры в чанках
  caesar l3 reindex               Переиндексировать все чанки (новый chunk_size)
  caesar l3 clear                 Удалить ВСЕ чанки L3 (осторожно!)

Управление daemon (systemd user):
  systemctl --user start caesar-daemon
  systemctl --user stop caesar-daemon
  systemctl --user restart caesar-daemon
  systemctl --user status caesar-daemon
  journalctl --user -u caesar-daemon -f       # смотреть логи

Документация: https://github.com/madlenprust/caesar#readme
""")


if __name__ == "__main__":
    main()
