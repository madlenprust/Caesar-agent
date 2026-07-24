"""LLM-роутер.

См. roadmap раздел 9.

Дешёвая LLM анализирует запрос → если скилл найден, код выполняет →
иначе умная LLM отвечает + самопроверка.

Поддерживаемые провайдеры (через OpenAI-совместимый API):
- openai (gpt-4o, gpt-4o-mini, ...)
- anthropic (claude-3-5-sonnet, claude-3-haiku, ...) — через их Messages API
- zai (glm-4.6, glm-4-flash, ...) — через их OpenAI-совместимый endpoint
- ollama (llama3, qwen, ...) — локально, OpenAI-совместимый
- любой OpenAI-совместимый endpoint через base_url

Используем httpx напрямую, без SDK — легче и универсальнее.
"""

import asyncio
import json
import random
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from caesar.config import Config, LLMConfig
from caesar.logging_setup import get_logger


# Retry константы
MAX_RETRIES = 3


@dataclass
class LLMErrorClass:
    """Семантическая классификация HTTP-ошибки провайдера.

    Решение о ретрае принимается по смыслу ошибки, а не только по статус-коду:
    разные провайдеры шлют разный код для одной семантики (429 = rate limit у
    OpenAI, 529 = overloaded у Anthropic), а внутри одного кода бывают разные
    смыслы (429 rate-limit ретраим, 429 quota-exceeded — нет, не восстановится).
    """
    category: str  # rate_limit | overloaded | server_error | quota_exceeded | auth | invalid_request | unknown
    retry: bool
    backoff_mult: float = 1.0  # множитель базевого backoff (2 ** attempt)


def classify_http_error(status_code: int, body: str) -> LLMErrorClass:
    """Классифицировать ошибку по СМЫСЛУ: статус-код + тело ответа."""
    body_lower = (body or "").lower()

    # Auth — невосстановимо при текущем ключе, ретраить бессмысленно
    if status_code in (401, 403):
        return LLMErrorClass("auth", retry=False)

    # Quota / billing — не снимется за секунды
    if "quota" in body_lower or "billing" in body_lower or "insufficient" in body_lower:
        return LLMErrorClass("quota_exceeded", retry=False)

    # Overloaded — Anthropic 529, или тело говорит об overload
    if status_code == 529 or "overloaded" in body_lower or "overload" in body_lower:
        return LLMErrorClass("overloaded", retry=True, backoff_mult=4.0)

    # Rate limit — снимется, но нужен длинный backoff
    if status_code == 429:
        return LLMErrorClass("rate_limit", retry=True, backoff_mult=8.0)

    # Transient server errors
    if status_code in (502, 503, 504):
        return LLMErrorClass("server_error", retry=True, backoff_mult=1.0)

    # Прочий 4xx — невосстановимый bad request
    if 400 <= status_code < 500:
        return LLMErrorClass("invalid_request", retry=False)

    # Неизвестный 5xx — ретраим на всякий
    if 500 <= status_code < 600:
        return LLMErrorClass("server_error", retry=True, backoff_mult=1.0)

    return LLMErrorClass("unknown", retry=False)


@dataclass
class LLMMessage:
    role: str  # "system" | "user" | "assistant" | "tool"
    content: str
    tool_call_id: str | None = None
    name: str | None = None  # для role=tool


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class LLMResponse:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"  # stop | tool_calls | length
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    elapsed_ms: int = 0
    model: str = ""
    provider: str = ""


class LLMProvider:
    """Базовый класс провайдера."""

    provider_name: str = "base"
    # Минимальный интервал между последовательными запросами к одному
    # провайдеру (provider pacing) — софт-рейт-лимит: бережём лимиты API,
    # понижаем шанс 429. Мягкий: гонки допустимы, точного guarantee нет.
    MIN_REQUEST_INTERVAL: float = 0.5

    def __init__(self, config: LLMConfig, role: str):
        """role = 'smart' | 'cheap'"""
        self.role = role
        if role == "smart":
            self.model = config.smart_model
            self.api_key = config.smart_api_key
            self.base_url = config.smart_base_url or "https://api.openai.com/v1"
            self.provider_name = config.smart_provider
        else:
            self.model = config.cheap_model
            self.api_key = config.cheap_api_key
            self.base_url = config.cheap_base_url or "https://api.openai.com/v1"
            self.provider_name = config.cheap_provider
        self.log = get_logger(f"llm.{role}")
        # Метка прошлого запроса — для provider pacing
        self._last_request_time: float = 0.0

    async def _pace(self) -> None:
        """Подождать MIN_REQUEST_INTERVAL с прошлого запроса (provider pacing)."""
        now = time.time()
        if self._last_request_time:
            remaining = self.MIN_REQUEST_INTERVAL - (now - self._last_request_time)
            if remaining > 0:
                await asyncio.sleep(remaining)
        self._last_request_time = time.time()
    
    async def chat(
        self,
        messages: list[LLMMessage],
        tools: list[dict] | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        raise NotImplementedError


class OpenAICompatibleProvider(LLMProvider):
    """OpenAI-совместимый провайдер (OpenAI, Z.ai, Ollama, vLLM, LM Studio, ...)."""
    
    # Популярные модели — для сортировки списка (от популярных к непопулярным)
    POPULAR_MODELS_ORDER = [
        # OpenAI
        "gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-4", "gpt-3.5-turbo",
        # Anthropic (через совместимый API)
        "claude-3-5-sonnet", "claude-3-5-haiku", "claude-3-opus", "claude-3-sonnet", "claude-3-haiku",
        # Z.ai / GLM
        "glm-4.6", "glm-4-plus", "glm-4-flash", "glm-4", "glm-4-air",
        # Google
        "gemini-2.0-flash", "gemini-1.5-pro", "gemini-1.5-flash",
        # Meta / Open source
        "llama-3.1-405b", "llama-3.1-70b", "llama-3.1-8b",
        "qwen2.5-72b", "qwen2.5-7b",
        # Mistral
        "mistral-large", "mistral-small",
    ]
    
    async def list_models(self) -> list[str]:
        """Получить список доступных моделей через GET /v1/models.
        
        Возвращает список ID моделей, отсортированный от популярных к непопулярным.
        Если endpoint недоступен — возвращает пустой список.
        """
        url = f"{self.base_url}/models"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(url, headers=headers)
                if resp.status_code != 200:
                    self.log.warning(f"list_models HTTP {resp.status_code}: {resp.text[:200]}")
                    return []
                data = resp.json()
                models = []
                for item in data.get("data", []):
                    model_id = item.get("id", "")
                    if model_id:
                        models.append(model_id)
                
                # Сортируем: популярные вперёд, остальные по алфавиту
                def sort_key(m):
                    m_lower = m.lower()
                    for i, popular in enumerate(self.POPULAR_MODELS_ORDER):
                        if popular in m_lower:
                            return (0, i, m)  # популярные: по индексу популярности
                    return (1, 0, m)  # остальные: по алфавиту
                
                models.sort(key=sort_key)
                return models
        except Exception as e:
            self.log.warning(f"list_models failed: {type(e).__name__}: {e}")
            return []
    
    async def chat(
        self,
        messages: list[LLMMessage],
        tools: list[dict] | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        url = f"{self.base_url}/chat/completions"
        
        payload: dict = {
            "model": self.model,
            "messages": [
                {
                    "role": m.role,
                    "content": m.content,
                    **({"name": m.name} if m.name else {}),
                    **({"tool_call_id": m.tool_call_id} if m.tool_call_id else {}),
                }
                for m in messages
            ],
            "temperature": temperature,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        await self._pace()  # provider pacing — софт-рейт-лимит перед запросом
        start = time.time()
        data = None
        
        for attempt in range(MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=120) as client:
                    resp = await client.post(url, json=payload, headers=headers)
                elapsed_ms = int((time.time() - start) * 1000)

                if resp.status_code == 200:
                    data = resp.json()
                    break

                # Классифицируем ошибку по СМЫСЛУ (статус + тело), не только по коду
                err = classify_http_error(resp.status_code, resp.text[:500])
                if err.retry and attempt < MAX_RETRIES:
                    wait = (2 ** attempt) * err.backoff_mult + random.uniform(0, 2)
                    self.log.warning(
                        f"LLM {resp.status_code} [{err.category}] "
                        f"(attempt {attempt+1}/{MAX_RETRIES}), retry in {wait:.1f}s"
                    )
                    await asyncio.sleep(wait)
                    continue

                error_text = resp.text[:500]
                self.log.error(f"LLM error {resp.status_code} [{err.category}]: {error_text}")
                raise RuntimeError(
                    f"LLM API error {resp.status_code} [{err.category}]: {error_text}"
                )

            except (httpx.RemoteProtocolError, httpx.ConnectError, httpx.ReadTimeout,
                    httpx.ConnectTimeout, httpx.NetworkError, httpx.TimeoutException) as e:
                # Транспортная ошибка (Server disconnected, ConnectTimeout, …) —
                # transient, ретраим с backoff. Иначе один disconnect = сразу «Ошибка LLM»
                # и агент не доделывает задачу.
                if attempt < MAX_RETRIES:
                    wait = (2 ** attempt) + random.uniform(0, 1)
                    self.log.warning(
                        f"LLM transport error (attempt {attempt+1}/{MAX_RETRIES}): "
                        f"{type(e).__name__}, retry in {wait:.1f}s"
                    )
                    await asyncio.sleep(wait)
                    continue
                raise RuntimeError(
                    f"LLM transport error after {MAX_RETRIES} retries: {type(e).__name__}"
                )
        
        choice = data["choices"][0]
        message = choice["message"]
        content = message.get("content") or ""
        tool_calls_data = message.get("tool_calls") or []
        
        tool_calls: list[ToolCall] = []
        for tc in tool_calls_data:
            try:
                args = json.loads(tc["function"]["arguments"])
            except (json.JSONDecodeError, KeyError):
                args = {}
            tool_calls.append(ToolCall(
                id=tc.get("id", ""),
                name=tc["function"]["name"],
                arguments=args,
            ))
        
        usage = data.get("usage", {})
        
        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=choice.get("finish_reason", "stop"),
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            total_tokens=usage.get("total_tokens", 0),
            elapsed_ms=elapsed_ms,
            model=data.get("model", self.model),
            provider=self.provider_name,
        )


class AnthropicProvider(LLMProvider):
    """Anthropic Claude через Messages API."""
    
    async def chat(
        self,
        messages: list[LLMMessage],
        tools: list[dict] | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        url = f"{self.base_url}/v1/messages" if self.base_url else "https://api.anthropic.com/v1/messages"
        
        # Anthropic требует system отдельно
        system_msg = ""
        conv_messages = []
        for m in messages:
            if m.role == "system":
                system_msg += m.content + "\n"
            else:
                conv_messages.append({"role": m.role, "content": m.content})
        
        payload: dict = {
            "model": self.model,
            "max_tokens": max_tokens or 4096,
            "messages": conv_messages,
            "temperature": temperature,
        }
        if system_msg:
            payload["system"] = system_msg.strip()
        if tools:
            # Конвертация OpenAI tools → Anthropic tools
            anthropic_tools = []
            for t in tools:
                if t.get("type") == "function":
                    fn = t["function"]
                    anthropic_tools.append({
                        "name": fn["name"],
                        "description": fn.get("description", ""),
                        "input_schema": fn.get("parameters", {"type": "object"}),
                    })
            payload["tools"] = anthropic_tools
        
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }

        await self._pace()  # provider pacing — софт-рейт-лимит перед запросом
        start = time.time()
        data = None
        
        for attempt in range(MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=120) as client:
                    resp = await client.post(url, json=payload, headers=headers)
                elapsed_ms = int((time.time() - start) * 1000)

                if resp.status_code == 200:
                    data = resp.json()
                    break

                err = classify_http_error(resp.status_code, resp.text[:500])
                if err.retry and attempt < MAX_RETRIES:
                    wait = (2 ** attempt) * err.backoff_mult + random.uniform(0, 2)
                    self.log.warning(
                        f"Anthropic {resp.status_code} [{err.category}] "
                        f"(attempt {attempt+1}/{MAX_RETRIES}), retry in {wait:.1f}s"
                    )
                    await asyncio.sleep(wait)
                    continue

                error_text = resp.text[:500]
                self.log.error(f"Anthropic error {resp.status_code} [{err.category}]: {error_text}")
                raise RuntimeError(
                    f"Anthropic API error {resp.status_code} [{err.category}]: {error_text}"
                )

            except (httpx.RemoteProtocolError, httpx.ConnectError, httpx.ReadTimeout,
                    httpx.ConnectTimeout, httpx.NetworkError, httpx.TimeoutException) as e:
                if attempt < MAX_RETRIES:
                    wait = (2 ** attempt) + random.uniform(0, 1)
                    self.log.warning(
                        f"Anthropic transport error (attempt {attempt+1}/{MAX_RETRIES}): "
                        f"{type(e).__name__}, retry in {wait:.1f}s"
                    )
                    await asyncio.sleep(wait)
                    continue
                raise RuntimeError(
                    f"Anthropic transport error after {MAX_RETRIES} retries: {type(e).__name__}"
                )
        
        content = ""
        tool_calls: list[ToolCall] = []
        for block in data.get("content", []):
            if block["type"] == "text":
                content += block["text"]
            elif block["type"] == "tool_use":
                tool_calls.append(ToolCall(
                    id=block["id"],
                    name=block["name"],
                    arguments=block.get("input", {}),
                ))
        
        usage = data.get("usage", {})
        
        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=data.get("stop_reason", "stop"),
            prompt_tokens=usage.get("input_tokens", 0),
            completion_tokens=usage.get("output_tokens", 0),
            total_tokens=usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
            elapsed_ms=elapsed_ms,
            model=data.get("model", self.model),
            provider="anthropic",
        )


class LLMRouter:
    """Роутер между умной и дешёвой моделями.
    
    См. roadmap раздел 9.6 (полный flow).
    """
    
    def __init__(self, config: Config):
        self.config = config
        self.log = get_logger("llm.router")

        if config.llm.is_multi_provider():
            # NEW: multi-provider — resolve smart/cheap from the providers list
            self.smart = self._resolve_from_list(config.llm, "smart")
            self.cheap = self._resolve_from_list(config.llm, "cheap")
            # Auto-config: если cheap не задан (нет имени/модели) → используем smart
            if self.cheap is None and self.smart is not None:
                config.llm.cheap_role = config.llm.smart_role
                self.cheap = self.smart
                self.log.info("Auto-configured cheap = smart (same provider+model)")
        else:
            # LEGACY: old single-provider fields
            if not config.llm.cheap_api_key and config.llm.smart_api_key:
                config.llm.cheap_api_key = config.llm.smart_api_key
                config.llm.cheap_provider = config.llm.smart_provider
                config.llm.cheap_base_url = config.llm.smart_base_url
                if config.llm.cheap_model == "gpt-4o-mini" and config.llm.smart_provider != "openai":
                    config.llm.cheap_model = config.llm.smart_model
                self.log.info(
                    f"Auto-configured cheap LLM: {config.llm.cheap_provider} / "
                    f"{config.llm.cheap_model} (same key as smart)"
                )
            self.smart = self._make_provider(config.llm, "smart")
            self.cheap = self._make_provider(config.llm, "cheap")

    def _resolve_from_list(self, llm_config: "LLMConfig", role: str):
        """Resolve a provider instance from the multi-provider list (new format).

        role = "smart" | "cheap" → RoleConfig(provider="openai", model="gpt-4o")
        → find ProviderEntry by name → construct a virtual LLMConfig → _make_provider.
        """
        role_cfg = llm_config.smart_role if role == "smart" else llm_config.cheap_role
        provider_name = role_cfg.provider
        model = role_cfg.model
        # Find the provider entry by name
        entry = next((p for p in llm_config.providers if p.name == provider_name), None)
        if not entry and llm_config.providers:
            entry = llm_config.providers[0]  # fallback to first
            self.log.warning(f"LLM {role}: provider '{provider_name}' not found, "
                             f"using '{entry.name}'")
        if not entry:
            self.log.error(f"LLM {role}: no providers configured")
            return None
        # Construct a virtual LLMConfig for the legacy _make_provider
        virtual = LLMConfig()
        virtual.smart_provider = entry.type
        virtual.smart_model = model
        virtual.smart_api_key = entry.api_key
        virtual.smart_base_url = entry.base_url
        # Also set cheap_* (for auto-config within _make_provider if needed)
        virtual.cheap_provider = entry.type
        virtual.cheap_model = model
        virtual.cheap_api_key = entry.api_key
        virtual.cheap_base_url = entry.base_url
        self.log.info(f"LLM {role}: {entry.name}/{model} (type={entry.type})")
        return self._make_provider(virtual, "smart")
    
    def _make_provider(self, llm_config: LLMConfig, role: str) -> LLMProvider:
        provider_name = (
            llm_config.smart_provider if role == "smart"
            else llm_config.cheap_provider
        )
        if provider_name == "anthropic":
            return AnthropicProvider(llm_config, role)
        return OpenAICompatibleProvider(llm_config, role)
    
    async def analyze_request(self, user_message: str) -> dict:
        """Дешёвая LLM анализирует запрос.
        
        Возвращает:
        {
            "is_trivial": bool,
            "trivial_response": str,  # если is_trivial
            "needs_memory": bool,
            "memory_queries": list[str],
            "complexity": "simple" | "medium" | "complex",
            "entities": list[str],  # ключевые сущности
        }
        """
        system = (
            "Ты — анализатор запросов для AI-агента. Проанализируй запрос пользователя "
            "и верни JSON с оценкой. Не отвечай на сам запрос, только анализируй.\n\n"
            "Формат ответа (строго JSON, без markdown):\n"
            "{\n"
            '  "is_trivial": false,\n'
            '  "trivial_response": "",\n'
            '  "needs_memory": false,\n'
            '  "memory_queries": [],\n'
            '  "complexity": "simple",\n'
            '  "entities": []\n'
            "}\n\n"
            "is_trivial=true только для приветствий, благодарностей, коротких ответов.\n"
            "needs_memory=true если запрос ссылается на прошлое обсуждение.\n"
            "complexity: simple (1-3 шага), medium (4-15), complex (>15 или анализ)."
        )
        
        messages = [
            LLMMessage(role="system", content=system),
            LLMMessage(role="user", content=user_message),
        ]
        
        try:
            resp = await self.cheap.chat(messages, temperature=0.1, max_tokens=500)
            # Парсим JSON
            content = resp.content.strip()
            # Убираем markdown обёртку если есть
            if content.startswith("```"):
                content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            
            return json.loads(content)
        except (json.JSONDecodeError, RuntimeError) as e:
            self.log.warning(f"Analysis failed, defaulting: {e}")
            return {
                "is_trivial": False,
                "needs_memory": False,
                "complexity": "simple",
                "entities": [],
                "memory_queries": [],
            }
    
    async def smart_chat(
        self,
        messages: list[LLMMessage],
        tools: list[dict] | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        """Вызвать умную LLM.

        Хард-таймаут 180с поверх httpx: провайдер, капающий keepalive-байтами,
        обходет read-timeout (120с) — asyncio.wait_for срубает вызов гарантированно.
        """
        import asyncio
        try:
            return await asyncio.wait_for(
                self.smart.chat(messages, tools, temperature, max_tokens),
                timeout=180,
            )
        except asyncio.TimeoutError:
            raise RuntimeError("smart LLM: hard timeout 180s (provider hangs / keepalive)")

    async def cheap_chat(
        self,
        messages: list[LLMMessage],
        temperature: float = 0.3,
        max_tokens: int = 1000,
    ) -> LLMResponse:
        """Вызвать дешёвую LLM (хард-таймаут 60с — см. smart_chat)."""
        import asyncio
        try:
            return await asyncio.wait_for(
                self.cheap.chat(messages, temperature=temperature, max_tokens=max_tokens),
                timeout=60,
            )
        except asyncio.TimeoutError:
            raise RuntimeError("cheap LLM: hard timeout 60s")
    
    async def extract_facts(self, dialog_text: str) -> list[dict]:
        """Дешёвая LLM извлекает факты из диалога (lazy consolidation).
        
        См. roadmap раздел 6.6.
        """
        system = (
            "Извлеки из диалога ТОЛЬКО конкретные решения и факты. "
            "Не извлекай гипотезы, вопросы без ответов, мнения.\n\n"
            "Каждый факт в формате JSON (массив):\n"
            "[{\n"
            '  "entity": "о чём факт",\n'
            '  "attribute": "что именно",\n'
            '  "value": "значение",\n'
            '  "category": "fact|decision|win|incident|preference",\n'
            '  "confidence": "high|medium|low",\n'
            '  "source_quote": "точная цитата из диалога"\n'
            "}]\n\n"
            "category:\n"
            "  decision — принятое решение/выбор;\n"
            "  win — достигнутый успех/результат;\n"
            "  incident — сбой/инцидент/проблема;\n"
            "  preference — указание/предпочтение юзера (надолго);\n"
            "  fact — прочий конкретный факт (по умолчанию).\n\n"
            "Если конкретных решений/фактов нет — верни пустой массив []."
        )

        messages = [
            LLMMessage(role="system", content=system),
            LLMMessage(role="user", content=dialog_text),
        ]

        try:
            resp = await self.cheap.chat(messages, temperature=0.1, max_tokens=2000)
            content = resp.content.strip()
            if content.startswith("```"):
                content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()

            facts = json.loads(content)
            # Фильтруем low confidence (вариант C, раздел 6.6). Clamp category (T1):
            # LLM может прислать мусор/пропустить → дефолт 'fact'.
            _CATS = {"fact", "decision", "win", "incident", "preference"}
            out = []
            for f in facts:
                if f.get("confidence") not in ("high", "medium"):
                    continue
                if f.get("category") not in _CATS:
                    f["category"] = "fact"
                out.append(f)
            return out
        except (json.JSONDecodeError, RuntimeError) as e:
            self.log.warning(f"Fact extraction failed: {e}")
            return []
