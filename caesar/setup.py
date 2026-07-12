"""Setup wizard.

См. roadmap раздел 14.4.

Запускается после установки: `agent setup`.
Спрашивает LLM-провайдер, API ключ, TG-бот, режим.
Валидирует сразу.
"""

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

import httpx
import yaml

from caesar.config import Config, CONFIG_PATH
from caesar.logging_setup import setup_logging, get_logger


PROVIDERS = {
    "1": {
        "name": "openai",
        "display": "OpenAI",
        "smart_model": "gpt-4o",
        "cheap_model": "gpt-4o-mini",
        "base_url": "https://api.openai.com/v1",
    },
    "2": {
        "name": "anthropic",
        "display": "Anthropic",
        "smart_model": "claude-3-5-sonnet-20241022",
        "cheap_model": "claude-3-5-haiku-20241022",
        "base_url": "",
    },
    "3": {
        "name": "zai",
        "display": "Z.ai (GLM)",
        "smart_model": "glm-4.6",
        "cheap_model": "glm-4-flash",
        "base_url": "https://api.z.ai/api/paas/v4",
    },
    "4": {
        "name": "ollama",
        "display": "Ollama (локально)",
        "smart_model": "llama3.1",
        "cheap_model": "llama3.1",
        "base_url": "http://localhost:11434/v1",
    },
    "5": {
        "name": "custom",
        "display": "Custom — свой OpenAI-совместимый endpoint",
        "smart_model": "",   # спросим
        "cheap_model": "",   # спросим
        "base_url": "",      # спросим
        "custom": True,
    },
}


async def validate_llm_key(
    provider: str,
    api_key: str,
    base_url: str,
    model: str | None = None,
) -> tuple[bool, str]:
    """Проверить ключ LLM тестовым запросом."""
    if provider == "ollama":
        # Проверяем что ollama отвечает
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{base_url.rstrip('/v1')}/api/tags")
                if resp.status_code == 200:
                    return True, "Ollama отвечает"
                return False, f"Ollama вернул {resp.status_code}"
        except Exception as e:
            return False, f"Не могу подключиться к Ollama: {e}"
    
    if not api_key:
        return False, "Пустой ключ"
    
    # Тестовый запрос
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            if provider == "anthropic":
                resp = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": api_key,
                        "anthropic-version": "2023-06-01",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model or "claude-3-5-haiku-20241022",
                        "max_tokens": 10,
                        "messages": [{"role": "user", "content": "Hi"}],
                    },
                )
            else:
                # OpenAI-совместимый (включая custom endpoints)
                test_model = model or (
                    "gpt-4o-mini" if provider == "openai" else "glm-4-flash"
                )
                resp = await client.post(
                    f"{base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": test_model,
                        "max_tokens": 10,
                        "messages": [{"role": "user", "content": "Hi"}],
                    },
                )
            
            if resp.status_code == 200:
                return True, "Ключ валидный"
            return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
    except Exception as e:
        return False, f"Ошибка: {e}"


async def validate_tg_token(token: str) -> tuple[bool, str, str]:
    """Проверить TG-токен. Возвращает (ok, message, bot_username)."""
    if not token:
        return False, "Пустой токен", ""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"https://api.telegram.org/bot{token}/getMe")
            data = resp.json()
            if data.get("ok"):
                username = data["result"]["username"]
                return True, f"Бот @{username}", username
            return False, data.get("description", "Ошибка"), ""
    except Exception as e:
        return False, f"Ошибка: {e}", ""


def ask(prompt: str, default: str = "") -> str:
    """Спросить с default. Корректно обрабатывает Ctrl+C и Ctrl+D."""
    try:
        if default:
            s = input(f"{prompt} [{default}]: ").strip()
            return s or default
        return input(f"{prompt}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n\n⚠️ Прервано пользователем. Выход.")
        raise SystemExit(130)


def safe_input(prompt: str) -> str:
    """input() с обработкой Ctrl+C / Ctrl+D."""
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print("\n\n⚠️ Прервано пользователем. Выход.")
        raise SystemExit(130)


def ask_choice(prompt: str, options: dict, default_key: str = "1") -> str:
    """Спросить с выбором. Корректно обрабатывает Ctrl+C и Ctrl+D."""
    print(prompt)
    for k, v in options.items():
        marker = " ← default" if k == default_key else ""
        print(f"   {k}) {v}{marker}")
    print("   q) Выйти из setup")
    while True:
        try:
            choice = safe_input(f"   Выбор [{default_key}]: ").strip() or default_key
        except (EOFError, KeyboardInterrupt):
            print("\n\n⚠️ Прервано пользователем. Выход.")
            raise SystemExit(130)
        
        if choice.lower() in ("q", "quit", "exit", "выход"):
            print("Выход из setup. Настройки не сохранены.")
            raise SystemExit(0)
        if choice in options:
            return choice
        print(f"   Неверный выбор. Доступно: {', '.join(options.keys())}, q (выход)")


async def run_setup(non_interactive: bool = False, config_path: Path = CONFIG_PATH) -> int:
    """Запустить setup wizard.
    
    Показывает текущие значения как defaults — Enter сохраняет текущее.
    """
    setup_logging()
    log = get_logger("setup")
    
    print()
    print("🤖 Caesar setup")
    print("---------------")
    print("Нажми Enter чтобы сохранить текущее значение, или введи новое.")
    print("В любой момент: q — выйти, Ctrl+C — прервать.")
    print()
    
    config = Config.load(config_path)
    
    # Определяем текущий default для режима
    current_mode = {"auto": "1", "autonomous": "2", "full": "3"}.get(config.mode, "1")
    
    # 1. Режим работы
    mode_choice = ask_choice(
        "1. Режим работы:",
        {
            "1": "Обычный (песочница, безопасно)",
            "2": "Автономный (права твоего юзера)",
            "3": "Полный (sudo, может сломать систему)",
        },
        default_key=current_mode,
    )
    config.mode = {"1": "auto", "2": "autonomous", "3": "full"}[mode_choice]
    print(f"   ✅ Режим: {config.mode}")
    
    # 2. LLM-провайдер — определяем текущий
    current_provider_key = "1"  # default
    for k, v in PROVIDERS.items():
        if v["name"] == config.llm.smart_provider:
            current_provider_key = k
            break
    
    # Показываем какой сейчас выбран
    if config.llm.smart_api_key:
        print(f"   (Текущий: {config.llm.smart_provider} / {config.llm.smart_model}, ключ установлен)")
    else:
        print(f"   (Текущий: {config.llm.smart_provider}, ключ НЕ установлен)")
    
    llm_choice = ask_choice(
        "\n2. LLM-провайдер:",
        {k: v["display"] for k, v in PROVIDERS.items()},
        default_key=current_provider_key,
    )
    provider = PROVIDERS[llm_choice]
    
    # Для custom — спрашиваем base_url (с текущим как default)
    if provider.get("custom"):
        print(f"\n   🔗 Custom endpoint (OpenAI-совместимый API)")
        print(f"   Примеры: https://api.deepseek.com/v1, https://api.groq.com/openai/v1,")
        print(f"            https://api.together.xyz/v1, https://openrouter.ai/api/v1,")
        print(f"            http://localhost:8000/v1 (vLLM/LM Studio)\n")
        current_url = config.llm.smart_base_url or ""
        base_url = ask("   Base URL", default=current_url)
        provider["base_url"] = base_url
        provider["name"] = "openai"
    
    if provider["name"] != "ollama":
        # Показываем текущий ключ (маскированный)
        if config.llm.smart_api_key:
            masked = config.llm.smart_api_key[:8] + "..." + config.llm.smart_api_key[-4:]
            print(f"\n   Текущий ключ: {masked}")
            api_key = safe_input(f"   Новый API ключ (Enter = оставить текущий): ").strip()
            if not api_key:
                api_key = config.llm.smart_api_key  # оставляем текущий
                print(f"   ✅ Оставлен текущий ключ")
        else:
            api_key = safe_input(f"   API ключ для {provider['display']}: ").strip()
        
        if api_key:
            print("   Проверяю ключ...")
            ok, msg = await validate_llm_key(
                provider["name"], api_key, provider["base_url"],
                model=config.llm.smart_model if provider.get("smart_model") is None else provider.get("smart_model"),
            )
            if ok:
                print(f"   ✅ {msg}")
            else:
                print(f"   ⚠️ {msg}")
                print("   (можно поправить позже через `caesar setup`)")
        
        config.llm.smart_api_key = api_key
        config.llm.cheap_api_key = api_key
    
    config.llm.smart_provider = provider["name"]
    config.llm.cheap_provider = provider["name"]
    
    # Для custom — спрашиваем имена моделей (с текущими как default)
    if provider.get("custom") or not provider.get("smart_model"):
        print(f"\n   📝 Имена моделей")
        print(f"   Примеры: deepseek-chat, llama-3.3-70b-versatile, gpt-4o, и т.д.\n")
        smart_model = ask("   Умная модель", default=config.llm.smart_model or "")
        config.llm.smart_model = smart_model
        
        cheap_model = ask("   Дешёвая модель", default=config.llm.cheap_model or smart_model)
        config.llm.cheap_model = cheap_model
    else:
        smart_model = ask("   Умная модель", default=provider["smart_model"])
        config.llm.smart_model = smart_model
        
        cheap_model = ask("   Дешёвая модель", default=provider["cheap_model"])
        config.llm.cheap_model = cheap_model
    
    if provider.get("base_url"):
        config.llm.smart_base_url = provider["base_url"]
        config.llm.cheap_base_url = provider["base_url"]
    
    # 3. Telegram-бот — с текущим значением
    print()
    if config.telegram.bot_token:
        print("3. Telegram-бот: уже настроен.")
        tg_choice = safe_input("   Перенастроить? [y/N]: ").strip().lower()
        if tg_choice in ("y", "yes", "д", "да"):
            tg_token = safe_input("   Новый Bot token (от @BotFather): ").strip()
            if tg_token:
                print("   Проверяю токен...")
                ok, msg, username = await validate_tg_token(tg_token)
                if ok:
                    print(f"   ✅ {msg}")
                    config.telegram.bot_token = tg_token
                else:
                    print(f"   ⚠️ {msg}")
                    config.telegram.bot_token = tg_token
    else:
        tg_choice = safe_input("\n3. Telegram-бот: хочешь подключить? [Y/n]: ").strip().lower()
        if tg_choice in ("", "y", "yes", "д", "да"):
            tg_token = safe_input("   Bot token (от @BotFather): ").strip()
            if tg_token:
                print("   Проверяю токен...")
                ok, msg, username = await validate_tg_token(tg_token)
                if ok:
                    print(f"   ✅ {msg}")
                    config.telegram.bot_token = tg_token
                else:
                    print(f"   ⚠️ {msg}")
                    config.telegram.bot_token = tg_token
    
    # 4. Сохраняем — пишем напрямую в файл, надёжно
    config.save(config_path)
    
    # Дополнительно — перечитаем и проверим что ключи сохранились
    verify = Config.load(config_path)
    if verify.llm.smart_api_key:
        print(f"\n✅ Конфигурация сохранена в {config_path}")
        print(f"   smart_api_key: {'✅ установлен' if verify.llm.smart_api_key else '❌ пустой'}")
        print(f"   smart_model: {verify.llm.smart_model}")
        if verify.telegram.bot_token:
            print(f"   telegram: ✅ настроен")
    else:
        print(f"\n⚠️ Конфигурация сохранена, но smart_api_key пустой!")
        print(f"   Проверь {config_path} вручную.")
    
    # 5. Инструкция что дальше
    print()
    print("Что дальше:")
    print()
    print("  # Перезапусти daemon чтобы он подхватил новый конфиг:")
    print("  systemctl --user restart caesar-daemon")
    print()
    print("  # Проверь статус:")
    print("  caesar --status")
    print()
    print("  # Отправь сообщение:")
    print("  caesar 'привет'")
    print()
    print("  # Или запусти REPL:")
    print("  caesar")
    print()
    print("  # Смотреть логи:")
    print("  journalctl --user -u caesar-daemon -f")
    print()
    
    return 0


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Agent setup wizard")
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    args = parser.parse_args()
    
    sys.exit(asyncio.run(run_setup(non_interactive=args.non_interactive, config_path=args.config)))


if __name__ == "__main__":
    main()
