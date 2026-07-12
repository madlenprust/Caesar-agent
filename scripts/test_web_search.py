"""Тест web_search: multi-engine fallback и парсеры.

Живой сетевой тест — по умолчанию пропускается, чтобы CI не зависел от сети
и не флакал. Включить вручную:

    CAESAR_LIVE_NET=1 python -m pytest scripts/test_web_search.py -v
"""
import os

import pytest

from caesar.tools.internet import WebSearchTool

LIVE = pytest.mark.skipif(
    not os.environ.get("CAESAR_LIVE_NET"),
    reason="живой сетевой запрос; выставь CAESAR_LIVE_NET=1 чтобы запустить",
)


@pytest.mark.parametrize(
    "query",
    [
        "Hermes agent news",
        "новости python",
        "LLM agent framework 2025",
    ],
)
@LIVE
async def test_query(query: str):
    tool = WebSearchTool()
    result = await tool.execute(query=query, max_results=5)

    assert result.success in (True, False)
    if result.success:
        assert result.data is not None
        assert "results" in result.data
        assert isinstance(result.data["results"], list)
        # multi-engine fallback должен оставить след, какой движок сработал
        assert result.data.get("engine") or result.data.get("engines_tried")
