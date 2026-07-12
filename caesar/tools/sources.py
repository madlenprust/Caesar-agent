"""Инструменты категории 3: Источники информации.

См. roadmap раздел 11.4.

Бесплатные: rss_read, tg_read_channel, hn_search, reddit_search, mastodon_read,
             youtube_read, github_read, wikipedia_read
"""

import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote_plus, urlparse

import httpx

from caesar.tools.base import Tool, ToolResult


def _parse_since(since: str | None) -> datetime | None:
    """Парсить '24h', '7d', '1h' → datetime."""
    if not since:
        return None
    m = re.match(r"(\d+)([hdw])", since)
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    delta = {"h": timedelta(hours=n), "d": timedelta(days=n), "w": timedelta(weeks=n)}[unit]
    return datetime.now(timezone.utc) - delta


class RssReadTool(Tool):
    """Читать RSS/Atom фид."""
    
    name = "rss_read"
    description = "Прочитать RSS/Atom фид. Возвращает последние N постов."
    category = "sources"
    parameters_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL фида"},
            "limit": {"type": "integer", "default": 10, "description": "Макс 50"},
            "since": {"type": "string", "description": "1h|24h|7d|30d"},
        },
        "required": ["url"],
    }
    
    async def execute(self, url: str, limit: int = 10, since: str | None = None, **_) -> ToolResult:
        limit = min(limit, 50)
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                resp = await client.get(url, headers={"User-Agent": "Agent/0.1 RSS Reader"})
                if resp.status_code != 200:
                    return ToolResult(success=False, error=f"HTTP {resp.status_code}")
                
                # Парсим через feedparser если доступен
                try:
                    import feedparser
                    feed = feedparser.parse(resp.text)
                    
                    posts = []
                    since_dt = _parse_since(since)
                    
                    for entry in feed.entries[:limit * 2]:  # берём с запасом
                        published = None
                        if hasattr(entry, "published_parsed") and entry.published_parsed:
                            published = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
                        elif hasattr(entry, "updated_parsed") and entry.updated_parsed:
                            published = datetime(*entry.updated_parsed[:6], tzinfo=timezone.utc)
                        
                        if since_dt and published and published < since_dt:
                            continue
                        
                        posts.append({
                            "title": getattr(entry, "title", ""),
                            "link": getattr(entry, "link", ""),
                            "published": published.isoformat() if published else "",
                            "summary": getattr(entry, "summary", "")[:500],
                            "author": getattr(entry, "author", ""),
                        })
                        
                        if len(posts) >= limit:
                            break
                    
                    return ToolResult(
                        success=True,
                        data={
                            "feed_title": feed.feed.get("title", ""),
                            "feed_url": url,
                            "posts": posts,
                            "total_returned": len(posts),
                        },
                    )
                except ImportError:
                    return ToolResult(
                        success=False,
                        error="feedparser не установлен",
                    )
        except Exception as e:
            return ToolResult(success=False, error=str(e))


class TgReadChannelTool(Tool):
    """Читать TG-канал через t.me/s/ парсинг (web режим)."""
    
    name = "tg_read_channel"
    description = (
        "Читать последние посты из публичного Telegram-канала. "
        "Принимает @username или URL. Только публичные каналы."
    )
    category = "sources"
    parameters_schema = {
        "type": "object",
        "properties": {
            "channel": {"type": "string", "description": "@username или URL"},
            "limit": {"type": "integer", "default": 10},
            "before": {"type": "integer", "description": "ID поста для пагинации (старые)"},
        },
        "required": ["channel"],
    }
    
    async def execute(self, channel: str, limit: int = 10, before: int | None = None, **_) -> ToolResult:
        # Нормализуем channel → username
        channel = channel.strip()
        if channel.startswith("https://t.me/"):
            username = channel.split("t.me/")[1].split("/")[0]
        elif channel.startswith("@"):
            username = channel[1:]
        else:
            username = channel
        
        # t.me/s/<username> — публичная веб-версия
        url = f"https://t.me/s/{username}"
        if before:
            url += f"/{before}?before={before}"
        
        try:
            async with httpx.AsyncClient(
                timeout=15,
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Agent/0.1"},
            ) as client:
                resp = await client.get(url)
                
                if resp.status_code != 200:
                    return ToolResult(
                        success=False,
                        error=f"HTTP {resp.status_code}. Возможно канал приватный или не существует.",
                    )
                
                html = resp.text
                
                # Парсим посты из HTML
                posts = self._parse_posts(html, limit)
                
                return ToolResult(
                    success=True,
                    data={
                        "channel_title": self._extract_title(html),
                        "channel_username": username,
                        "posts": posts,
                        "total_returned": len(posts),
                    },
                )
        except Exception as e:
            return ToolResult(success=False, error=str(e))
    
    def _parse_posts(self, html: str, limit: int) -> list[dict]:
        """Распарсить посты из t.me/s/ HTML."""
        posts = []
        # Ищем блоки .tgme_widget_message
        post_pattern = re.compile(
            r'<div[^>]+class="tgme_widget_message[^"]*"[^>]+data-post="([^"]+)"[^>]*>(.*?)</div>\s*</div>\s*</div>',
            re.DOTALL,
        )
        
        # Простой подход: ищем data-post
        for m in re.finditer(r'data-post="([^"]+)"', html):
            post_id_full = m.group(1)  # "username/123"
            try:
                channel, post_id = post_id_full.rsplit("/", 1)
                post_id_int = int(post_id)
            except (ValueError, IndexError):
                continue
            
            # Ищем контекст поста (грубо)
            start = m.end()
            end = html.find('data-post="', start)
            if end == -1:
                end = start + 10000
            post_html = html[start:end]
            
            # Текст поста
            text_match = re.search(
                r'<div[^>]+class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>',
                post_html, re.DOTALL,
            )
            text = ""
            if text_match:
                text = text_match.group(1)
                text = re.sub(r'<br\s*/?>', '\n', text)
                text = re.sub(r'<[^>]+>', '', text)
                import html as html_mod
                text = html_mod.unescape(text).strip()
            
            # Дата
            date_match = re.search(r'datetime="([^"]+)"', post_html)
            date_str = date_match.group(1) if date_match else ""
            
            posts.append({
                "id": post_id_int,
                "date": date_str,
                "text": text[:4000],
                "media_type": None,
                "media_urls": [],
                "link_to_post": f"https://t.me/{post_id_full}",
            })
            
            if len(posts) >= limit:
                break
        
        # Сортируем от новых к старым
        posts.sort(key=lambda p: p["id"], reverse=True)
        return posts
    
    def _extract_title(self, html: str) -> str:
        m = re.search(r'<title>([^<]+)</title>', html)
        return m.group(1).strip() if m else ""


class HnSearchTool(Tool):
    """HackerNews search."""
    
    name = "hn_search"
    description = "Поиск по HackerNews через Algolia API. Бесплатно, без ключа."
    category = "sources"
    parameters_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer", "default": 10},
            "since": {"type": "string", "description": "1h|24h|7d|30d"},
        },
        "required": ["query"],
    }
    
    async def execute(self, query: str, limit: int = 10, since: str | None = None, **_) -> ToolResult:
        limit = min(limit, 50)
        params = {
            "query": query,
            "tags": "story",
            "hitsPerPage": limit,
        }
        since_dt = _parse_since(since)
        if since_dt:
            params["numericFilters"] = f"created_at_i>{int(since_dt.timestamp())}"
        
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                resp = await client.get(
                    "https://hn.algolia.com/api/v1/search",
                    params=params,
                )
                if resp.status_code != 200:
                    return ToolResult(success=False, error=f"HTTP {resp.status_code}")
                
                data = resp.json()
                posts = [
                    {
                        "id": h["objectID"],
                        "title": h.get("title", ""),
                        "url": h.get("url", f"https://news.ycombinator.com/item?id={h['objectID']}"),
                        "points": h.get("points", 0),
                        "author": h.get("author", ""),
                        "date": datetime.fromtimestamp(h.get("created_at_i", 0), tz=timezone.utc).isoformat(),
                        "comments": h.get("num_comments", 0),
                    }
                    for h in data.get("hits", [])[:limit]
                ]
                return ToolResult(
                    success=True,
                    data={"posts": posts, "total_found": data.get("nbHits", 0)},
                )
        except Exception as e:
            return ToolResult(success=False, error=str(e))


class RedditSearchTool(Tool):
    """Reddit search через публичный RSS endpoint.
    
    ВАЖНО: Reddit закрыл публичный .json API (возвращает 403 без OAuth).
    Используем .rss endpoint — он ещё работает без auth.
    """
    
    name = "reddit_search"
    description = (
        "Поиск по Reddit через публичный RSS. Без ключа. "
        "Возвращает: title, url, permalink, subreddit, author, score, selftext."
    )
    category = "sources"
    parameters_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "subreddit": {"type": "string", "description": "Ограничить сабреддитом"},
            "limit": {"type": "integer", "default": 10},
        },
        "required": ["query"],
    }
    
    async def execute(self, query: str, subreddit: str | None = None, limit: int = 10, **_) -> ToolResult:
        limit = min(limit, 25)
        UA = "Caesar-Agent/0.1 (autonomous AI agent; +https://github.com/madlenprust/caesar)"
        
        # RSS endpoint: reddit.com/search.rss или reddit.com/r/{sub}/search.rss
        if subreddit:
            url = f"https://www.reddit.com/r/{subreddit}/search.rss"
            params = {"q": query, "restrict_sr": 1, "limit": limit, "sort": "relevance"}
        else:
            url = "https://www.reddit.com/search.rss"
            params = {"q": query, "limit": limit, "sort": "relevance"}
        
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    url,
                    params=params,
                    headers={"User-Agent": UA},
                )
                if resp.status_code != 200:
                    return ToolResult(success=False, error=f"HTTP {resp.status_code}")
                
                # Парсим Atom feed (Reddit возвращает Atom для .rss)
                posts = self._parse_reddit_atom(resp.text, limit)
                
                return ToolResult(
                    success=True,
                    data={"posts": posts, "total_found": len(posts)},
                )
        except Exception as e:
            return ToolResult(success=False, error=str(e))
    
    def _parse_reddit_atom(self, xml: str, limit: int) -> list[dict]:
        """Распарсить Reddit Atom feed."""
        import re
        import html as html_mod
        from urllib.parse import unquote
        
        posts = []
        # Atom <entry> элементы
        entry_pattern = re.compile(r'<entry[^>]*>(.*?)</entry>', re.DOTALL)
        
        for entry in entry_pattern.findall(xml):
            if len(posts) >= limit:
                break
            
            # Title
            title_m = re.search(r'<title[^>]*>(.*?)</title>', entry, re.DOTALL)
            title = html_mod.unescape(re.sub(r'<[^>]+>', '', title_m.group(1))).strip() if title_m else ""
            
            # Link — Atom использует <link href="..."/>
            link_m = re.search(r'<link[^>]*href="([^"]+)"', entry)
            link = unquote(html_mod.unescape(link_m.group(1))) if link_m else ""
            
            # ID (Reddit permalink)
            id_m = re.search(r'<id[^>]*>(.*?)</id>', entry, re.DOTALL)
            permalink = id_m.group(1).strip() if id_m else link
            
            # Updated (date)
            updated_m = re.search(r'<updated[^>]*>(.*?)</updated>', entry, re.DOTALL)
            updated = updated_m.group(1).strip() if updated_m else ""
            
            # Content (selftext)
            content_m = re.search(r'<content[^>]*>(.*?)</content>', entry, re.DOTALL)
            content = ""
            if content_m:
                content = html_mod.unescape(re.sub(r'<[^>]+>', '', content_m.group(1))).strip()
            
            # Author
            author_m = re.search(r'<name[^>]*>(.*?)</name>', entry, re.DOTALL)
            author = author_m.group(1).strip() if author_m else ""
            
            # Subreddit — извлекаем из permalink
            subreddit = ""
            if "/r/" in permalink:
                m = re.search(r'/r/([^/]+)', permalink)
                if m:
                    subreddit = m.group(1)
            
            if title or permalink:
                posts.append({
                    "title": title[:300],
                    "url": link,
                    "permalink": permalink,
                    "subreddit": subreddit,
                    "author": author,
                    "score": 0,  # RSS не отдаёт score
                    "created": updated,
                    "selftext": content[:500],
                })
        
        return posts


class WikipediaReadTool(Tool):
    """Читать Wikipedia."""
    
    name = "wikipedia_read"
    description = "Прочитать статью Wikipedia. Default: ru.wikipedia.org"
    category = "sources"
    parameters_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Термин для поиска"},
            "language": {"type": "string", "default": "ru"},
            "limit": {"type": "integer", "default": 1},
        },
        "required": ["query"],
    }
    
    # Wikipedia требует корректный UA с контактом, иначе 403
    UA = "Caesar-Agent/0.1 (autonomous AI agent; +https://github.com/madlenprust/caesar)"
    
    async def execute(self, query: str, language: str = "ru", limit: int = 1, **_) -> ToolResult:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                # Поиск через opensearch
                resp = await client.get(
                    f"https://{language}.wikipedia.org/w/api.php",
                    params={
                        "action": "query",
                        "format": "json",
                        "list": "search",
                        "srsearch": query,
                        "srlimit": limit,
                    },
                    headers={"User-Agent": self.UA},
                )
                if resp.status_code != 200:
                    return ToolResult(success=False, error=f"HTTP {resp.status_code}")
                
                data = resp.json()
                results = []
                for item in data.get("query", {}).get("search", [])[:limit]:
                    # Получаем полный текст
                    title = item["title"]
                    extract_resp = await client.get(
                        f"https://{language}.wikipedia.org/w/api.php",
                        params={
                            "action": "query",
                            "format": "json",
                            "titles": title,
                            "prop": "extracts",
                            "explaintext": 1,
                        },
                        headers={"User-Agent": self.UA},
                    )
                    extract_data = extract_resp.json()
                    pages = extract_data.get("query", {}).get("pages", {})
                    extract = ""
                    for p in pages.values():
                        extract = p.get("extract", "")
                        break
                    
                    results.append({
                        "title": title,
                        "url": f"https://{language}.wikipedia.org/wiki/{quote_plus(title)}",
                        "content": extract[:50000],
                        "snippet": item.get("snippet", ""),
                    })
                
                return ToolResult(
                    success=True,
                    data={"results": results, "total": len(results)},
                )
        except Exception as e:
            return ToolResult(success=False, error=str(e))


def get_sources_tools() -> list[Tool]:
    return [
        RssReadTool(),
        TgReadChannelTool(),
        HnSearchTool(),
        RedditSearchTool(),
        WikipediaReadTool(),
    ]
