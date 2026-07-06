import httpx
from datetime import datetime

def HN_scan() -> dict[str, str]:
    try:
        res = httpx.get("https://hacker-news.firebaseio.com/v0/jobstories.json", timeout=10.0)
        res.raise_for_status()
        ids = res.json()
        first_story_id = ids[0] if ids else None
        if first_story_id is None:
            return {"res": "No Hacker News job stories found"}

        story_url = f"https://hacker-news.firebaseio.com/v0/item/{first_story_id}.json"
        story_resp = httpx.get(story_url, timeout=10.0)
        story_resp.raise_for_status()
        info = story_resp.json()
        info["time"] = datetime.utcfromtimestamp(info["time"]).isoformat()
        print(info)
        return {"res": str(info)}
    except Exception as exc:
        return {"res": f"funding error: {exc}"}