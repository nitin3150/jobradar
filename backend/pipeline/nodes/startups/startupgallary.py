import httpx
from bs4 import BeautifulSoup

def SG_scan() -> dict[str, str]:
    try:
        res = httpx.get("https://startups.gallery/news", timeout=10.0)
        html = res.text
        soup = BeautifulSoup(html, "html.parser")

        startup = soup.select('framer-1r7rp0')
        print("================")
        print(startup)
        res.raise_for_status()
        return {"res": f"Startups Gallery responded with status {res.status_code}"}
    except Exception as exc:
        return {"res": f"funding error: {exc}"}