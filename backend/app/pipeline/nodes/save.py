"""Node 4: Save to Database.

Upserts scored companies into PostgreSQL.
Skips duplicates using Redis dedup cache.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.pipeline.state import PipelineState
from app.redis_client import is_duplicate, mark_seen
from app.utils.slug import make_slug

logger = logging.getLogger(__name__)


async def save_node(state: PipelineState, db_session: AsyncSession | None = None) -> dict:
    """Save scored companies to PostgreSQL."""
    scored = state.get("scored_companies", [])
    errors = list(state.get("errors", []))
    saved_count = 0

    if db_session is None:
        from app.database import async_session

        async with async_session() as db_session:
            saved_count = await _save_companies(scored, db_session, errors)
            await db_session.commit()
    else:
        saved_count = await _save_companies(scored, db_session, errors)
        await db_session.commit()

    stats = {**state.get("stats", {}), "saved": saved_count}
    return {"saved_count": saved_count, "errors": errors, "stats": stats}


async def _save_companies(
    companies: list[dict], db: AsyncSession, errors: list[str]
) -> int:
    """Save or update companies in the database."""
    saved = 0

    for data in companies:
        try:
            name = data.get("name", "")
            if not name:
                continue

            slug = data.get("name_slug") or make_slug(name)
            funding_date = data.get("funding_date", "")

            # Check Redis dedup
            if funding_date and await is_duplicate(slug, str(funding_date)):
                # Check if we should update the existing record (new score data)
                existing = await db.execute(
                    select(Company).where(Company.name_slug == slug)
                )
                existing_company = existing.scalar_one_or_none()
                if existing_company:
                    _update_company(existing_company, data)
                    saved += 1
                continue

            # Check if company exists in DB
            result = await db.execute(
                select(Company).where(Company.name_slug == slug)
            )
            existing_company = result.scalar_one_or_none()

            if existing_company:
                _update_company(existing_company, data)
            else:
                company = Company(
                    name=name,
                    name_slug=slug,
                    website=data.get("website"),
                    funding_amount=data.get("funding_amount"),
                    funding_stage=data.get("funding_stage", "unknown"),
                    funding_date=_parse_date(data.get("funding_date")),
                    source=data.get("source", "unknown"),
                    source_url=data.get("source_url"),
                    founder_name=data.get("founder_name"),
                    founder_twitter=data.get("founder_twitter"),
                    founder_linkedin=data.get("founder_linkedin"),
                    team_size=data.get("team_size"),
                    description=data.get("description"),
                    hiring_intent_score=data.get("hiring_intent_score", 0),
                    hiring_signals=data.get("hiring_signals", []),
                    likely_roles=data.get("likely_roles", []),
                    company_summary=data.get("company_summary"),
                    category=data.get("category", "startup"),
                )
                db.add(company)

            # Mark as seen in Redis
            if funding_date:
                await mark_seen(slug, str(funding_date))

            saved += 1

        except Exception as e:
            logger.error(f"Failed to save company {data.get('name', '?')}: {e}")
            errors.append(f"save: {data.get('name', '?')}: {e}")

    return saved


def _update_company(company: Company, data: dict) -> None:
    """Update an existing company with new data."""
    if data.get("hiring_intent_score") is not None:
        company.hiring_intent_score = data["hiring_intent_score"]
    if data.get("hiring_signals"):
        company.hiring_signals = data["hiring_signals"]
    if data.get("likely_roles"):
        company.likely_roles = data["likely_roles"]
    if data.get("company_summary"):
        company.company_summary = data["company_summary"]
    if data.get("description") and not company.description:
        company.description = data["description"]
    if data.get("founder_name") and not company.founder_name:
        company.founder_name = data["founder_name"]
    if data.get("founder_twitter") and not company.founder_twitter:
        company.founder_twitter = data["founder_twitter"]
    if data.get("team_size") and not company.team_size:
        company.team_size = data["team_size"]
    company.updated_at = datetime.now(timezone.utc)


def _parse_date(date_str: str | None) -> datetime | None:
    """Parse various date formats into datetime."""
    if not date_str:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(date_str, fmt).replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
    return None
