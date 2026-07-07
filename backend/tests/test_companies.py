"""Unit tests for the ``/api/companies`` router.

Drives the route end-to-end via FastAPI ``TestClient`` and resets the
in-memory ``_COMPANIES_DB`` from the canonical ``_SEED_RECORDS`` between
cases via the module's ``_seed()`` helper. Cross-test isolation is
critical because PATCH mutates the dict in place.
"""
import unittest

from fastapi.testclient import TestClient

from main import app
from routes.companies import _COMPANIES_DB, _seed


# --------------------------------------------------------------------------
# Helpers — every test class's ``setUp`` calls ``_seed()`` so PATCH
# mutations from earlier tests do not leak into later assertions.
# --------------------------------------------------------------------------
class _CompaniesTestCase(unittest.TestCase):
    def setUp(self):
        _seed()
        self.client = TestClient(app)


class TestListCompanies(_CompaniesTestCase):
    def test_list_returns_all_seeded_records(self):
        r = self.client.get("/api/companies")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["total"], 6)
        self.assertEqual(body["count"], 6)
        self.assertEqual(len(body["companies"]), 6)

    def test_filter_by_category_boards(self):
        r = self.client.get("/api/companies", params={"category": "boards"})
        body = r.json()
        self.assertEqual(body["total"], 2)  # c_1 (Vercel) + c_6 (Cloudflare)
        ids = {c["id"] for c in body["companies"]}
        self.assertEqual(ids, {"c_1", "c_6"})
        for c in body["companies"]:
            self.assertEqual(c["category"], "boards")

    def test_filter_by_status_alias_param(self):
        """React side passes ``?status=interested``; the route aliasing
        keeps the public query key ``status`` distinct from the Python
        ``status`` builtin."""
        r = self.client.get("/api/companies", params={"status": "interested"})
        body = r.json()
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["companies"][0]["id"], "c_1")

    def test_filter_by_source_idealist(self):
        r = self.client.get("/api/companies", params={"source": "idealist"})
        body = r.json()
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["companies"][0]["id"], "c_5")

    def test_search_substring_matches_title_or_organization(self):
        r = self.client.get("/api/companies", params={"search": "anthropic"})
        body = r.json()
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["companies"][0]["organization"], "Anthropic")

        r = self.client.get("/api/companies", params={"search": "engineer"})
        body = r.json()
        # c_1 "Senior Frontend Engineer", c_5 "Open Source Engineer — ...", c_6 "Distributed Systems Engineer"
        self.assertEqual(body["total"], 3)
        ids = {c["id"] for c in body["companies"]}
        self.assertEqual(ids, {"c_1", "c_5", "c_6"})

    def test_search_substring_matches_tag(self):
        """Tag-only matches — ``?search=nextjs`` lands via c_1's tag of
        the same name. The substring does not appear in c_1's title,
        organization, or description, so this test genuinely pins the
        tag-search code path (a regression that drops the
        ``or any(... tags ...)`` branch will fail this assertion).
        """
        r = self.client.get("/api/companies", params={"search": "nextjs"})
        body = r.json()
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["companies"][0]["id"], "c_1")

    def test_search_substring_matches_description_only(self):
        """``?search=nowcasting`` appears only in c_5's description text —
        pins the description-search path so it can't regress silently."""
        r = self.client.get("/api/companies", params={"search": "nowcasting"})
        body = r.json()
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["companies"][0]["id"], "c_5")

    def test_search_composes_with_category_filter(self):
        """Filters compose — ``?search=engineer&category=boards`` matches c_1 + c_6
        but NOT c_5 (which has "Engineer" in title but ``category="ngos"``)."""
        r = self.client.get(
            "/api/companies",
            params={"search": "engineer", "category": "boards"},
        )
        body = r.json()
        ids = {c["id"] for c in body["companies"]}
        self.assertEqual(ids, {"c_1", "c_6"})

    def test_pagination_limit_offset_envelope(self):
        r = self.client.get("/api/companies", params={"limit": 2, "offset": 1})
        body = r.json()
        self.assertEqual(body["total"], 6)  # total reflects ALL matches post-filter
        self.assertEqual(body["count"], 2)  # count reflects page length
        self.assertEqual(len(body["companies"]), 2)


class TestStats(_CompaniesTestCase):
    def test_stats_total_matches_seeded_count(self):
        r = self.client.get("/api/companies/stats")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["total"], 6)

    def test_stats_by_status_matches_individual_records(self):
        body = self.client.get("/api/companies/stats").json()
        # c_1 interested, c_2 saved, c_3 engaged, c_4 outreach_sent,
        # c_5 saved, c_6 dismissed.
        self.assertEqual(body["by_status"]["saved"], 2)
        self.assertEqual(body["by_status"]["interested"], 1)
        self.assertEqual(body["by_status"]["engaged"], 1)
        self.assertEqual(body["by_status"]["outreach_sent"], 1)
        self.assertEqual(body["by_status"]["dismissed"], 1)

    def test_stats_by_category_matches_seeded(self):
        body = self.client.get("/api/companies/stats").json()
        # boards×2 (c_1, c_6), oss×1, remote×1, funding×1, ngos×1.
        self.assertEqual(body["by_category"], {
            "boards": 2, "oss": 1, "remote": 1, "funding": 1, "ngos": 1,
        })

    def test_stats_by_source_matches_seeded(self):
        body = self.client.get("/api/companies/stats").json()
        self.assertEqual(body["by_source"], {
            "ashby": 1, "github_issues": 1, "remotive": 1,
            "producthunt": 1, "idealist": 1, "greenhouse": 1,
        })


class TestGetById(_CompaniesTestCase):
    def test_get_known_company_returns_full_record(self):
        r = self.client.get("/api/companies/c_1")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["id"], "c_1")
        self.assertEqual(body["organization"], "Vercel")
        self.assertEqual(body["status"], "interested")
        # OutreachPanel reads these:
        self.assertIsNotNone(body["company_summary"])
        self.assertEqual(len(body["hiring_signals"]), 2)

    def test_get_unknown_company_returns_404(self):
        r = self.client.get("/api/companies/never-seen")
        self.assertEqual(r.status_code, 404)
        self.assertIn("not found", r.json()["detail"].lower())


class TestPatchStatus(_CompaniesTestCase):
    def test_patch_status_uses_react_wire_shape_and_bumps_updated_at(self):
        # Mirror what ``updateCompanyStatus`` in client.js sends.
        before = self.client.get("/api/companies/c_2").json()
        self.assertEqual(before["status"], "saved")
        before_updated_at = before["updated_at"]

        r = self.client.patch(
            "/api/companies/c_2/status", json={"status": "interested"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        # Full company back so the React cache update doesn't need a refetch.
        self.assertEqual(body["id"], "c_2")
        self.assertEqual(body["status"], "interested")
        self.assertNotEqual(body["updated_at"], before_updated_at)

        # And the change persisted in the live dict.
        live = self.client.get("/api/companies/c_2").json()
        self.assertEqual(live["status"], "interested")

    def test_patch_status_can_revert_to_default_saved(self):
        # First move away from saved...
        self.client.patch(
            "/api/companies/c_2/status", json={"status": "dismissed"},
        )
        # Then revert — the Literal must accept "saved" too.
        r = self.client.patch(
            "/api/companies/c_2/status", json={"status": "saved"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "saved")

    def test_patch_invalid_status_returns_422(self):
        # "ghosted" is the ApplicationTracker status enum, NOT a Company
        # status — Pydantic Literal validation surfaces this.
        r = self.client.patch(
            "/api/companies/c_1/status", json={"status": "ghosted"},
        )
        self.assertEqual(r.status_code, 422)

    def test_patch_unknown_company_returns_404(self):
        r = self.client.patch(
            "/api/companies/nope/status", json={"status": "interested"},
        )
        self.assertEqual(r.status_code, 404)

    def test_patch_empty_body_returns_422(self):
        r = self.client.patch("/api/companies/c_1/status", json={})
        self.assertEqual(r.status_code, 422)


class TestRouterMount(_CompaniesTestCase):
    def test_openapi_lists_all_four_routes(self):
        spec = app.openapi()
        # The list-collection endpoints may register with trailing slash or
        # not; match on prefix substring to be tolerant of FastAPI's
        # normalisation.
        companies_paths = [p for p in spec["paths"].keys() if p.startswith("/api/companies")]
        self.assertGreaterEqual(len(companies_paths), 3)
        self.assertTrue(
            any(p.endswith("/stats") for p in companies_paths),
            f"missing /stats in {companies_paths}",
        )
        self.assertTrue(
            any("{company_id}" in p for p in companies_paths),
            f"missing /{{company_id}} in {companies_paths}",
        )
        self.assertTrue(
            any("{company_id}/status" in p for p in companies_paths),
            f"missing /{{company_id}}/status in {companies_paths}",
        )

    def test_get_stats_takes_precedence_over_get_by_id_pattern(self):
        """``/api/companies/stats`` returns the aggregate envelope, NOT a
        single-company record with ``company_id='stats'``."""
        r = self.client.get("/api/companies/stats")
        body = r.json()
        self.assertIn("by_status", body)
        self.assertNotIn("title", body)


if __name__ == "__main__":
    unittest.main()
