"""Outreach generation endpoints."""

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.company import Company
from app.models.outreach import OutreachMessage
from app.pipeline.llm import call_claude
from app.schemas.outreach import OutreachRequest, OutreachResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/outreach", tags=["outreach"])

OUTREACH_PROMPT = """Generate a personalized {type} for {founder_name} at {company_name}.

Their company: {company_summary}
Recent funding: {funding_stage} round of {funding_amount}
Hiring signals: {hiring_signals}

Applicant info:
- Name: {user_name}
- Current role: {user_role}
- Skills: {user_skills}
- Background: {user_background}

Requirements:
- Be specific to their recent funding
- Mention a relevant skill match
- Under 150 words
- No fluff, conversational not salesy
- {type_specific_instructions}

Write ONLY the message, no subject line or greeting prefix for DMs."""

TYPE_INSTRUCTIONS = {
    "email": "Include a clear subject line on the first line, then the email body. Keep professional but warm.",
    "twitter_dm": "Keep it under 280 characters if possible. Very casual and direct.",
    "linkedin": "Professional tone. Reference a shared connection or interest if possible.",
}


@router.post("/generate", response_model=OutreachResponse)
async def generate_outreach(
    req: OutreachRequest,
    db: AsyncSession = Depends(get_db),
):
    """Generate a personalized outreach message using Claude."""
    company = await db.get(Company, req.company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    # Build prompt
    funding_amount_str = (
        f"${company.funding_amount:,.0f}" if company.funding_amount else "undisclosed"
    )
    signals = company.hiring_signals or []
    signals_text = "; ".join(signals[:3]) if signals else "No specific signals detected"

    prompt = OUTREACH_PROMPT.format(
        type=req.type,
        founder_name=company.founder_name or "the team",
        company_name=company.name,
        company_summary=company.company_summary or company.description or "A recently funded startup",
        funding_stage=company.funding_stage,
        funding_amount=funding_amount_str,
        hiring_signals=signals_text,
        user_name=req.user_context.name or "a software engineer",
        user_role=req.user_context.role or "Software Engineer",
        user_skills=", ".join(req.user_context.skills) if req.user_context.skills else "engineering",
        user_background=req.user_context.background or "",
        type_specific_instructions=TYPE_INSTRUCTIONS.get(req.type, ""),
    )

    # Generate with Claude, fallback on error
    try:
        content = await call_claude(prompt, max_tokens=512)
    except Exception as e:
        logger.error(f"Outreach generation failed: {e}")
        content = (
            f"Hi {company.founder_name or 'there'},\n\n"
            f"I noticed {company.name}'s recent {company.funding_stage} round — "
            f"congratulations! I'd love to chat about how my background in "
            f"{req.user_context.role or 'engineering'} could help as you grow.\n\n"
            f"Best,\n{req.user_context.name or 'Me'}"
        )

    # Save to DB
    message = OutreachMessage(
        company_id=req.company_id,
        type=req.type,
        content=content,
    )
    db.add(message)
    await db.flush()
    await db.refresh(message)

    return OutreachResponse.model_validate(message)


@router.get("/{company_id}", response_model=list[OutreachResponse])
async def list_outreach(
    company_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """List all outreach messages for a company."""
    result = await db.execute(
        select(OutreachMessage)
        .where(OutreachMessage.company_id == company_id)
        .order_by(OutreachMessage.generated_at.desc())
    )
    messages = result.scalars().all()
    return [OutreachResponse.model_validate(m) for m in messages]
