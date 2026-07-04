from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_backend_env = Path(__file__).resolve().parent.parent / ".env"
_root_env = Path(__file__).resolve().parent.parent.parent / ".env"

_env_file = str(_backend_env if _backend_env.exists() else _root_env)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_env_file,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Database
    database_url: str

    redis_url: str

    # Primary provider
    llm_provider: str = "nvidia"

    # Fallback provider
    llm_fallback_provider: str = "groq"

    # NVIDIA
    nvidia_api_key: str = ""
    nvidia_model: str = "openai/z-ai/glm-5.2"
    nvidia_base_url: str = "https://integrate.api.nvidia.com/v1"

    # Groq
    groq_api_key: str = ""
    groq_model: str = "llama-3.3-70b-versatile"

    # CORS
    cors_origins: list[str] = ["http://localhost:3000", "http://localhost:5173"]

    # Apply worker
    apply_worker_screenshot_dir: str = "screenshots"
    resume_storage_dir: str = "resumes"
    scraper_jobs_enabled: bool = True

    # Scraper toggles
    scraper_sec_edgar_enabled: bool = True
    scraper_yc_enabled: bool = True
    scraper_vc_portfolio_enabled: bool = True
    scraper_twitter_enabled: bool = False
    scraper_crunchbase_enabled: bool = False

    # Additional startup scrapers
    scraper_hackernews_enabled: bool = True
    scraper_techcrunch_enabled: bool = True
    scraper_producthunt_enabled: bool = True

    # NGO/Nonprofit scrapers
    scraper_idealist_enabled: bool = True
    # unjobs.org is Cloudflare bot-walled over httpx (403) — off by default.
    scraper_unjobs_enabled: bool = False
    scraper_techjobsforgood_enabled: bool = True
    # ReliefWeb API v2 requires an approved appname (register at
    # https://apidoc.reliefweb.int/parameters#appname). Off until you set one.
    scraper_reliefweb_enabled: bool = False
    reliefweb_appname: str = "jobradar"

    # Scheduler
    pipeline_schedule_hour: int = 8
    pipeline_schedule_timezone: str = "America/New_York"
    twitter_refresh_hours: int = 6


    job_fetch_interval_hours: int = 1

    # Job scraper + review window
    review_window_hours: int = 2
    review_deadline_action: str = "reject"  # "reject" or "approve"
    qa_match_threshold: float = 0.75

    # Noise gate: only save jobs at/above this fit score (0.0 disables gate)
    job_fit_threshold: float = 0.6
    # Target roles (used for discovery search queries + title prefilter)
    target_roles: list[str] = [
        "AI Engineer",
        "Machine Learning Engineer",
        "LLM Engineer",
        "Software Engineer",
    ]

    # ATS board discovery (finds new company slugs via site: search)
    discovery_enabled: bool = True
    discovery_boards: list[str] = ["ashby", "greenhouse", "lever"]
    # Search backend: "auto" | "playwright" | "serper" | "apify".
    # "auto" picks serper when SERPER_API_KEY is set, else playwright.
    discovery_search_backend: str = "auto"
    # Freshness window for search recency filter (Google tbs=qdr:h{N})
    discovery_freshness_hours: int = 24
    # How often discovery runs (isolated from the hourly fetch loop)
    discovery_interval_hours: int = 24
    # Max result links to parse per (role x board) query
    discovery_max_results: int = 30

    # --- External API keys / paths (kept Optional-or-defaulted so their code
    #     paths degrade gracefully instead of crashing) ---
    # serper.dev SERP API (app/scrapers/jobs/search.py discovery path)
    serper_api_key: str | None = None
    # Apify actor key (enrichment.py Twitter signals, twitter_scraper.py)
    apify_api_key: str | None = None

    # Gmail poll (app/gmail/connector.py) — readonly poll only.
    # String defaults so os.path.exists()/label query never see None.
    gmail_token_path: str = "gmail_token.json"
    gmail_credentials_path: str = "gmail_credentials.json"
    gmail_label: str = "job-applications"

    # Apply worker dry-run: fill + attach + screenshot, but never click submit
    apply_dry_run: bool = False


settings = Settings()
