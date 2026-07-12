"""Инструменты категории 2: Интернет.

См. roadmap раздел 11.3.

web_search: поиск через DDG (default), Tavily, Brave, SearXNG
web_fetch: скачать страницу, извлечь текст
http_request: произвольный HTTP
"""

import asyncio
import re
from typing import Any
from urllib.parse import quote_plus, urljoin

import httpx

from caesar.tools.base import Tool, ToolResult


class WebSearchTool(Tool):
    """Поиск в интернете.
    
    Порядок движков (по результатам реальной диагностики 2026-07):
      1. Bing HTML — стабильно 200, парсер извлекает URL из <cite>
      2. DDG html endpoint — нестабилен (то 202 CAPTCHA, то timeout),
         но иногда срабатывает. Запасной.
    
    DDG Lite убран — почти всегда возвращает пустую страницу (202 без результатов).
    Google не используется — 429 rate limit без auth.
    """
    
    name = "web_search"
    description = (
        "Поиск в интернете. Возвращает список результатов (title, url, snippet). "
        "Primary: Bing. Fallback: DuckDuckGo. "
        "time_filter: day|week|month|year. "
        "Если total_found=0 — попробуй: github_releases (для новостей проекта), "
        "hn_search, reddit_search, wikipedia_read, или web_fetch на конкретный URL."
    )
    category = "internet"
    parameters_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Поисковый запрос"},
            "max_results": {"type": "integer", "default": 5, "description": "Макс 20"},
            "time_filter": {"type": "string", "enum": ["day", "week", "month", "year", None]},
        },
        "required": ["query"],
    }
    
    UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    
    async def execute(
        self,
        query: str,
        max_results: int = 5,
        time_filter: str | None = None,
        **_,
    ) -> ToolResult:
        max_results = min(max_results, 20)
        engines_tried = []
        
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                # Engine 1: Bing HTML — primary, стабильно работает
                engines_tried.append("bing_html")
                bing_params = {"q": query, "count": max_results * 2}
                # Bing поддерживает freshness параметр для time filter
                if time_filter:
                    bing_params["filters"] = f"ex1:\"ez5_{time_filter}\""
                resp = await client.get(
                    "https://www.bing.com/search",
                    params=bing_params,
                    headers={"User-Agent": self.UA, "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8"},
                )
                if resp.status_code == 200:
                    results = self._parse_bing_html(resp.text, max_results)
                    if results:
                        return ToolResult(
                            success=True,
                            data={
                                "results": results,
                                "total_found": len(results),
                                "engine": "bing_html",
                                "engines_tried": engines_tried,
                            },
                        )
                
                # Engine 2: DDG html endpoint — fallback (нестабилен, но иногда помогает)
                engines_tried.append("ddg_html")
                try:
                    resp = await client.get(
                        "https://html.duckduckgo.com/html/",
                        params={"q": query},
                        headers={"User-Agent": self.UA},
                    )
                    # DDG может вернуть 200 или 202 — принимаем оба
                    if resp.status_code in (200, 202):
                        results = self._parse_ddg_html(resp.text, max_results)
                        if results:
                            return ToolResult(
                                success=True,
                                data={
                                    "results": results,
                                    "total_found": len(results),
                                    "engine": "duckduckgo_html",
                                    "engines_tried": engines_tried,
                                },
                            )
                except (httpx.ConnectTimeout, httpx.ReadTimeout):
                    pass  # DDG часто таймаутит — просто пропускаем
                
                # Все движки пусты — даём LLM подсказку
                return ToolResult(
                    success=True,
                    data={
                        "results": [],
                        "total_found": 0,
                        "engine": "none",
                        "engines_tried": engines_tried,
                        "suggestion": (
                            "Все поисковые движки вернули 0 результатов. "
                            "Не повторяй этот же запрос. Попробуй: "
                            "(1) github_releases — если ищешь новости конкретного проекта, "
                            "(2) hn_search — для техно-новостей, "
                            "(3) reddit_search — для обсуждений, "
                            "(4) wikipedia_read — для фактов, "
                            "(5) web_fetch на конкретный URL если известен сайт."
                        ),
                    },
                )
        
        except Exception as e:
            return ToolResult(success=False, error=str(e))
    
    def _parse_ddg_lite(self, html: str, max_results: int) -> list[dict]:
        """Распарсить DDG Lite HTML."""
        import re
        results = []
        # DDG Lite возвращает таблицу с результатами
        # Ищем ссылки и текст
        link_pattern = re.compile(
            r'<a[^>]+class="result-link"[^>]*href="([^"]+)"[^>]*>([^<]+)</a>',
            re.IGNORECASE,
        )
        snippet_pattern = re.compile(
            r'<td[^>]+class="result-snippet"[^>]*>([^<]+)</td>',
            re.IGNORECASE,
        )
        
        links = link_pattern.findall(html)
        snippets = snippet_pattern.findall(html)
        
        for i, (url, title) in enumerate(links[:max_results]):
            snippet = snippets[i] if i < len(snippets) else ""
            # Очистка от HTML entities
            snippet = re.sub(r'<[^>]+>', '', snippet).strip()
            title = re.sub(r'<[^>]+>', '', title).strip()
            results.append({
                "title": title,
                "url": url,
                "snippet": snippet,
            })
        
        return results

    def _parse_ddg_html(self, html: str, max_results: int) -> list[dict]:
        """Распарсить DDG html.duckduckgo.com/html/ результаты."""
        import re
        import html as html_mod
        results = []
        # DDG html endpoint: <a class="result__a" href="...">title</a>
        # и <a class="result__snippet" ...>snippet</a>
        # URL в href имеет формат //duckduckgo.com/l/?uddg=<encoded>&rut=...
        link_pattern = re.compile(
            r'<a[^>]+class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            re.IGNORECASE | re.DOTALL,
        )
        snippet_pattern = re.compile(
            r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
            re.IGNORECASE | re.DOTALL,
        )
        
        links = link_pattern.findall(html)
        snippets = snippet_pattern.findall(html)
        
        for i, (raw_url, raw_title) in enumerate(links[:max_results]):
            # Декодируем DDG redirect URL
            url = raw_url
            uddg_match = re.search(r'uddg=([^&]+)', raw_url)
            if uddg_match:
                from urllib.parse import unquote
                url = unquote(uddg_match.group(1))
            elif url.startswith("//"):
                url = "https:" + url
            
            title = re.sub(r'<[^>]+>', '', raw_title)
            title = html_mod.unescape(title).strip()
            
            snippet = ""
            if i < len(snippets):
                snippet = re.sub(r'<[^>]+>', '', snippets[i])
                snippet = html_mod.unescape(snippet).strip()
            
            if title and url:
                results.append({
                    "title": title,
                    "url": url,
                    "snippet": snippet,
                })
        
        return results

    def _parse_bing_html(self, html: str, max_results: int) -> list[dict]:
        """Распарсить Bing search results HTML."""
        import re
        import html as html_mod
        results = []
        # Bing: <li class="b_algo"><h2><a href="URL">title</a></h2>
        # Реальный URL виден в <cite> теге (не в href, который = bing.com/ck/a redirect)
        block_pattern = re.compile(
            r'<li[^>]+class="b_algo"[^>]*>(.*?)</li>',
            re.IGNORECASE | re.DOTALL,
        )
        link_pattern = re.compile(
            r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            re.IGNORECASE | re.DOTALL,
        )
        # <cite> содержит видимый URL (может быть с <span> для подсветки)
        cite_pattern = re.compile(
            r'<cite[^>]*>(.*?)</cite>',
            re.IGNORECASE | re.DOTALL,
        )
        
        for block in block_pattern.findall(html):
            if len(results) >= max_results:
                break
            link_m = link_pattern.search(block)
            if not link_m:
                continue
            href = link_m.group(1)
            title = re.sub(r'<[^>]+>', '', link_m.group(2))
            title = html_mod.unescape(title).strip()
            
            # Извлекаем реальный URL из <cite>
            url = href
            cite_m = cite_pattern.search(block)
            if cite_m:
                cite_text = re.sub(r'<[^>]+>', '', cite_m.group(1))
                cite_text = html_mod.unescape(cite_text).strip()
                if cite_text:
                    # <cite> может содержать "https://example.com" или "example.com/..."
                    if cite_text.startswith("http://") or cite_text.startswith("https://"):
                        url = cite_text
                    else:
                        url = "https://" + cite_text
            
            # Если URL всё ещё bing.com/ck/a — пропускаем, бесполезен
            if "bing.com/ck/a" in url:
                continue
            
            # snippet — текст после </h2>, обычно в <p>
            snippet = ""
            after_h2 = block.split("</h2>", 1)[-1] if "</h2>" in block else ""
            if after_h2:
                snippet = re.sub(r'<[^>]+>', ' ', after_h2)
                snippet = html_mod.unescape(snippet)
                snippet = re.sub(r'\s+', ' ', snippet).strip()[:300]
            
            if title and url:
                results.append({
                    "title": title,
                    "url": url,
                    "snippet": snippet,
                })
        
        return results


class WebFetchTool(Tool):
    """Скачать веб-страницу, извлечь текст."""
    
    name = "web_fetch"
    description = (
        "Скачать веб-страницу, вернуть чистый текст/markdown. "
        "Использует readability для извлечения главного контента. "
        "Максимум 50K символов."
    )
    category = "internet"
    parameters_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "output_format": {"type": "string", "enum": ["markdown", "text", "html"], "default": "markdown"},
            "max_chars": {"type": "integer", "default": 50000},
            "timeout": {"type": "integer", "default": 30},
        },
        "required": ["url"],
    }
    
    async def execute(
        self,
        url: str,
        output_format: str = "markdown",
        max_chars: int = 50000,
        timeout: int = 30,
        **_,
    ) -> ToolResult:
        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Agent/0.1"},
            ) as client:
                resp = await client.get(url)
                
                if resp.status_code != 200:
                    return ToolResult(
                        success=False,
                        error=f"HTTP {resp.status_code}",
                        data={"status_code": resp.status_code},
                    )
                
                content_type = resp.headers.get("content-type", "")
                html = resp.text
                
                # Простой extraction: убираем script, style, теги
                import re
                # Удаляем script и style
                html = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
                html = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL | re.IGNORECASE)
                # Удаляем nav, header, footer (rough)
                html = re.sub(r'<(nav|header|footer)[^>]*>.*?</\1>', '', html, flags=re.DOTALL | re.IGNORECASE)
                
                # Извлекаем title
                title_match = re.search(r'<title[^>]*>([^<]+)</title>', html, re.IGNORECASE)
                title = title_match.group(1).strip() if title_match else ""
                
                if output_format == "html":
                    content = html
                elif output_format == "markdown":
                    # Простая конверсия: теги → markdown
                    content = html
                    content = re.sub(r'<h([1-6])[^>]*>(.*?)</h\1>', lambda m: '#' * int(m.group(1)) + ' ' + m.group(2), content, flags=re.IGNORECASE | re.DOTALL)
                    content = re.sub(r'<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', r'[\2](\1)', content, flags=re.IGNORECASE | re.DOTALL)
                    content = re.sub(r'<strong[^>]*>(.*?)</strong>', r'**\1**', content, flags=re.IGNORECASE | re.DOTALL)
                    content = re.sub(r'<em[^>]*>(.*?)</em>', r'*\1*', content, flags=re.IGNORECASE | re.DOTALL)
                    content = re.sub(r'<li[^>]*>(.*?)</li>', r'- \1\n', content, flags=re.IGNORECASE | re.DOTALL)
                    content = re.sub(r'<br\s*/?>', '\n', content, flags=re.IGNORECASE)
                    content = re.sub(r'<p[^>]*>(.*?)</p>', r'\1\n\n', content, flags=re.IGNORECASE | re.DOTALL)
                    # Убираем остальные теги
                    content = re.sub(r'<[^>]+>', '', content)
                    # Декодируем entities
                    import html as html_module
                    content = html_module.unescape(content)
                    # Сжимаем пробелы
                    content = re.sub(r'\n{3,}', '\n\n', content)
                    content = re.sub(r'[ \t]+', ' ', content)
                else:  # text
                    content = re.sub(r'<[^>]+>', ' ', html)
                    import html as html_module
                    content = html_module.unescape(content)
                    content = re.sub(r'\s+', ' ', content).strip()
                
                # Обрезаем
                truncated = False
                if len(content) > max_chars:
                    content = content[:max_chars] + f"\n... (truncated, total {len(content)} chars)"
                    truncated = True
                
                return ToolResult(
                    success=True,
                    data={
                        "url": str(resp.url),
                        "title": title,
                        "content": content,
                        "content_type": content_type,
                        "status_code": resp.status_code,
                        "bytes_fetched": len(resp.content),
                        "truncated": truncated,
                    },
                )
        
        except Exception as e:
            return ToolResult(success=False, error=str(e))


class HttpRequestTool(Tool):
    """Произвольный HTTP-запрос."""
    
    name = "http_request"
    description = (
        "Произвольный HTTP-запрос к любому API. "
        "Секретные заголовки (Authorization, X-API-Key) не логируются. "
        "Body ответа ограничен 1 MB."
    )
    category = "internet"
    parameters_schema = {
        "type": "object",
        "properties": {
            "method": {"type": "string", "enum": ["GET", "POST", "PUT", "DELETE", "PATCH"]},
            "url": {"type": "string"},
            "headers": {"type": "object", "default": {}},
            "params": {"type": "object", "default": {}},
            "json": {"type": "object"},
            "body": {"type": "string"},
            "timeout": {"type": "integer", "default": 30},
        },
        "required": ["method", "url"],
    }
    
    # Заголовки которые не логируем
    SECRET_HEADERS = {"authorization", "x-api-key", "x-auth-token", "cookie", "set-cookie"}
    
    async def execute(
        self,
        method: str,
        url: str,
        headers: dict | None = None,
        params: dict | None = None,
        json: dict | None = None,
        body: str | None = None,
        timeout: int = 30,
        **_,
    ) -> ToolResult:
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                resp = await client.request(
                    method=method,
                    url=url,
                    headers=headers or {},
                    params=params or {},
                    json=json if json is not None else None,
                    content=body if body else None,
                )
                
                # Маскируем секретные заголовки в response
                resp_headers = {}
                for k, v in resp.headers.items():
                    if k.lower() in self.SECRET_HEADERS:
                        resp_headers[k] = "***"
                    else:
                        resp_headers[k] = v
                
                body_text = resp.text
                if len(body_text) > 1024 * 1024:  # 1 MB
                    body_text = body_text[:1024 * 1024] + "... (truncated)"
                
                # Пытаемся распарсить JSON
                body_json = None
                if "application/json" in resp.headers.get("content-type", "").lower():
                    try:
                        import json as json_mod
                        body_json = json_mod.loads(body_text)
                    except Exception:
                        pass
                
                return ToolResult(
                    success=200 <= resp.status_code < 400,
                    data={
                        "status_code": resp.status_code,
                        "headers": resp_headers,
                        "body": body_text,
                        "body_json": body_json,
                        "elapsed_ms": int(resp.elapsed.total_seconds() * 1000),
                    },
                    error=None if resp.status_code < 400 else f"HTTP {resp.status_code}",
                )
        except Exception as e:
            return ToolResult(success=False, error=str(e))


class GithubReleasesTool(Tool):
    """Получить свежие релизы GitHub репозитория.
    
    Лучший способ узнать "что нового про проект X" — посмотреть его releases.
    Работает без токена (60 запросов/час на IP, чего достаточно для агента).
    """
    
    name = "github_releases"
    description = (
        "Получить последние релизы GitHub репозитория. Идеально для 'что нового про проект X'. "
        "Параметр repo в формате 'owner/repo' (например 'NousResearch/hermes-agent'). "
        "Возвращает: tag_name, name, published_at, body (changelog), html_url."
    )
    category = "internet"
    parameters_schema = {
        "type": "object",
        "properties": {
            "repo": {"type": "string", "description": "owner/repo (например 'NousResearch/hermes-agent')"},
            "limit": {"type": "integer", "default": 5, "description": "Макс 30"},
        },
        "required": ["repo"],
    }
    
    UA = "Caesar-Agent/0.1 (autonomous AI agent; +https://github.com/madlenprust/caesar)"
    
    async def execute(self, repo: str, limit: int = 5, **_) -> ToolResult:
        limit = min(limit, 30)
        # Нормализуем repo: убираем ведущий https://github.com/
        repo = repo.strip()
        if repo.startswith("https://github.com/"):
            repo = repo[len("https://github.com/"):]
        if repo.startswith("http://github.com/"):
            repo = repo[len("http://github.com/"):]
        repo = repo.rstrip("/")
        
        if "/" not in repo:
            return ToolResult(
                success=False,
                error=f"repo должен быть в формате 'owner/repo', получено: {repo}",
            )
        
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"https://api.github.com/repos/{repo}/releases",
                    params={"per_page": limit},
                    headers={
                        "User-Agent": self.UA,
                        "Accept": "application/vnd.github+json",
                    },
                )
                
                if resp.status_code == 403:
                    return ToolResult(
                        success=False,
                        error="GitHub API rate limit (60 req/hour без токена). Подождите или используйте web_fetch.",
                    )
                if resp.status_code == 404:
                    return ToolResult(
                        success=False,
                        error=f"Репозиторий {repo} не найден",
                    )
                if resp.status_code != 200:
                    return ToolResult(
                        success=False,
                        error=f"GitHub API error: HTTP {resp.status_code}",
                    )
                
                releases_raw = resp.json()
                if not isinstance(releases_raw, list):
                    return ToolResult(success=False, error="Unexpected GitHub response format")
                
                releases = []
                for rel in releases_raw[:limit]:
                    body = (rel.get("body") or "").strip()
                    # Обрезаем очень длинные changelog
                    if len(body) > 3000:
                        body = body[:3000] + "... (truncated)"
                    releases.append({
                        "tag": rel.get("tag_name", ""),
                        "name": rel.get("name", ""),
                        "published": rel.get("published_at", ""),
                        "draft": rel.get("draft", False),
                        "prerelease": rel.get("prerelease", False),
                        "body": body,
                        "url": rel.get("html_url", ""),
                    })
                
                return ToolResult(
                    success=True,
                    data={
                        "repo": repo,
                        "releases": releases,
                        "total": len(releases),
                    },
                )
        except Exception as e:
            return ToolResult(success=False, error=str(e))


class GithubSearchTool(Tool):
    """Поиск репозиториев на GitHub через REST API."""
    
    name = "github_search"
    description = (
        "Поиск репозиториев на GitHub по запросу. Возвращает: full_name, description, "
        "stars, language, updated_at, url. Полезно для 'найди проект X' "
        "или чтобы узнать owner/repo для github_releases."
    )
    category = "internet"
    parameters_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Поисковый запрос"},
            "language": {"type": "string", "description": "Фильтр по языку (например 'python')"},
            "limit": {"type": "integer", "default": 5, "description": "Макс 30"},
        },
        "required": ["query"],
    }
    
    UA = "Caesar-Agent/0.1 (autonomous AI agent; +https://github.com/madlenprust/caesar)"
    
    async def execute(self, query: str, language: str | None = None, limit: int = 5, **_) -> ToolResult:
        limit = min(limit, 30)
        q = query
        if language:
            q += f" language:{language}"
        
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    "https://api.github.com/search/repositories",
                    params={"q": q, "sort": "stars", "order": "desc", "per_page": limit},
                    headers={
                        "User-Agent": self.UA,
                        "Accept": "application/vnd.github+json",
                    },
                )
                
                if resp.status_code == 403:
                    return ToolResult(
                        success=False,
                        error="GitHub API rate limit (10 req/min для search без токена). Подождите.",
                    )
                if resp.status_code != 200:
                    return ToolResult(
                        success=False,
                        error=f"GitHub API error: HTTP {resp.status_code}",
                    )
                
                data = resp.json()
                repos = []
                for item in data.get("items", [])[:limit]:
                    repos.append({
                        "full_name": item.get("full_name", ""),
                        "description": item.get("description", "") or "",
                        "stars": item.get("stargazers_count", 0),
                        "language": item.get("language", "") or "",
                        "updated": item.get("updated_at", ""),
                        "url": item.get("html_url", ""),
                        "owner_repo": item.get("full_name", ""),  # для передачи в github_releases
                    })
                
                return ToolResult(
                    success=True,
                    data={
                        "query": query,
                        "total_found": data.get("total_count", 0),
                        "repos": repos,
                        "returned": len(repos),
                    },
                )
        except Exception as e:
            return ToolResult(success=False, error=str(e))


def get_internet_tools() -> list[Tool]:
    return [
        WebSearchTool(),
        WebFetchTool(),
        HttpRequestTool(),
        GithubReleasesTool(),
        GithubSearchTool(),
    ]
