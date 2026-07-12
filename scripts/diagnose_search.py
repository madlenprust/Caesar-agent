"""Диагностика: какие источники реально работают из этого окружения."""
import asyncio
import httpx
import json


async def test_url(name, method, url, **kwargs):
    print(f"\n=== {name} ===")
    print(f"  {method} {url}")
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.request(method, url, **kwargs)
            print(f"  Status: {resp.status_code}")
            print(f"  Length: {len(resp.text)} chars")
            print(f"  Content-Type: {resp.headers.get('content-type', 'N/A')}")
            if resp.status_code == 200:
                # Show first 300 chars to see what we got
                preview = resp.text[:300].replace('\n', ' ')
                print(f"  Preview: {preview}")
            else:
                print(f"  Body preview: {resp.text[:200]}")
            return resp.status_code == 200
    except Exception as e:
        print(f"  ERROR: {type(e).__name__}: {e}")
        return False


async def main():
    UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    
    # === 1. DuckDuckGo variants ===
    await test_url("DDG Lite ru", "GET", "https://lite.duckduckgo.com/lite/",
                   params={"q": "Hermes agent news", "kl": "ru-ru"},
                   headers={"User-Agent": UA})
    
    await test_url("DDG Lite en", "GET", "https://lite.duckduckgo.com/lite/",
                   params={"q": "Hermes agent news", "kl": "us-en"},
                   headers={"User-Agent": UA})
    
    await test_url("DDG HTML", "GET", "https://html.duckduckgo.com/html/",
                   params={"q": "Hermes agent news"},
                   headers={"User-Agent": UA})
    
    # === 2. Bing ===
    await test_url("Bing search", "GET", "https://www.bing.com/search",
                   params={"q": "Hermes agent news", "count": 10},
                   headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    
    # === 3. Google ===
    await test_url("Google search", "GET", "https://www.google.com/search",
                   params={"q": "Hermes agent news"},
                   headers={"User-Agent": UA})
    
    # === 4. GitHub API (no auth) — releases for hermes-agent ===
    await test_url("GitHub API search repos", "GET", 
                   "https://api.github.com/search/repositories",
                   params={"q": "hermes-agent", "sort": "updated", "per_page": 3},
                   headers={"User-Agent": UA, "Accept": "application/vnd.github+json"})
    
    await test_url("GitHub API NousResearch hermes-agent", "GET",
                   "https://api.github.com/repos/NousResearch/hermes-agent",
                   headers={"User-Agent": UA, "Accept": "application/vnd.github+json"})
    
    await test_url("GitHub API releases", "GET",
                   "https://api.github.com/repos/NousResearch/hermes-agent/releases",
                   headers={"User-Agent": UA, "Accept": "application/vnd.github+json"})
    
    # === 5. HN Algolia ===
    await test_url("HN Algolia search", "GET", "https://hn.algolia.com/api/v1/search",
                   params={"query": "hermes agent", "tags": "story", "hitsPerPage": 5})
    
    # === 6. Reddit ===
    await test_url("Reddit search .json", "GET", "https://www.reddit.com/search.json",
                   params={"q": "hermes agent", "limit": 5, "sort": "relevance"},
                   headers={"User-Agent": UA})
    
    # === 7. Wikipedia ===
    await test_url("Wikipedia search", "GET", "https://en.wikipedia.org/w/api.php",
                   params={"action": "query", "format": "json", "list": "search",
                           "srsearch": "hermes agent", "srlimit": 3})
    
    # === 8. RSS of common news sources ===
    await test_url("HN RSS front", "GET", "https://hnrss.org/frontpage",
                   headers={"User-Agent": UA})
    
    await test_url("TechCrunch RSS", "GET", "https://techcrunch.com/feed/",
                   headers={"User-Agent": UA})
    
    # === 9. Direct fetch hermes-agent.nousresearch.com ===
    await test_url("Hermes official site", "GET", "https://hermes-agent.nousresearch.com/",
                   headers={"User-Agent": UA})
    
    await test_url("Hermes news page", "GET", "https://www.hermes-ai.net/news/",
                   headers={"User-Agent": UA})


if __name__ == "__main__":
    asyncio.run(main())
