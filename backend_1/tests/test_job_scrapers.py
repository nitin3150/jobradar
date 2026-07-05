# backend/tests/test_job_scrapers.py
import pytest
import respx
import httpx
from app.scrapers.jobs.ashby import fetch_ashby_jobs
from app.scrapers.jobs.lever import fetch_lever_jobs
from app.scrapers.jobs.greenhouse import fetch_greenhouse_jobs


@respx.mock
@pytest.mark.asyncio
async def test_fetch_ashby_jobs_returns_list():
    respx.get("https://api.ashbyhq.com/posting-api/job-board/acme").mock(
        return_value=httpx.Response(
            200,
            json={
                "jobs": [
                    {"id": "123", "title": "AI Engineer", "jobUrl": "https://jobs.ashbyhq.com/acme/123", "descriptionHtml": "<p>We need AI.</p>"}
                ]
            },
        )
    )
    async with httpx.AsyncClient() as client:
        jobs = await fetch_ashby_jobs(client, "acme")
    assert len(jobs) == 1
    assert jobs[0]["title"] == "AI Engineer"
    assert jobs[0]["url"] == "https://jobs.ashbyhq.com/acme/123"


@respx.mock
@pytest.mark.asyncio
async def test_fetch_lever_jobs_returns_list():
    respx.get("https://api.lever.co/v0/postings/acme").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": "abc", "text": "ML Engineer", "hostedUrl": "https://jobs.lever.co/acme/abc", "descriptionPlain": "We need ML."}
            ],
        )
    )
    async with httpx.AsyncClient() as client:
        jobs = await fetch_lever_jobs(client, "acme")
    assert len(jobs) == 1
    assert jobs[0]["title"] == "ML Engineer"


@respx.mock
@pytest.mark.asyncio
async def test_fetch_greenhouse_jobs_returns_list():
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").mock(
        return_value=httpx.Response(
            200,
            json={
                "jobs": [
                    {"id": 999, "title": "LLM Engineer", "absolute_url": "https://boards.greenhouse.io/acme/jobs/999", "content": "We need LLM."}
                ]
            },
        )
    )
    async with httpx.AsyncClient() as client:
        jobs = await fetch_greenhouse_jobs(client, "acme")
    assert len(jobs) == 1
    assert jobs[0]["title"] == "LLM Engineer"
