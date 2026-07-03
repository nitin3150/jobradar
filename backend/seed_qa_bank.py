# backend/seed_qa_bank.py
"""Seed the Q&A bank with common job application questions and answers.

Edit answers below before running.
Run: python seed_qa_bank.py
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from app.database import async_session
from app.models.qa_bank import QABankEntry, AnswerType

# ── Edit these answers ──────────────────────────────────────────────────────
ANSWERS = [
    ("authorized to work in the us", "Are you authorized to work in the US?", "Yes", AnswerType.TEXT),
    ("require visa sponsorship", "Do you require visa sponsorship?", "No", AnswerType.TEXT),
    ("years of experience", "Years of experience", "3", AnswerType.NUMBER),
    ("first name", "First name", "Nitin", AnswerType.TEXT),
    ("last name", "Last name", "Goyal", AnswerType.TEXT),
    ("email", "Email address", "goyal.niti@northeastern.edu", AnswerType.TEXT),
    ("phone", "Phone number", "", AnswerType.TEXT),  # fill in
    ("linkedin", "LinkedIn profile URL", "", AnswerType.TEXT),  # fill in
    ("github", "GitHub profile URL", "", AnswerType.TEXT),  # fill in
    ("salary", "Expected salary", "140000", AnswerType.NUMBER),
    ("willing to relocate", "Are you willing to relocate?", "No", AnswerType.TEXT),
    ("remote work", "Are you open to remote work?", "Yes", AnswerType.TEXT),
    ("how did you hear", "How did you hear about this position?", "Online job board", AnswerType.TEXT),
    ("cover letter", "Cover letter", "I am an MS AI student at Northeastern with 3 years of experience building LLM-powered applications using LangChain, LangGraph, and FastAPI. I am passionate about agentic systems and healthcare AI.", AnswerType.TEXT),
]
# ────────────────────────────────────────────────────────────────────────────


async def seed():
    async with async_session() as db:
        for pattern, canonical, answer, answer_type in ANSWERS:
            from sqlalchemy import select
            existing = await db.execute(
                select(QABankEntry).where(QABankEntry.question_pattern == pattern)
            )
            if existing.scalar_one_or_none():
                print(f"  skip: {pattern}")
                continue
            entry = QABankEntry(
                question_pattern=pattern,
                canonical_question=canonical,
                answer=answer if answer else None,
                answer_type=answer_type.value,
            )
            db.add(entry)
            print(f"  add:  {pattern}")
        await db.commit()
    print("Seed complete.")


if __name__ == "__main__":
    asyncio.run(seed())
