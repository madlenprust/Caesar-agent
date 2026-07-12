"""End-to-end тест: поиск 'что нового про Hermes agent'.

Стратегия:
1. github_search → находим NousResearch/hermes-agent
2. github_releases → получаем свежие релизы
3. web_search → общие новости
4. web_fetch на https://www.hermes-ai.net/news/ — официальная страница новостей
"""
import asyncio
import sys
sys.path.insert(0, "/home/z/my-project")

from caesar.tools.internet import (
    WebSearchTool, WebFetchTool, GithubReleasesTool, GithubSearchTool
)
from caesar.tools.sources import HnSearchTool, RedditSearchTool, WikipediaReadTool


async def main():
    print("=" * 70)
    print("E2E ТЕСТ: 'что нового про Hermes agent'")
    print("=" * 70)
    
    # === 1. github_search ===
    print("\n--- 1. github_search('hermes agent') ---")
    tool = GithubSearchTool()
    result = await tool.execute(query="hermes agent", limit=3)
    print(f"success: {result.success}, error: {result.error}")
    if result.data:
        print(f"total_found: {result.data.get('total_found')}")
        for r in result.data.get("repos", []):
            print(f"  - {r['full_name']} ⭐{r['stars']} | {r['description'][:60]}")
            print(f"    Updated: {r['updated'][:10]} | URL: {r['url']}")
    
    # === 2. github_releases ===
    print("\n--- 2. github_releases('NousResearch/hermes-agent') ---")
    tool = GithubReleasesTool()
    result = await tool.execute(repo="NousResearch/hermes-agent", limit=3)
    print(f"success: {result.success}, error: {result.error}")
    if result.data:
        print(f"repo: {result.data.get('repo')}, total: {result.data.get('total')}")
        for rel in result.data.get("releases", []):
            print(f"\n  Release: {rel['tag']} ({rel['published'][:10]})")
            print(f"  Name: {rel['name'][:100]}")
            print(f"  Body preview: {rel['body'][:300]}")
            print(f"  URL: {rel['url']}")
    
    # === 3. web_search Bing ===
    print("\n--- 3. web_search('Hermes agent news', time_filter=month) ---")
    tool = WebSearchTool()
    result = await tool.execute(query="Hermes agent news", max_results=5, time_filter="month")
    print(f"success: {result.success}, error: {result.error}")
    if result.data:
        print(f"engine: {result.data.get('engine')}, total: {result.data.get('total_found')}")
        for r in result.data.get("results", [])[:3]:
            print(f"  - {r['title'][:80]}")
            print(f"    URL: {r['url']}")
            print(f"    snippet: {r['snippet'][:120]}")
    
    # === 4. web_fetch official news page ===
    print("\n--- 4. web_fetch('https://www.hermes-ai.net/news/') ---")
    tool = WebFetchTool()
    result = await tool.execute(url="https://www.hermes-ai.net/news/", max_chars=3000)
    print(f"success: {result.success}, error: {result.error}")
    if result.data:
        print(f"title: {result.data.get('title')}")
        content = result.data.get("content", "")
        print(f"content preview: {content[:600]}")
    
    # === 5. hn_search ===
    print("\n--- 5. hn_search('hermes agent') ---")
    tool = HnSearchTool()
    result = await tool.execute(query="hermes agent", limit=3)
    print(f"success: {result.success}, error: {result.error}")
    if result.data:
        print(f"total_found: {result.data.get('total_found')}")
        for p in result.data.get("posts", []):
            print(f"  - {p['title'][:80]} | points: {p.get('points', 0)}")
    
    # === 6. reddit_search (RSS) ===
    print("\n--- 6. reddit_search('hermes agent') ---")
    tool = RedditSearchTool()
    result = await tool.execute(query="hermes agent", limit=3)
    print(f"success: {result.success}, error: {result.error}")
    if result.data:
        print(f"total_found: {result.data.get('total_found')}")
        for p in result.data.get("posts", []):
            print(f"  - {p['title'][:80]}")
            print(f"    r/{p['subreddit']} | URL: {p['url']}")


if __name__ == "__main__":
    asyncio.run(main())
