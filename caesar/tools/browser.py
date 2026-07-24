"""Инструменты категории 10: Browser automation (H1, Playwright).

browser_fetch: рендер JS-страницы → видимый текст + title. Drop-in upgrade
  над web_fetch для SPA/JS-тяжёлых страниц (React/Vue/Angular — контент грузится JS).
browser_action: многошаговое взаимодействие (navigate/click/fill/text/screenshot)
  в одной браузер-сессии (с сохранением состояния между шагами — для логинов/форм).

Playwright — ОПЦИОНАЛЬНАЯ тяжёлая зависимость (~150MB browser binary). Если не
установлен → инструмент возвращает понятную ошибку с инструкцией по установке.
Регистрируется всегда (как STT), ошибки — на execute.
"""
from typing import Any

from caesar.tools.base import Tool, ToolResult


_INSTALL_HINT = (
    "Browser automation требует Playwright (опц. зависимость ~150MB). "
    "Установи: pip install playwright && playwright install chromium"
)


def _get_playwright():
    """Импорт playwright async_api. None если не установлен."""
    try:
        from playwright.async_api import async_playwright
        return async_playwright
    except ImportError:
        return None


class BrowserFetchTool(Tool):
    """Скачать JS-рендеренную страницу через реальный браузер."""

    name = "browser_fetch"
    description = (
        "Скачать страницу через реальный браузер (Playwright) — JS рендерится. "
        "Возвращает видимый текст + title. Используй ЭТО вместо web_fetch для "
        "SPA/JS-тяжёлых страниц (React/Vue/Angular, динамический контент). "
        "Для простых статических HTML — быстрее web_fetch."
    )
    category = "browser"
    parameters_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL страницы"},
            "wait_for": {"type": "string",
                         "description": "CSS-селектор — дождаться его перед извлечением текста (опц.)"},
            "timeout": {"type": "integer", "default": 15, "description": "Таймаут сек (макс 30)"},
        },
        "required": ["url"],
    }

    async def execute(self, url: str, wait_for: str = "", timeout: int = 15, **_) -> ToolResult:
        timeout = min(max(timeout, 5), 30)
        apw = _get_playwright()
        if apw is None:
            return ToolResult(success=False, error=_INSTALL_HINT)
        try:
            async with apw() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page()
                await page.goto(url, timeout=timeout * 1000, wait_until="domcontentloaded")
                if wait_for:
                    await page.wait_for_selector(wait_for, timeout=timeout * 1000)
                else:
                    try:
                        await page.wait_for_load_state("networkidle", timeout=timeout * 1000)
                    except Exception:
                        pass  # networkidle может не наступить — не критично
                title = await page.title()
                text = await page.inner_text("body")
                await browser.close()
            if len(text) > 50000:
                text = text[:50000] + "\n... (обрезано, browser_fetch)"
            return ToolResult(success=True, data={"title": title, "text": text, "url": url})
        except Exception as e:
            return ToolResult(success=False,
                              error=f"browser_fetch failed: {type(e).__name__}: {str(e)[:300]}")


class BrowserActionTool(Tool):
    """Многошаговое взаимодействие с JS-страницей (с сохранением состояния)."""

    name = "browser_action"
    description = (
        "Взаимодействие с JS-страницей через браузер (Playwright). Принимает список "
        "шагов steps — выполняет их в ОДНОЙ браузер-сессии (состояние сохраняется "
        "между шагами — для логинов/форм). "
        "step.action: navigate(url) | click(selector) | fill(selector,value) | "
        "text | title | screenshot(screenshot_path). "
        "Возвращает results[] (результат каждого шага) + final_text."
    )
    category = "browser"
    parameters_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Стартовый URL (navigate)"},
            "steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string",
                                   "enum": ["navigate", "click", "fill", "text", "title", "screenshot"]},
                        "selector": {"type": "string"},
                        "value": {"type": "string"},
                        "url": {"type": "string", "description": "URL для action=navigate"},
                        "screenshot_path": {"type": "string"},
                    },
                },
            },
            "timeout": {"type": "integer", "default": 15, "description": "Таймаут сек (макс 30)"},
        },
        "required": ["url"],
    }

    async def execute(self, url: str = "", steps: list | None = None,
                      timeout: int = 15, **_) -> ToolResult:
        timeout = min(max(timeout, 5), 30)
        apw = _get_playwright()
        if apw is None:
            return ToolResult(success=False, error=_INSTALL_HINT)
        if not steps:
            steps = [{"action": "text"}]  # по умолчанию — вернуть видимый текст

        try:
            async with apw() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page()
                if url:
                    await page.goto(url, timeout=timeout * 1000, wait_until="domcontentloaded")

                results: list[dict] = []
                for i, step in enumerate(steps):
                    act = step.get("action", "text")
                    sel = step.get("selector", "")
                    val = step.get("value", "")
                    shot = step.get("screenshot_path", "")
                    try:
                        if act == "navigate":
                            tgt = step.get("url", "") or sel or url
                            await page.goto(tgt, timeout=timeout * 1000, wait_until="domcontentloaded")
                            results.append({"step": i, "action": "navigate", "title": await page.title()})
                        elif act == "click":
                            await page.click(sel, timeout=timeout * 1000)
                            try:
                                await page.wait_for_load_state("networkidle", timeout=timeout * 1000)
                            except Exception:
                                pass
                            results.append({"step": i, "action": "click", "selector": sel})
                        elif act == "fill":
                            await page.fill(sel, val, timeout=timeout * 1000)
                            results.append({"step": i, "action": "fill", "selector": sel, "value": val})
                        elif act == "text":
                            txt = await page.inner_text("body")
                            results.append({"step": i, "action": "text", "text": txt[:10000]})
                        elif act == "title":
                            results.append({"step": i, "action": "title", "title": await page.title()})
                        elif act == "screenshot":
                            if not shot:
                                import tempfile, os
                                shot = os.path.join(tempfile.gettempdir(), f"caesar_browser_{i}.png")
                            await page.screenshot(path=shot, full_page=True)
                            results.append({"step": i, "action": "screenshot", "path": shot})
                        else:
                            results.append({"step": i, "error": f"unknown action: {act}"})
                    except Exception as e:
                        results.append({"step": i, "action": act,
                                        "error": f"{type(e).__name__}: {str(e)[:200]}"})

                final_text = ""
                try:
                    final_text = (await page.inner_text("body"))[:5000]
                except Exception:
                    pass
                await browser.close()

            return ToolResult(success=True,
                              data={"results": results, "final_text": final_text, "url": url})
        except Exception as e:
            return ToolResult(success=False,
                              error=f"browser_action failed: {type(e).__name__}: {str(e)[:300]}")


def get_browser_tools() -> list[Tool]:
    """Вернуть browser-инструменты. Playwright-зависимость — lazy (на execute)."""
    return [BrowserFetchTool(), BrowserActionTool()]
