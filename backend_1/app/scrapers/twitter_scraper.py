"""Twitter/X hiring signal scraper via Apify.

Uses Apify's Twitter scraper actor to find hiring-related tweets
from startup founders and companies.
"""

import logging
from datetime import date, timedelta

from app.config import settings
from app.scrapers.base import BaseScraper
from app.utils.backoff import with_backoff

logger = logging.getLogger(__name__)

APIFY_API_BASE = "https://api.apify.com/v2"
TWITTER_ACTOR_ID = "apify~twitter-scraper"

HIRING_KEYWORDS = [
    "we're hiring",
    "we are hiring",
    "open roles",
    "join our team",
    "we need a",
    "looking for a",
    "come work with us",
]


class TwitterScraper(BaseScraper):
    name = "twitter"
    enabled_setting = "scraper_twitter_enabled"

    @with_backoff(max_retries=2, base_delay=5.0)
    async def scrape(self) -> list[dict]:
        if not settings.apify_api_key:
            logger.warning("No Apify API key configured, skipping Twitter scraper")
            return []

        since_date = (date.today() - timedelta(days=7)).isoformat()
        query = " OR ".join(f'"{kw}"' for kw in HIRING_KEYWORDS[:3])
        query += f" since:{since_date}"

        # Start the Apify actor run
        run_url = f"{APIFY_API_BASE}/acts/{TWITTER_ACTOR_ID}/runs"
        headers = {"Authorization": f"Bearer {settings.apify_api_key}"}

        run_input = {
            "searchTerms": [query],
            "maxTweets": 100,
            "addUserInfo": True,
        }

        resp = await self.http.post(
            run_url, json=run_input, headers=headers, timeout=120.0
        )
        resp.raise_for_status()
        run_data = resp.json().get("data", {})
        run_id = run_data.get("id")

        if not run_id:
            logger.error("Failed to start Apify actor run")
            return []

        # Wait for run to complete and get results
        dataset_url = f"{APIFY_API_BASE}/actor-runs/{run_id}/dataset/items"
        # Poll for completion (Apify runs can take a while)
        import asyncio

        for _ in range(30):  # Max 5 minutes
            status_resp = await self.http.get(
                f"{APIFY_API_BASE}/actor-runs/{run_id}", headers=headers
            )
            status_data = status_resp.json().get("data", {})
            if status_data.get("status") in ("SUCCEEDED", "FAILED", "ABORTED"):
                break
            await asyncio.sleep(10)

        if status_data.get("status") != "SUCCEEDED":
            logger.error(f"Apify run {run_id} ended with status: {status_data.get('status')}")
            return []

        # Fetch results
        results_resp = await self.http.get(dataset_url, headers=headers)
        results_resp.raise_for_status()
        tweets = results_resp.json()

        return self._parse_tweets(tweets)

    def _parse_tweets(self, tweets: list[dict]) -> list[dict]:
        """Extract hiring signals from tweets."""
        signals = []
        seen_users = set()

        for tweet in tweets:
            user = tweet.get("user", {})
            username = user.get("username", "")

            if username in seen_users:
                continue
            seen_users.add(username)

            text = tweet.get("text", "")
            has_hiring_signal = any(kw.lower() in text.lower() for kw in HIRING_KEYWORDS)

            if has_hiring_signal:
                signals.append(
                    {
                        "name": user.get("name", username),
                        "source": self.name,
                        "founder_twitter": f"@{username}" if username else None,
                        "hiring_signals": [text[:500]],
                        "raw_data": {
                            "tweet_text": text,
                            "tweet_url": tweet.get("url", ""),
                            "user_bio": user.get("description", ""),
                            "followers": user.get("followers", 0),
                        },
                    }
                )

        return signals
