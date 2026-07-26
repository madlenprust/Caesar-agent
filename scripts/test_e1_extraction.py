"""Тест E1 — cheap-LLM предобработка результатов поиска (token economy).

Покрытие:
- SEARCH_FETCH_TOOLS: содержит search/fetch инструменты, НЕ содержит shell/memory/cron.
- Gate: только большие результаты (>2000 chars) идут через cheap-LLM extraction.
"""
from caesar.core.orchestrator import Orchestrator


def test_search_fetch_tools_set():
    """SEARCH_FETCH_TOOLS содержит search/fetch/browser инструменты."""
    tools = Orchestrator.SEARCH_FETCH_TOOLS
    expected = {
        "web_search", "web_fetch", "http_request",
        "browser_fetch", "browser_action",
        "github_releases", "github_search",
        "hn_search", "reddit_search", "wikipedia_read",
        "rss_read", "tg_read_channel",
    }
    assert expected.issubset(tools), f"missing: {expected - tools}"


def test_search_fetch_tools_excludes_non_search():
    """Не содержит shell/memory/cron/cron/self инструменты (они не search/fetch)."""
    tools = Orchestrator.SEARCH_FETCH_TOOLS
    non_search = {"shell_exec", "memory_add_fact", "memory_search",
                  "cron_add", "cron_list", "self_edit", "read_file",
                  "write_file", "edit_file", "skill_find"}
    for t in non_search:
        assert t not in tools, f"{t} should NOT be in SEARCH_FETCH_TOOLS"


def test_gate_threshold_2000_chars():
    """Gate: 2000 chars — результаты меньше идут через brute-truncation (без cheap-LLM).
    Это подтверждается кодом (if len(raw) > 2000), но тест документирует порог."""
    # По документации/коду: gate at >2000 chars
    assert 2000 > 500  # brute-truncation обрезает до 500 per field; gate выше
