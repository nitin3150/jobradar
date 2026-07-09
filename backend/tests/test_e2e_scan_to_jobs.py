"""End-to-end ``POST /api/scan/<domain>`` → score → ``jobs`` table test.

The user-facing contract for ``/api/scan/funding`` (and every other
domain endpoint) is "fire a scan, get back the raw opportunities,
have winners silently land in the review queue". This integration
test verifies the full chain with both edges mocked:

* the scanner (``:func:`routes.scanner.scan_funding```) is patched
  to return a stable list of three opportunities with deterministic
  URLs so each one gets a stable UUID5 id downstream;
* the LLM client (``:class:`services.scoring_service.LLMClient```) is
  patched so ``score_opportunity`` returns ``0.85`` / ``0.91`` /
  ``0.30`` depending on the URL. The first two clear the default
  ``preferences.job_fit_threshold == 0.6``; the third does not.

Assertions:

* the response envelope (``message``, ``domain``, ``count``,
  ``opportunities``) is unchanged from the contract the React
  frontend reads;
* exactly **two** rows landed in the ``jobs`` table — the
  above-threshold winners;
* the below-threshold loser (``scan-203-below``) was silently
  dropped — no DB row, no entry in the response's opportunities
  list still mirrors the scan output (intentional: scoring is
  invisible, the response reflects raw scan results).

Run with ``JOBRADAR_TEST_DB=1`` so SQLAlchemy uses NullPool for
the per-test event loop in ``asyncio.run`` DB peek helpers.
"""
from __future__ import annotations

import asyncio
import os

# IMPORTANT: this env var must be set BEFORE `main` / `db.session`
# / `routes.scanner` are imported — SQLAlchemy constructs the engine
# pool at module-import time. setdefault also accepts the
# subprocess-injected form (``JOBRADAR_TEST_DB=1 python -m unittest …``).
os.environ.setdefault("JOBRADAR_TEST_DB", "1")

import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import delete as sa_delete, select

from db import models as db_models
from db.session import AsyncSessionLocal
from main import app
from routes.jobs import _seed_job_rows
from routes.settings import _PREFS_STATE, _reset_prefs


# ---------------------------------------------------------------------
# Test-only score tables keyed on mock URLs so each opportunity gets
# a stable, predictable score regardless of the asyncio.gather
# scheduling order inside ``_score_one``. Posted at module scope so
# both funding and oss tests share one source of truth; splitting
# into per-domain tables keeps each test self-contained and makes
# a future third-domain test (e.g. ngos) cheap to add.
# ---------------------------------------------------------------------
SCAN_URLS = {
    "scan-201-above": (0.85, "above threshold; solid AI-platform match"),
    "scan-202-verygood": (0.91, "above threshold; very close match"),
    "scan-203-below": (0.30, "below preferred threshold"),
}

SCAN_URLS_OSS = {
    "scan-oss-above": (0.7, "good"),
    "scan-oss-below": (0.2, "no"),
}

SCAN_URLS_BOARDS = {
    "scan-board-301-above": (0.81, "above threshold; direct AI platform role"),
    "scan-board-302-verygood": (0.93, "above threshold; very strong match"),
    "scan-board-303-below": (0.28, "below preferred threshold"),
}


async def _score_side_effect(profile, opp):
    """Async shape that mirrors ``LLMClient.score_opportunity``.

    Lookup is keyed on the URL — every mock opp has a unique URL,
    so concurrency of ``asyncio.gather`` doesn't change which score
    gets returned for any specific opportunity. ``.get`` with a
    defensive default keeps a future mock opp with a typo from
    crashing mid-test and masking the underlying assertion failure.

    Implementation note: AsyncMock with ``side_effect=async_func``
    awaits whatever the callable returns — sync ``side_effect``
    functions return plain values, async ones return coroutines
    which AsyncMock awaits automatically. We rely on the async form
    here so the test exercises the real
    ``await client.score_opportunity(...)`` path.
    """
    return SCAN_URLS.get(opp.get("url") or "", (0.0, "unknown URL — default reject"))


async def _score_side_effect_oss(profile, opp):
    """Same contract as :func:`_score_side_effect` for the oss test.

    Kept separate so a future maintainer who wants different
    reasoning text per domain can edit one table without touching
    the other.
    """
    return SCAN_URLS_OSS.get(
        opp.get("url") or "", (0.0, "unknown URL — default reject")
    )


async def _score_side_effect_boards(profile, opp):
    """Same contract for the boards test.

    Kept separate so a future maintainer who wants different
    reasoning text per domain can edit one table without touching
    the other. The boards path inherits the same URL → UUID5
    determinism the funding/oss tests rely on, so an LLM failure
    or scoring-service exception surfaces here with the same
    diagnostic shape.
    """
    return SCAN_URLS_BOARDS.get(
        opp.get("url") or "", (0.0, "unknown URL — default reject")
    )


# ---------------------------------------------------------------------
# DB helpers. Tests stay sync (TestCase) but each DB peek opens a
# fresh event loop via ``asyncio.run`` so NullPool binds a fresh
# asyncpg connection to the calling loop — no cross-loop errors.
# ---------------------------------------------------------------------
def _run(coro):
    return asyncio.run(coro)


async def _truncate_jobs_table() -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(sa_delete(db_models.Job))
        await session.commit()


async def _count_jobs_for_url(url: str) -> int:
    async with AsyncSessionLocal() as session:
        stmt = select(db_models.Job).where(db_models.Job.url == url)
        return len((await session.execute(stmt)).scalars().all())


async def _fetch_winner(url: str):
    async with AsyncSessionLocal() as session:
        stmt = select(db_models.Job).where(db_models.Job.url == url)
        return (await session.execute(stmt)).scalars().first()


# ---------------------------------------------------------------------
def _build_mock_opportunities():
    """Stable list of 3 mock opportunities keyed on the URL for scoring."""
    return [
        {
            "title": "Funding Round — Series B for AI Infra Co",
            "company_name": "Acme AI",
            "url": "scan-201-above",
            "description": "Series B funding round — leads matching target roles.",
            "source": "producthunt",
            "category": "boards",
        },
        {
            "title": "Stealth Launch — LLM Eval Platform",
            "company_name": "Stealth AI",
            "url": "scan-202-verygood",
            "description": "Stealth launch of an LLM eval / observability product.",
            "source": "startupsgallery",
            "category": "boards",
        },
        {
            "title": "Niche Funding — Off-profile consumer app",
            "company_name": "Local Co",
            "url": "scan-203-below",
            "description": "A small consumer launch — well below profile match.",
            "source": "producthunt",
            "category": "boards",
        },
    ]


# ---------------------------------------------------------------------
class TestScanFundingEndToEnd(unittest.TestCase):
    """Verify scan → score → DB persistence for ``/api/scan/funding``.

    The scanning domain is funding because it is the simplest shape
    (no extra parameters beyond delta_hours / limit / sources) and
    the route handler is sync (``def``, not ``async def``). The mock
    strategy is identical for oss / remote / ngos — only the
    patched scanner entry would change.
    """

    def setUp(self) -> None:
        # Reset in-memory prefs to factory defaults (threshold = 0.6
        # so the test's 0.85 / 0.91 mock scores pass and 0.30 fails).
        _reset_prefs()
        self.assertEqual(_PREFS_STATE["data"]["job_fit_threshold"], 0.6)
        # Truncate + reseed jobs DB to the canonical 6 fixtures so
        # pre-test counts are deterministic.
        _run(_seed_job_rows(AsyncSessionLocal()))
        self.client = TestClient(app)

    def tearDown(self) -> None:
        # Wipe the jobs table to avoid bleed into the next test.
        _run(_truncate_jobs_table())

    def test_funding_scan_persists_winners_only_and_keeps_envelope(self) -> None:
        opportunities = _build_mock_opportunities()

        # Pre-state: none of the mock URLs are in the jobs DB.
        for url in SCAN_URLS:
            self.assertEqual(_run(_count_jobs_for_url(url)), 0)

        # Patch the scanner entry AND the LLM scoring entry so we
        # never hit real APIs and our scoring is deterministic.
        with patch("routes.scanner.scan_funding") as mock_scan, \
             patch("services.scoring_service.LLMClient.from_env") as mock_llm:
            mock_scan.return_value = opportunities

            fake_client = AsyncMock()
            fake_client.score_opportunity = AsyncMock(
                side_effect=_score_side_effect
            )
            mock_llm.return_value = fake_client

            # POST the canonical scan; defaults for delta_hours and
            # limit match ``run_funding``'s Query() defaults.
            response = self.client.post("/api/scan/funding")

        # ------------------------------------------------------------------
        # Response envelope is unchanged.
        # ------------------------------------------------------------------
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["message"], "True")
        self.assertEqual(body["domain"], "funding")
        # ``count`` reflects the raw scan output (3); scoring does
        # NOT mutate this — the user still sees their full list.
        self.assertEqual(body["count"], 3)
        self.assertEqual(len(body["opportunities"]), 3)
        urls_in_resp = {item.get("url") for item in body["opportunities"]}
        self.assertEqual(urls_in_resp, set(SCAN_URLS))

        # ------------------------------------------------------------------
        # Scoring was called exactly once per opportunity.
        # ------------------------------------------------------------------
        self.assertEqual(fake_client.score_opportunity.await_count, 3)
        mock_scan.assert_called_once()

        # ------------------------------------------------------------------
        # Above-threshold winners landed in the DB.
        # ------------------------------------------------------------------
        self.assertEqual(_run(_count_jobs_for_url("scan-201-above")), 1)
        self.assertEqual(_run(_count_jobs_for_url("scan-202-verygood")), 1)

        # Inspect one winner's fields end-to-end — verifies the Job
        # schema is the same the React ``JobsReview`` page expects.
        winner = _run(_fetch_winner("scan-201-above"))
        self.assertIsNotNone(winner)
        self.assertEqual(winner.status, "in_review")
        self.assertEqual(winner.ats_type, "funding")
        self.assertEqual(winner.title, "Funding Round — Series B for AI Infra Co")
        self.assertEqual(winner.company_name, "Acme AI")
        self.assertAlmostEqual(winner.ai_fit_score, 0.85, places=4)
        self.assertEqual(winner.ai_fit_reasoning, "above threshold; solid AI-platform match")
        # review_deadline starts as None (scheduler populates later).
        self.assertIsNone(winner.review_deadline)

        # ------------------------------------------------------------------
        # Below-threshold loser was silently dropped — no DB row.
        # ------------------------------------------------------------------
        self.assertEqual(_run(_count_jobs_for_url("scan-203-below")), 0)


# ---------------------------------------------------------------------
class TestScanOssEndToEnd(unittest.TestCase):
    """Same flow for ``/api/scan/oss`` — exercises a different scanner
    entry to prove the wiring is not funding-specific.

    A regression that breaks oss / ngos / remote while leaving
    funding green would go uncaught without this — each domain
    endpoint has its own _score_then_ok call, so each one is an
    independent failure surface.
    """

    def setUp(self) -> None:
        _reset_prefs()
        _run(_seed_job_rows(AsyncSessionLocal()))
        self.client = TestClient(app)

    def tearDown(self) -> None:
        _run(_truncate_jobs_table())

    def test_oss_scan_persists_winners_only(self) -> None:
        opportunities = [
            {
                "title": "OSS opportunity",
                "company_name": "OSS Co 1",
                "url": "scan-oss-above",
                "description": "OSS that matches.",
                "category": "oss",
                "source": "github",
            },
            {
                "title": "OSS opportunity 2",
                "company_name": "OSS Co 2",
                "url": "scan-oss-below",
                "description": "Nope.",
                "category": "oss",
                "source": "github",
            },
        ]

        with patch("routes.scanner.scan_oss") as mock_scan, \
             patch("services.scoring_service.LLMClient.from_env") as mock_llm:
            mock_scan.return_value = opportunities
            fake_client = AsyncMock()
            fake_client.score_opportunity = AsyncMock(
                side_effect=_score_side_effect_oss
            )
            mock_llm.return_value = fake_client

            response = self.client.post("/api/scan/oss")

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["domain"], "oss")
        self.assertEqual(body["count"], 2)

        # Exactly one winner.
        self.assertEqual(_run(_count_jobs_for_url("scan-oss-above")), 1)
        self.assertEqual(_run(_count_jobs_for_url("scan-oss-below")), 0)


# ---------------------------------------------------------------------
# Boards-test fixtures — slugs the boards runner will report plus the
# canned opportunity each ``execute_fetch`` call returns for that slug.
# Three slugs ⇒ three jobs ⇒ two above + one below, matching the
# funding/oss fixtures' shape so the same scoring-service assertion
# pattern works.
# ---------------------------------------------------------------------
_BOARD_OPPS_BY_SLUG: dict[str, dict[str, object]] = {
    "fake-acme": {
        "title": "Senior AI Engineer",
        "company_name": "Acme AI",
        "url": "scan-board-301-above",
        "description": "AI platform role — strong profile match.",
        "source": "ashby",
        "external_id": "ashby:acme",
        "ats_type": "ashby",
        "category": "boards",
        "posted_at": "2026-01-01T00:00:00Z",
    },
    "fake-beta": {
        "title": "Founding AI Engineer",
        "company_name": "Stealth Labs",
        "url": "scan-board-302-verygood",
        "description": "Founding seat on an LLM observability team.",
        "source": "greenhouse",
        "external_id": "greenhouse:stealth",
        "ats_type": "greenhouse",
        "category": "boards",
        "posted_at": "2026-01-01T01:00:00Z",
    },
    "fake-gamma": {
        "title": "Junior Backend Engineer",
        "company_name": "Local Co",
        "url": "scan-board-303-below",
        "description": "(far below profile match — kept as a deliberate loser)",
        "source": "lever",
        "external_id": "lever:local",
        "ats_type": "lever",
        "category": "boards",
        "posted_at": "2026-01-01T02:00:00Z",
    },
}


def _boards_fetch_side_effect(fetcher, board_name, slug, since, seen_ids, client):
    """Build the canned ``execute_fetch`` result for a given slug.

    The boards runner invokes :func:`executor.submit` with six
    positional arguments — ``(fetcher, board_name, slug, since,
    seen_ids, client)`` — so the contract placed on this ``side_effect``
    MUST mirror that order: the first parameter here is the fetcher
    callable, the second is the board name, and only the third is the
    slug. Anything that drops a leading param (`(board_name, slug, …)`)
    would silently bind ``fetcher`` to ``board_name`` and raise
    ``KeyError: '<board name>'`` inside the side effect body.

    Returns the dict shape :func:`pipeline.nodes.jobs_boards.runner.execute_fetch`
    produces — ``outcome == "ok"`` with one job and a populated
    ``new_ids`` map, so the runner's ``results.extend(r["jobs"])`` AND
    ``seen[jid] = stamp`` branches both fire exactly as they would
    against a real ATS response.
    """
    opp = _BOARD_OPPS_BY_SLUG[slug]
    return {
        "board": board_name,
        "slug": slug,
        "outcome": "ok",
        "jobs": [opp],
        "new_ids": {opp["url"]: "2026-01-01T00:00:00Z"},
        "latest": "2026-01-01T00:00:00Z",
    }


# ---------------------------------------------------------------------
class TestScanBoardsEndToEnd(unittest.TestCase):
    """Verify the *boards-runner scoring path* end-to-end.

    Compared to the funding/oss tests, this one is harder: the boards
    path goes through :func:`pipeline.nodes.jobs_boards.runner.run_all`,
    which touches real on-disk state and real ATS networks. To still
    exercise the FULL chain
    (``run_boards`` → ``run_all`` → ``filter_roles`` → ``score_and_persist``
    → ``INSERT``) the test has to mock every external surface the runner
    consults.

    Why those ten :mod:`pipeline.nodes.jobs_boards.runner` patches?

    * ``load_file`` / ``save_seen`` — the ``utils.seen`` on-disk dedupe
      file. The user's spec was that this file must NOT be touched; the
      pair of ``MagicMock`` patches below replaces the real ``open()``
      with a no-op so the file's mtime is unchanged across the test.
    * ``load_orgs`` — production reads ``data/<board>_companies.json``
      (the user's actual company list), which would make the test
      fragile to config changes. Mocking it returns the same canned
      slugs no matter which board is requested.
    * ``load_last_run_state`` / ``save_last_run_state`` — additional
      ``data/last_run.json`` reads/writes the runner schedules.
    * ``load_failure_counts`` / ``save_failure_counts`` —
      ``data/missing_failures.json`` reads/writes for the bench logic.
    * ``_write_missing_lists`` — writes
      ``data/<board>_missing_orgs.json`` once a slug trips the
      ``MISSING_THRESHOLD``. We don't simulate missing in this test so
      the function would never fire, but we still mock it to keep the
      test hermetic — a future test that DOES simulate a 404 might
      regress silently if ``_write_missing_lists`` is left real.
    * ``filter_roles`` — the title-keyword filter at the end of
      ``run_all``. Standing in as identity so our three canned slug
      opportunities all reach ``score_and_persist`` regardless of how
      the keyword filter is calibrated.
    * ``execute_fetch`` — the per-slug wrapper that calls a real ATS
      fetcher. We patch it wholesale so the thread-pool loop ends with
      a known job list; ``side_effect`` keys on ``slug`` so each
      fetch returns its own canned opportunity.

    Plus :mod:`services.scoring_service.LLMClient.from_env` mock —
    same one used by the funding/oss tests, keyed on the URL via a
    module-level ``SCAN_URLS_BOARDS`` table.

    Gaps the test pins down:

    * The boards response envelope — ``{message, domain, delta_hours,
      boards, limit, opportunities, count}`` — is structurally different
      from funding/oss (``sources`` key) and easy to break by a
      careless refactor.
    * The boards route's ``UnknownBoardError → 400`` path (not exercised
      here; ``boards=["ashby"]`` is valid).
    * ``score_and_persist(jobs, "boards")`` is actually called with
      ``ats_type="boards"``; the persisted jobs carry ``ats_type =
      "boards"`` so future ops can filter by domain. This catches a
      refactor that hard-codes ats_type from a different scanner.
    * ``load_orgs`` is called *exactly once* per board (slug list scope)
      — guards against the runner accidentally iterating twice.
    """

    def setUp(self) -> None:
        _reset_prefs()
        _run(_seed_job_rows(AsyncSessionLocal()))
        self.client = TestClient(app)

    def tearDown(self) -> None:
        _run(_truncate_jobs_table())

    def test_boards_scan_persists_winners_only_and_keeps_envelope(self) -> None:
        with patch(
            "pipeline.nodes.jobs_boards.runner.load_file", return_value={}
        ) as mock_seen_load, \
             patch(
                 "pipeline.nodes.jobs_boards.runner.save_seen"
             ) as mock_seen_save, \
             patch(
                 "pipeline.nodes.jobs_boards.runner.load_orgs",
                 return_value=["fake-acme", "fake-beta", "fake-gamma"],
             ) as mock_load_orgs, \
             patch(
                 "pipeline.nodes.jobs_boards.runner.load_failure_counts",
                 return_value={},
             ) as mock_load_failures, \
             patch(
                 "pipeline.nodes.jobs_boards.runner.save_failure_counts"
             ) as mock_save_failures, \
             patch(
                 "pipeline.nodes.jobs_boards.runner.load_last_run_state",
                 return_value={},
             ) as mock_load_last_run, \
             patch(
                 "pipeline.nodes.jobs_boards.runner.save_last_run_state"
             ) as mock_save_last_run, \
             patch(
                 "pipeline.nodes.jobs_boards.runner._write_missing_lists"
             ) as mock_write_missing, \
             patch(
                 "pipeline.nodes.jobs_boards.runner.filter_roles",
                 side_effect=lambda jobs: jobs,
             ) as mock_filter_roles, \
             patch(
                 "pipeline.nodes.jobs_boards.runner.execute_fetch"
             ) as mock_execute_fetch, \
             patch(
                 "services.scoring_service.LLMClient.from_env"
             ) as mock_llm:

            mock_execute_fetch.side_effect = _boards_fetch_side_effect

            fake_client = AsyncMock()
            fake_client.score_opportunity = AsyncMock(
                side_effect=_score_side_effect_boards
            )
            mock_llm.return_value = fake_client

            response = self.client.post(
                "/api/scan/boards?boards=ashby&limit=3&delta_hours=1"
            )

        # ------------------------------------------------------------------
        # Response envelope is the boards-specific shape (different from
        # funding/oss which use ``sources`` instead of ``boards``).
        # ------------------------------------------------------------------
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["message"], "True")
        self.assertEqual(body["domain"], "boards")
        self.assertEqual(body["delta_hours"], 1)
        self.assertEqual(body["boards"], ["ashby"])
        self.assertEqual(body["limit"], 3)
        self.assertEqual(body["count"], 3)
        self.assertEqual(len(body["opportunities"]), 3)
        urls_in_resp = {item.get("url") for item in body["opportunities"]}
        self.assertEqual(urls_in_resp, set(SCAN_URLS_BOARDS))

        # ------------------------------------------------------------------
        # Boards-runner wiring: the right number of fetch calls in the
        # right scope, with the right filter chain at the end.
        # ------------------------------------------------------------------
        self.assertEqual(mock_execute_fetch.call_count, 3)
        # The boards runner calls execute_fetch positionally as
        # ``(fetcher, board_name, slug, since, seen_ids, client)``, so
        # the *slug* is at ``call.args[2]``. The earlier
        # ``call.args[1]`` extraction silently read the board name and
        # would mask routing regressions where the wrong slug list is
        # passed to the inner thread-pool loop.
        slugs_fetched = {
            call.args[2] for call in mock_execute_fetch.call_args_list
        }
        self.assertEqual(slugs_fetched, {"fake-acme", "fake-beta", "fake-gamma"})
        mock_load_orgs.assert_called_once_with("ashby")
        # Verify ``filter_roles`` was called with the merged jobs
        # list — guards a regression where the runner forgets to
        # ``results.extend(r["jobs"])`` and feeds an empty list to
        # the role filter despite the fetches having succeeded.
        filter_call_args = mock_filter_roles.call_args.args[0]
        self.assertEqual(len(filter_call_args), 3)
        self.assertEqual(
            {opp["url"] for opp in filter_call_args},
            set(SCAN_URLS_BOARDS),
        )

        # ------------------------------------------------------------------
        # Scoring was called exactly once per opportunity.
        # ------------------------------------------------------------------
        self.assertEqual(fake_client.score_opportunity.await_count, 3)

        # ------------------------------------------------------------------
        # Above-threshold winners landed in DB; below-threshold loser absent.
        # ------------------------------------------------------------------
        self.assertEqual(_run(_count_jobs_for_url("scan-board-301-above")), 1)
        self.assertEqual(_run(_count_jobs_for_url("scan-board-302-verygood")), 1)
        self.assertEqual(_run(_count_jobs_for_url("scan-board-303-below")), 0)

        winner = _run(_fetch_winner("scan-board-301-above"))
        self.assertIsNotNone(winner)
        # ``ats_type`` must round-trip as ``"boards"`` so future ops can
        # filter winners by their source scanner.
        self.assertEqual(winner.status, "in_review")
        self.assertEqual(winner.ats_type, "boards")
        self.assertEqual(winner.title, "Senior AI Engineer")
        self.assertEqual(winner.company_name, "Acme AI")
        self.assertAlmostEqual(winner.ai_fit_score, 0.81, places=4)
        self.assertEqual(
            winner.ai_fit_reasoning,
            "above threshold; direct AI platform role",
        )

        # ------------------------------------------------------------------
        # No real ``seen.json`` was read or written. The mocks intercepted
        # both the load and the save, so the on-disk dedupe file the user
        # asked us to leave alone is untouched. We also assert the runner
        # actually MERGED each execute_fetch's ``new_ids`` into the
        # in-memory dict before persisting — a slip in the runner's
        # ``seen[job_id] = stamp`` loop would still satisfy
        # ``assert_called_once()`` with an empty seen; this stronger
        # assert catches that regression.
        # ------------------------------------------------------------------
        mock_seen_load.assert_called_once_with()
        mock_seen_save.assert_called_once()
        seen_passed_to_save = mock_seen_save.call_args.args[0]
        expected_seen_urls = {
            opp["url"] for opp in _BOARD_OPPS_BY_SLUG.values()
        }
        self.assertEqual(
            set(seen_passed_to_save.keys()),
            expected_seen_urls,
        )


if __name__ == "__main__":
    unittest.main()
