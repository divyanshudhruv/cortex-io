import httpx
from bs4 import BeautifulSoup

async def browse(query: str, url: str) -> str:
    """
    Fetches the web page at the given URL and returns the main text content as a string.
    """
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        # Try to extract main content, fallback to all text
        main = soup.find('main')
        if main:
            text = main.get_text(separator='\n', strip=True)
        else:
            text = soup.get_text(separator='\n', strip=True)
        return text
