"""llm.py unit-тесты — логика роутера без сети.

Раньше llm.py (492 LOC) не имел unit-тестов — все импортирующие тесты мокали
LLM целиком. Тут покрываем: dispatch провайдера, auto-config (cheap←smart),
analyze_request (JSON unwrap + markdown-fence + fallback на bad JSON).
"""
import json
from unittest.mock import AsyncMock, MagicMock

from caesar.config import Config, LLMConfig
from caesar.core.llm import (
    AnthropicProvider,
    LLMResponse,
    LLMRouter,
    OpenAICompatibleProvider,
)


def _router(smart_provider="openai", smart_key="sk-x", cheap_key="", smart_model="gpt-4o"):
    cfg = Config()
    cfg.llm = LLMConfig(
        smart_provider=smart_provider,
        smart_api_key=smart_key,
        smart_model=smart_model,
        cheap_api_key=cheap_key,
    )
    return LLMRouter(cfg)


def test_make_provider_openai():
    r = _router(smart_provider="openai", smart_key="sk-1")
    assert isinstance(r.smart, OpenAICompatibleProvider)


def test_make_provider_anthropic():
    r = _router(smart_provider="anthropic", smart_key="sk-a")
    assert isinstance(r.smart, AnthropicProvider)


def test_make_provider_custom_falls_back_to_openai_compat():
    # z.ai / ollama / custom → OpenAI-compatible endpoint
    r = _router(smart_provider="z.ai", smart_key="k")
    assert isinstance(r.smart, OpenAICompatibleProvider)


def test_auto_config_copies_smart_key_to_cheap():
    r = _router(smart_provider="openai", smart_key="sk-smart", cheap_key="")
    assert r.config.llm.cheap_api_key == "sk-smart"
    assert r.config.llm.cheap_provider == "openai"


async def test_analyze_request_parses_json():
    r = _router()
    r.cheap = MagicMock()
    r.cheap.chat = AsyncMock(return_value=LLMResponse(
        content=json.dumps({
            "is_trivial": True, "trivial_response": "Привет!",
            "needs_memory": False, "memory_queries": [],
            "complexity": "simple", "entities": [],
        }),
        model="x",
    ))
    result = await r.analyze_request("привет")
    assert result["is_trivial"] is True
    assert result["trivial_response"] == "Привет!"


async def test_analyze_request_strips_markdown_fence():
    r = _router()
    payload = json.dumps({"is_trivial": False, "complexity": "medium", "entities": ["nginx"]})
    r.cheap = MagicMock()
    r.cheap.chat = AsyncMock(return_value=LLMResponse(content=f"```json\n{payload}\n```", model="x"))
    result = await r.analyze_request("настрой nginx")
    assert result["complexity"] == "medium"
    assert "nginx" in result["entities"]


async def test_analyze_request_bad_json_defaults_to_simple():
    """Из аудита: malformed JSON тихо даёт complexity=simple — фиксим поведение тестом."""
    r = _router()
    r.cheap = MagicMock()
    r.cheap.chat = AsyncMock(return_value=LLMResponse(content="совсем не json", model="x"))
    result = await r.analyze_request("что-то")
    assert result["complexity"] == "simple"
    assert result["is_trivial"] is False
