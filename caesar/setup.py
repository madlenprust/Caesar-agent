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


def _setup_add_provider(config) -> None:
    """Добавить нового провайдера в список (не затирая старых)."""
    from caesar.config import ProviderEntry, RoleConfig
    name = ask("   Имя (напр. openai, zai, local)", default="").strip()
    if not name:
        print("   ⚠️ Имя не может быть пустым")
        return
    if any(p.name == name for p in config.llm.providers):
        print(f"   ⚠️ Провайдер '{name}' уже существует")
        return
    type_choice = ask_choice(
        "   Тип API:",
        {"1": "OpenAI", "2": "Anthropic", "3": "Custom (OpenAI-совместимый)"},
        default_key="1",
    )
    ptype = {"1": "openai", "2": "anthropic", "3": "custom"}[type_choice]
    api_key = safe_input("   API ключ: ").strip()
    base_url = None
    if ptype == "custom":
        print("   Примеры: https://api.deepseek.com/v1, https://api.groq.com/openai/v1")
        base_url = ask("   Base URL", default="").strip() or None
    print("   Имена моделей (Enter = пропустить)")
    smart_model = ask("   Smart модель", default="").strip()
    cheap_model = ask("   Cheap модель", default=smart_model).strip()
    models = [m for m in [smart_model, cheap_model] if m]
    entry = ProviderEntry(name=name, type=ptype, api_key=api_key,
                           base_url=base_url, models=models)
    config.llm.providers.append(entry)
    # Если первый — назначаем smart+cheap
    if len(config.llm.providers) == 1:
        config.llm.smart_role = RoleConfig(provider=name, model=smart_model or "gpt-4o")
        config.llm.cheap_role = RoleConfig(provider=name, model=cheap_model or smart_model or "gpt-4o-mini")
    print(f"   ✅ Добавлен: {name} ({ptype})")


def _setup_switch_roles(config) -> None:
    """Выбрать smart/cheap модели из списка провайдеров."""
    from caesar.config import RoleConfig
    if not config.llm.providers:
        print("   Нет провайдеров")
        return
    print("\n   Smart (умная):")
    for i, p in enumerate(config.llm.providers, 1):
        print(f"   [{i}] {p.name}")
    idx = ask("   Провайдер (номер)", default="1").strip()
    try:
        sp = config.llm.providers[int(idx) - 1]
    except (ValueError, IndexError):
        print("   ⚠️ Неверный выбор"); return
    smart_model = ask("   Smart модель", default=config.llm.smart_role.model or "").strip()
    config.llm.smart_role = RoleConfig(provider=sp.name, model=smart_model)
    print("\n   Cheap (дешёвая):")
    for i, p in enumerate(config.llm.providers, 1):
        print(f"   [{i}] {p.name}")
    idx = ask("   Провайдер (номер)", default="1").strip()
    try:
        cp = config.llm.providers[int(idx) - 1]
    except (ValueError, IndexError):
        cp = sp
    cheap_model = ask("   Cheap модель", default=config.llm.cheap_role.model or smart_model).strip()
    config.llm.cheap_role = RoleConfig(provider=cp.name, model=cheap_model)
    print(f"   ✅ smart={sp.name}/{smart_model}, cheap={cp.name}/{cheap_model}")


def _setup_remove_provider(config) -> None:
    """Удалить провайдера из списка."""
    print("   Кого удалить?")
    for i, p in enumerate(config.llm.providers, 1):
        print(f"   [{i}] {p.name}")
    idx = ask("   Номер", default="").strip()
    try:
        i = int(idx) - 1
        name = config.llm.providers[i].name
        config.llm.providers.pop(i)
        if config.llm.smart_role.provider == name and config.llm.providers:
            config.llm.smart_role.provider = config.llm.providers[0].name
        if config.llm.cheap_role.provider == name and config.llm.providers:
            config.llm.cheap_role.provider = config.llm.providers[0].name
        print(f"   ✅ Удалён: {name}")
    except (ValueError, IndexError):
        print("   ⚠️ Неверный выбор")


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
    
    # 2. LLM-провайдеры (multi-provider)
    from caesar.config import ProviderEntry, RoleConfig

    # Миграция: если legacy-формат (нет providers) → конвертируем
    if not config.llm.is_multi_provider() and config.llm.smart_api_key:
        config.llm.providers = [ProviderEntry(
            name=config.llm.smart_provider,
            type=config.llm.smart_provider,
            api_key=config.llm.smart_api_key,
            base_url=config.llm.smart_base_url,
            models=[],
        )]
        config.llm.smart_role = RoleConfig(
            provider=config.llm.smart_provider,
            model=config.llm.smart_model,
        )
        config.llm.cheap_role = RoleConfig(
            provider=config.llm.smart_provider,
            model=config.llm.cheap_model or config.llm.smart_model,
        )
        print(f"   (Мигрировано с legacy на multi-provider: 1 провайдер)")

    # Первый запуск (нет ни одного провайдера) → спрашиваем
    if not config.llm.providers:
        print("\n2. LLM-провайдер (первый):")
        _setup_add_provider(config)
    else:
        # Показываем существующих + действия
        print(f"\n2. LLM-провайдеры ({len(config.llm.providers)}):")
        for i, p in enumerate(config.llm.providers, 1):
            key_s = "✅ ключ" if p.api_key else "❌ ключ"
            roles = []
            if p.name == config.llm.smart_role.provider:
                roles.append(f"smart={config.llm.smart_role.model}")
            if p.name == config.llm.cheap_role.provider:
                roles.append(f"cheap={config.llm.cheap_role.model}")
            role_s = f" [{', '.join(roles)}]" if roles else ""
            print(f"   [{i}] {p.name} ({p.type}, {key_s}){role_s}")

        print(f"   [a] Добавить нового")
        print(f"   [s] Сменить smart/cheap модели")
        print(f"   [r] Удалить провайдера")
        action = ask("\n   Действие (Enter = оставить как есть)", default="").strip().lower()

        if action == "a":
            _setup_add_provider(config)
        elif action == "s":
            _setup_switch_roles(config)
        elif action == "r" and len(config.llm.providers) > 1:
            _setup_remove_provider(config)
        elif action == "r":
            print("   ⚠️ Нельзя удалить единственного провайдера")

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
