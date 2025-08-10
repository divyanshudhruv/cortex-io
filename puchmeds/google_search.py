import httpx
from typing import List, Any
from dataclasses import dataclass

@dataclass
class SearchResult:
    url: str
    title: str = ""
    snippet: str = ""

@dataclass
class SearchResults:
    results: List[SearchResult]

async def search(queries: List[str]) -> List[SearchResults]:
    """
    Performs a DuckDuckGo search for each query and returns a list of SearchResults objects.
    """
    all_results = []
    for query in queries:
        url = f"https://html.duckduckgo.com/html/?q={query.replace(' ', '+')}"
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code != 200:
                all_results.append(SearchResults(results=[]))
                continue
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(resp.text, "html.parser")
            results = []
            for a in soup.find_all("a", class_="result__a", href=True):
                href = a["href"]
                title = a.get_text(strip=True)
                snippet = ""
                parent = a.find_parent("div", class_="result")
                if parent:
                    snippet_tag = parent.find("a", class_="result__snippet")
                    if snippet_tag:
                        snippet = snippet_tag.get_text(strip=True)
                results.append(SearchResult(url=href, title=title, snippet=snippet))
                if len(results) >= 5:
                    break
            all_results.append(SearchResults(results=results))
    return all_results
