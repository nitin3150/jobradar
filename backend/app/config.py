from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Look for .env in backend/ first, then project root
_backend_env = Path(__file__).resolve().parent.parent / ".env"
_root_env = Path(__file__).resolve().parent.parent.parent / ".env"
_env_file = str(_backend_env) if _backend_env.exists() else str(_root_env)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_env_file, env_file_encoding="utf-8", extra="ignore"
    )

    # Database
    database_url: str = "postgresql+asyncpg://fundingradar:secret@localhost:5432/fundingradar"

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # API Keys
    anthropic_api_key: str = ""
    google_api_key: str = ""
    groq_api_key: str = ""
    openrouter_api_key: str = ""
    nvidia_api_key: str = ""
    apify_api_key: str = ""

    # LiteLLM unified key (used when provider needs single key)
    llm_api_key: str = ""
    llm_api_base: str = ""  # override base URL (e.g. Nvidia NIM endpoint)

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
    # Search backend: "playwright" | "serper" | "apify"
    discovery_search_backend: str = "playwright"
    # Freshness window for search recency filter (Google tbs=qdr:h{N})
    discovery_freshness_hours: int = 24
    # How often discovery runs (isolated from the hourly fetch loop)
    discovery_interval_hours: int = 24
    # Max result links to parse per (role x board) query
    discovery_max_results: int = 30
    serper_api_key: str = ""

    # Job fetch loop interval (hours). Runtime-overridable via /pipeline/schedule.
    job_fetch_interval_hours: int = 1

    # Gmail connector
    gmail_credentials_path: str = "gmail_credentials.json"
    gmail_token_path: str = "gmail_token.json"
    gmail_label: str = "job-applications"

    # Apply worker
    apply_worker_screenshot_dir: str = "screenshots"
    scraper_jobs_enabled: bool = True

    # LLM Provider: "groq", "ollama", "gemini", "anthropic", "openrouter", "nvidia_nim"
    llm_provider: str = "groq"
    llm_model: str = "llama-3.3-70b-versatile"
    # Base URL (only needed for Ollama)
    llm_base_url: str = "http://localhost:11434/v1"

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
    scraper_unjobs_enabled: bool = True
    scraper_techjobsforgood_enabled: bool = True
    scraper_reliefweb_enabled: bool = True

    # Scheduler
    pipeline_schedule_hour: int = 8
    pipeline_schedule_timezone: str = "America/New_York"
    twitter_refresh_hours: int = 6

    # CORS
    cors_origins: list[str] = ["http://localhost:3000", "http://localhost:5173"]


settings = Settings()
