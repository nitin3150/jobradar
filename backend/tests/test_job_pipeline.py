# backend/tests/test_job_pipeline.py
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from app.pipeline.jobs import run_job_scrape_pipeline


@pytest.mark.asyncio
async def test_run_job_scrape_pipeline_creates_jobs():
    mock_session = AsyncMock()
    mock_http = AsyncMock()

    fake_company = MagicMock()
    fake_company.id = uuid4()
    fake_company.ats_type = "ashby"
    fake_company.ats_slug = "acme"

    # scalars().all() returns list of companies
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [fake_company]
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.add = MagicMock()
    mock_session.commit = AsyncMock()

    with patch("app.pipeline.jobs.fetch_ashby_jobs", return_value=[
        {"title": "AI Engineer", "url": "https://jobs.ashbyhq.com/acme/1", "jd_text": "We build AI.", "ats_type": "ashby"}
    ]):
        with patch("app.pipeline.jobs.score_job", return_value=(0.9, "Great fit")):
            # Need to patch the dedup check too — second execute call (dedup) returns no existing job
            dedup_result = MagicMock()
            dedup_result.scalar_one_or_none.return_value = None
            mock_session.execute = AsyncMock(side_effect=[mock_result, dedup_result])
            count = await run_job_scrape_pipeline(mock_session, mock_http)

    mock_session.add.assert_called_once()
    assert count == 1
