"""Тест: фильтр _should_save_to_l3 — что попадает в L3, а что нет."""
import logging

import pytest

from caesar.config import Config
from caesar.core.orchestrator import Orchestrator


def make_orch() -> Orchestrator:
    """Собрать Orchestrator без полного __init__ — только то, что нужно фильтру."""
    orch = Orchestrator.__new__(Orchestrator)
    orch.config = Config()
    orch.log = logging.getLogger("test")
    return orch


# (name, user_msg, assistant, tool_calls, expected_save, expected_trigger)
SAVE_CASES = [
    pytest.param(
        "explicit 'запомни'",
        "запомни что мой любимый цвет синий",
        "OK, запомнил: твой любимый цвет синий.",
        0, True, "explicit_request",
        id="explicit-save",
    ),
    pytest.param(
        "code block",
        "как настроить nginx?",
        "Вот конфиг:\n```nginx\nserver {\n  listen 80;\n}\n```\nЭто базовая настройка.",
        1, True, "code_block",
        id="code-block",
    ),
    pytest.param(
        "URLs",
        "где почитать про Python?",
        "Хорошая статья: https://docs.python.org/3/tutorial/ и ещё https://realpython.com",
        0, True, "urls",
        id="urls",
    ),
    pytest.param(
        "long explanation",
        "объясни как работает HTTPS",
        "HTTPS — это протокол безопасной передачи данных. "
        + ("Он использует TLS/SSL для шифрования. " * 50)
        + "Вот так.",
        0, True, "long_explanation",
        id="long-explanation",
    ),
    pytest.param(
        "complex research task",
        "найди новости про Hermes agent",
        "Вот что нашёл: Hermes Agent — опенсорсный AI-агент от Nous Research.",
        4, True, "complex_task",
        id="complex-task",
    ),
    pytest.param(
        "structured steps",
        "как установить Docker?",
        "Установка Docker:\n1. Обнови пакеты: apt update\n2. Установи: apt install docker.io\n"
        "3. Проверь: docker --version\n4. Запусти: systemctl start docker",
        0, True, "structured_steps",
        id="structured-steps",
    ),
    # --- НЕ ДОЛЖНО СОХРАНЯТЬ ---
    pytest.param("trivial 'привет'", "привет", "Привет! Как дела?", 0, False, None, id="trivial-privet"),
    pytest.param("trivial 'ок'", "сделай это", "ок", 0, False, None, id="trivial-ok"),
    pytest.param("short chat about shashlik", "я люблю шашлык", "Здорово, я тоже!", 0, False, None, id="shashlik"),
    pytest.param("trivial thanks", "спасибо", "Пожалуйста!", 0, False, None, id="thanks"),
    pytest.param("short normal chat", "как дела?", "Да нормально, работаю. А у тебя?", 0, False, None, id="short-chat"),
    # --- BORDERLINE (сохраняем из-за триггера) ---
    pytest.param("short but with URL", "дай ссылку", "Вот: https://example.com", 0, True, "urls", id="short-url"),
    pytest.param("explicit save short", "сохрани это", "ок", 0, True, "explicit_request", id="explicit-short"),
]


@pytest.mark.parametrize(
    "name,user_msg,assistant,tool_calls,expected_save,expected_trigger",
    SAVE_CASES,
)
def test_should_save_to_l3(name, user_msg, assistant, tool_calls, expected_save, expected_trigger):
    orch = make_orch()
    triggers = orch._l3_save_triggers(user_msg, assistant, tool_calls)
    should_save = orch._should_save_to_l3(user_msg, assistant, tool_calls)
    assert should_save == expected_save, f"{name}: should_save={should_save} expected={expected_save}"
    if expected_trigger is not None:
        assert expected_trigger in triggers, f"{name}: trigger {expected_trigger!r} missing in {triggers}"


# (name, user_msg, assistant, tool_calls, web_tools, expected_save, expected_trigger)
WEB_TOOL_CASES = [
    pytest.param(
        "web_search ответ (НЕ explicit)",
        "найди новости про Hermes",
        "Вот что нашёл: Hermes Agent — опенсорсный AI-агент от Nous Research. "
        + ("Очень подробно. " * 100),
        4, {"web_search"}, False, None,
        id="web-search-no-explicit",
    ),
    pytest.param(
        "web_search + explicit 'запомни' (СОХРАНЯЕМ)",
        "запомни найди новости про Hermes",
        "Вот что нашёл: Hermes Agent — опенсорсный AI-агент.",
        4, {"web_search"}, True, "explicit_request",
        id="web-search-explicit",
    ),
    pytest.param(
        "hn_search без explicit (НЕ сохраняем)",
        "что нового на HN?",
        "Top stories: 1. AI breakthrough 2. New framework",
        3, {"hn_search"}, False, None,
        id="hn-search-no-explicit",
    ),
]


@pytest.mark.parametrize(
    "name,user_msg,assistant,tool_calls,web_tools,expected_save,expected_trigger",
    WEB_TOOL_CASES,
)
def test_l3_filter_with_web_tools(name, user_msg, assistant, tool_calls, web_tools, expected_save, expected_trigger):
    orch = make_orch()
    triggers = orch._l3_save_triggers(
        user_msg, assistant, tool_calls,
        used_tool_names=web_tools, used_web_tools=web_tools,
    )
    should_save = len(triggers) > 0
    assert should_save == expected_save, f"{name}: should_save={should_save} expected={expected_save}"
    if expected_trigger is not None:
        assert expected_trigger in triggers, f"{name}: trigger {expected_trigger!r} missing in {triggers}"
