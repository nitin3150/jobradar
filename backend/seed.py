"""Seed the database with sample companies for development/demo."""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from app.database import async_session, engine
from app.models.company import Company
from app.utils.slug import make_slug

SAMPLE_COMPANIES = [
    {
        "name": "Nextera AI",
        "website": "https://nextera.ai",
        "funding_amount": 12_000_000,
        "funding_stage": "series-a",
        "source": "sec_edgar",
        "description": "Building next-gen AI infrastructure for enterprise document understanding.",
        "founder_name": "Sarah Chen",
        "founder_twitter": "@sarahchen_ai",
        "hiring_intent_score": 92,
        "hiring_signals": [
            "We're hiring ML engineers to join our growing team!",
            "Looking for a senior backend engineer — remote friendly",
        ],
        "likely_roles": ["ML Engineer", "Backend Engineer", "Product Manager"],
        "company_summary": "Nextera AI builds AI-powered document processing for enterprises. Recently raised $12M Series A led by Sequoia.",
        "days_ago": 1,
    },
    {
        "name": "Stackflow",
        "website": "https://stackflow.dev",
        "funding_amount": 4_500_000,
        "funding_stage": "seed",
        "source": "yc",
        "description": "Developer tools for real-time collaborative debugging.",
        "founder_name": "Marcus Kim",
        "founder_twitter": "@marcuskim",
        "hiring_intent_score": 78,
        "hiring_signals": [
            "Join our team! We need passionate full-stack engineers",
        ],
        "likely_roles": ["Full-Stack Engineer", "DevRel", "Frontend Engineer"],
        "company_summary": "Stackflow (YC W25) is building collaborative debugging tools for dev teams. Raised $4.5M seed.",
        "days_ago": 3,
    },
    {
        "name": "Verdant Carbon",
        "website": "https://verdantcarbon.com",
        "funding_amount": 25_000_000,
        "funding_stage": "series-b",
        "source": "sec_edgar",
        "description": "Carbon credit marketplace powered by satellite imagery and ML.",
        "founder_name": "Dr. Priya Patel",
        "founder_linkedin": "https://linkedin.com/in/priyapatel",
        "hiring_intent_score": 85,
        "hiring_signals": [
            "Scaling our engineering team — open roles in ML and data",
            "Hiring a VP of Engineering to lead our next phase of growth",
        ],
        "likely_roles": ["ML Engineer", "Data Engineer", "VP Engineering", "Climate Scientist"],
        "company_summary": "Verdant Carbon uses satellite imagery and ML to verify carbon credits. Raised $25M Series B from a16z.",
        "days_ago": 2,
    },
    {
        "name": "PayNova",
        "website": "https://paynova.io",
        "funding_amount": 8_000_000,
        "funding_stage": "series-a",
        "source": "crunchbase",
        "description": "Instant cross-border B2B payments for emerging markets.",
        "founder_name": "Ade Okonkwo",
        "founder_twitter": "@adeokonkwo",
        "hiring_intent_score": 71,
        "hiring_signals": [
            "We're looking for backend engineers with fintech experience",
        ],
        "likely_roles": ["Backend Engineer", "Product Manager", "Compliance Officer"],
        "company_summary": "PayNova enables instant B2B cross-border payments in Africa and SE Asia. Raised $8M Series A.",
        "days_ago": 5,
    },
    {
        "name": "Cortex Robotics",
        "website": "https://cortexrobotics.com",
        "funding_amount": 2_000_000,
        "funding_stage": "seed",
        "source": "yc",
        "description": "Autonomous warehouse robots with advanced computer vision.",
        "founder_name": "Jake Morrison",
        "founder_twitter": "@jakemorrison",
        "hiring_intent_score": 65,
        "hiring_signals": [],
        "likely_roles": ["Robotics Engineer", "Computer Vision Engineer", "Firmware Engineer"],
        "company_summary": "Cortex Robotics (YC S24) builds autonomous warehouse robots. Raised $2M seed round.",
        "days_ago": 7,
    },
    {
        "name": "MediSync",
        "website": "https://medisync.health",
        "funding_amount": 18_000_000,
        "funding_stage": "series-a",
        "source": "sec_edgar",
        "description": "AI-powered clinical trial matching and patient recruitment platform.",
        "founder_name": "Dr. Lisa Wang",
        "founder_linkedin": "https://linkedin.com/in/lisawang",
        "hiring_intent_score": 88,
        "hiring_signals": [
            "Growing fast! Hiring across engineering, product, and clinical ops",
            "Open roles: Senior ML Engineer, Staff Backend Engineer",
        ],
        "likely_roles": ["ML Engineer", "Backend Engineer", "Clinical Data Scientist", "Product Designer"],
        "company_summary": "MediSync uses AI to match patients with clinical trials 10x faster. Raised $18M Series A from Benchmark.",
        "days_ago": 1,
    },
    {
        "name": "Synthwave Studios",
        "website": "https://synthwave.studio",
        "funding_amount": 750_000,
        "funding_stage": "pre-seed",
        "source": "yc",
        "description": "AI-generated music and sound design tools for content creators.",
        "founder_name": "DJ Nakamura",
        "founder_twitter": "@djnakamura",
        "hiring_intent_score": 45,
        "hiring_signals": [],
        "likely_roles": ["Audio ML Engineer", "Frontend Developer"],
        "company_summary": "Synthwave Studios builds AI music tools for creators. Pre-seed stage, YC W25 batch.",
        "days_ago": 10,
    },
    {
        "name": "DataForge",
        "website": "https://dataforge.io",
        "funding_amount": 30_000_000,
        "funding_stage": "series-b",
        "source": "sec_edgar",
        "description": "Real-time data pipeline orchestration for enterprise analytics teams.",
        "founder_name": "Tom Bradley",
        "founder_twitter": "@tombradley",
        "hiring_intent_score": 95,
        "hiring_signals": [
            "We're hiring across the board — 15 open roles!",
            "Join us! Looking for distributed systems engineers",
            "Hiring a Head of Product to lead our platform strategy",
        ],
        "likely_roles": ["Distributed Systems Engineer", "Head of Product", "SRE", "Solutions Architect", "Backend Engineer"],
        "company_summary": "DataForge provides real-time data pipeline orchestration. Raised $30M Series B led by Greylock.",
        "days_ago": 0,
    },
    {
        "name": "Orbitly",
        "website": "https://orbitly.space",
        "funding_amount": 6_000_000,
        "funding_stage": "seed",
        "source": "vc_a16z",
        "description": "Satellite-as-a-service platform for Earth observation startups.",
        "founder_name": "Elena Vasquez",
        "founder_twitter": "@elenavasquez",
        "hiring_intent_score": 58,
        "hiring_signals": [
            "Looking for aerospace engineers to join our mission",
        ],
        "likely_roles": ["Aerospace Engineer", "Backend Engineer", "DevOps Engineer"],
        "company_summary": "Orbitly offers satellite-as-a-service for Earth observation. Raised $6M seed from a16z.",
        "days_ago": 12,
    },
    {
        "name": "LegalMind AI",
        "website": "https://legalmind.ai",
        "funding_amount": 15_000_000,
        "funding_stage": "series-a",
        "source": "sec_edgar",
        "description": "AI copilot for contract review and legal document analysis.",
        "founder_name": "James Hartwell",
        "founder_linkedin": "https://linkedin.com/in/jameshartwell",
        "hiring_intent_score": 82,
        "hiring_signals": [
            "Hiring NLP engineers to build the future of legal AI",
            "Open role: Senior Product Designer",
        ],
        "likely_roles": ["NLP Engineer", "Product Designer", "Backend Engineer", "Legal Domain Expert"],
        "company_summary": "LegalMind AI automates contract review using LLMs. Raised $15M Series A from Sequoia.",
        "days_ago": 4,
    },
]


async def seed():
    async with async_session() as session:
        now = datetime.now(timezone.utc)
        count = 0

        for data in SAMPLE_COMPANIES:
            slug = make_slug(data["name"])

            company = Company(
                id=uuid.uuid4(),
                name=data["name"],
                name_slug=slug,
                website=data.get("website"),
                funding_amount=data.get("funding_amount"),
                funding_stage=data.get("funding_stage", "unknown"),
                funding_date=now - timedelta(days=data.get("days_ago", 0)),
                source=data.get("source", "unknown"),
                founder_name=data.get("founder_name"),
                founder_twitter=data.get("founder_twitter"),
                founder_linkedin=data.get("founder_linkedin"),
                description=data.get("description"),
                hiring_intent_score=data.get("hiring_intent_score", 50),
                hiring_signals=data.get("hiring_signals", []),
                likely_roles=data.get("likely_roles", []),
                company_summary=data.get("company_summary"),
                status="new",
            )
            session.add(company)
            count += 1

        await session.commit()
        print(f"Seeded {count} companies successfully!")


if __name__ == "__main__":
    asyncio.run(seed())
