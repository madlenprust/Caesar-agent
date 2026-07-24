"""Тесты H1 — Browser automation (Playwright).

Покрытие:
- get_browser_tools: 2 инструмента, правильные имена.
- import-guard: playwright не установлен → понятная ошибка с инструкцией.
- browser_fetch с mock-playwright: рендер → text+title.
- browser_action с mock-playwright: steps (text/click/fill) → results + final_text.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from caesar.tools.browser import (
    BrowserActionTool,
    BrowserFetchTool,
    get_browser_tools,
    _INSTALL_HINT,
)


# --- регистрация ---

def test_get_browser_tools_returns_two():
    tools = get_browser_tools()
    names = {t.name for t in tools}
    assert names == {"browser_fetch", "browser_action"}


# --- import-guard (playwright не установлен) ---

async def test_browser_fetch_no_playwright_install_hint():
    with patch("caesar.tools.browser._get_playwright", return_value=None):
        r = await BrowserFetchTool().execute(url="http://example.com")
    assert not r.success
    assert "playwright" in r.error.lower()
    assert "install" in r.error.lower()


async def test_browser_action_no_playwright_install_hint():
    with patch("caesar.tools.browser._get_playwright", return_value=None):
        r = await BrowserActionTool().execute(url="http://example.com", steps=[{"action": "text"}])
    assert not r.success
    assert "playwright" in r.error.lower()


# --- mock-playwright ---

def _mock_playwright(text="Hello World", title="Test Title"):
    """Строит mock-дерево async with apw() as p: p.chromium.launch()→browser→page."""
    page = AsyncMock()
    page.title = AsyncMock(return_value=title)
    page.inner_text = AsyncMock(return_value=text)
    page.goto = AsyncMock(return_value=None)
    page.wait_for_selector = AsyncMock(return_value=None)
    page.wait_for_load_state = AsyncMock(return_value=None)
    page.click = AsyncMock(return_value=None)
    page.fill = AsyncMock(return_value=None)
    page.screenshot = AsyncMock(return_value=None)

    browser = AsyncMock()
    browser.new_page = AsyncMock(return_value=page)
    browser.close = AsyncMock(return_value=None)

    chromium = AsyncMock()
    chromium.launch = AsyncMock(return_value=browser)

    p = AsyncMock()
    p.chromium = chromium

    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=p)
    ctx.__aexit__ = AsyncMock(return_value=None)

    apw = MagicMock(return_value=ctx)
    return apw, page


async def test_browser_fetch_renders_text_and_title():
    apw, page = _mock_playwright(text="JS-rendered content", title="SPA Page")
    with patch("caesar.tools.browser._get_playwright", return_value=apw):
        r = await BrowserFetchTool().execute(url="http://spa.example.com")
    assert r.success
    assert r.data["title"] == "SPA Page"
    assert r.data["text"] == "JS-rendered content"
    assert r.data["url"] == "http://spa.example.com"
    page.goto.assert_called_once()


async def test_browser_fetch_truncates_huge_text():
    apw, _ = _mock_playwright(text="X" * 60000)
    with patch("caesar.tools.browser._get_playwright", return_value=apw):
        r = await BrowserFetchTool().execute(url="http://big.example.com")
    assert r.success
    assert len(r.data["text"]) <= 51000  # 50000 + обрезка
    assert "обрезано" in r.data["text"]


async def test_browser_action_text_step():
    apw, _ = _mock_playwright(text="Page body text", title="T")
    with patch("caesar.tools.browser._get_playwright", return_value=apw):
        r = await BrowserActionTool().execute(
            url="http://x.example.com", steps=[{"action": "text"}])
    assert r.success
    assert r.data["results"][0]["action"] == "text"
    assert r.data["results"][0]["text"] == "Page body text"


async def test_browser_action_click_then_text():
    apw, page = _mock_playwright(text="After click")
    with patch("caesar.tools.browser._get_playwright", return_value=apw):
        r = await BrowserActionTool().execute(
            url="http://x.example.com",
            steps=[
                {"action": "click", "selector": "#submit"},
                {"action": "text"},
            ])
    assert r.success
    assert len(r.data["results"]) == 2
    page.click.assert_called_once()
    assert r.data["results"][1]["text"] == "After click"


async def test_browser_action_fill_step():
    apw, page = _mock_playwright(text="filled")
    with patch("caesar.tools.browser._get_playwright", return_value=apw):
        r = await BrowserActionTool().execute(
            url="http://x.example.com",
            steps=[{"action": "fill", "selector": "#email", "value": "a@b.c"}])
    assert r.success
    page.fill.assert_called_once()
    assert r.data["results"][0]["action"] == "fill"
    assert r.data["results"][0]["value"] == "a@b.c"


async def test_browser_action_default_text_when_no_steps():
    apw, _ = _mock_playwright(text="default body")
    with patch("caesar.tools.browser._get_playwright", return_value=apw):
        r = await BrowserActionTool().execute(url="http://x.example.com")
    assert r.success
    assert r.data["results"][0]["text"] == "default body"
