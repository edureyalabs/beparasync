# beparasync/web_search.py
import os
import httpx
from typing import Any


BRAVE_API_KEY = os.getenv("BRAVE_API_KEY", "")
BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"


async def run_web_search(query: str, count: int = 5) -> str:
    """
    Call Brave Search API and return a formatted string of results
    suitable for the LLM to read as a tool result.
    """
    if not BRAVE_API_KEY:
        return "Error: BRAVE_API_KEY is not configured on the server."

    params: dict[str, Any] = {
        "q":     query,
        "count": min(max(1, count), 10),  # Brave allows 1-10
    }
    headers = {
        "Accept":               "application/json",
        "Accept-Encoding":      "gzip",
        "X-Subscription-Token": BRAVE_API_KEY,
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(BRAVE_SEARCH_URL, params=params, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        return f"Error: Brave Search returned HTTP {e.response.status_code}: {e.response.text}"
    except Exception as e:
        return f"Error: Web search failed: {e}"

    web_results = data.get("web", {}).get("results", [])
    if not web_results:
        return f"No results found for query: {query}"

    lines = [f"Web search results for: {query}\n"]
    for i, r in enumerate(web_results, 1):
        title       = r.get("title", "No title")
        url         = r.get("url", "")
        description = r.get("description", "No description")
        lines.append(f"{i}. {title}\n   URL: {url}\n   {description}\n")

    return "\n".join(lines)