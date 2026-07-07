"""Unit tests for the Open Source domain — trending scraper, good-first-issues
scraper, strategy generator, and runner combination logic.

Network I/O is stubbed by patching the runner module's local aliases
(``trending_scan`` / ``gfi_scan``) — that's where the runner's
``from .github_trending import scan as trending_scan`` bindings actually
resolve at call time. Patching the source module's ``scan`` attribute
does NOT reach the runner's local binding.
"""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from pipeline.nodes.oss.runner import scan_oss
from pipeline.nodes.oss.strategy import (
    attach_strategy,
    build_outreach,
    build_strategy,
    classify_difficulty,
)
from pipeline.nodes.oss import github_trending, github_issues


# ---------------------------------------------------------------------------
# Strategy generator — pure functions, easy to assert against.
# ---------------------------------------------------------------------------
class DifficultyClassificationTests(unittest.TestCase):
    def _row(self, **overrides):
        row = {
            "title": "owner/repo",
            "organization": "owner",
            "url": "https://github.com/owner/repo",
            "description": "",
            "tags": ["oss", "github", "python"],
            "source": "github",
            "category": "oss",
            "published": datetime.now(timezone.utc).isoformat(),
            "status": "review",
            "score": 0.5,
            "stars": 0,
            "forks": 0,
            "primary_language": "python",
            "top_issues": [],
            "last_activity": datetime.now(timezone.utc).isoformat(),
        }
        row.update(overrides)
        return row

    def test_low_stars_plus_gfi_is_easy(self):
        self.assertEqual(classify_difficulty(self._row(stars=2_000, top_issues=[{"number": 1}])), "easy")

    def test_high_stars_is_hard(self):
        self.assertEqual(classify_difficulty(self._row(stars=80_000, top_issues=[{"number": 1}])), "hard")

    def test_idle_repo_is_hard(self):
        thirteen_months_ago = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
        self.assertEqual(classify_difficulty(self._row(stars=2_000, last_activity=thirteen_months_ago)), "hard")

    def test_mid_range_with_no_gfi_is_medium(self):
        self.assertEqual(classify_difficulty(self._row(stars=12_000, top_issues=[])), "medium")

    def test_low_stars_without_gfi_is_medium(self):
        self.assertEqual(classify_difficulty(self._row(stars=2_000, top_issues=[])), "medium")


class StrategyTemplatesTests(unittest.TestCase):
    def test_gfi_template_lists_issue_numbers(self):
        opp = {
            "title": "owner/repo",
            "primary_language": "python",
            "top_issues": [
                {"number": 12, "title": "foo"},
                {"number": 17, "title": "bar"},
                {"number": 22, "title": "baz"},
            ],
        }
        text = build_strategy(opp, has_gfi=True)
        self.assertIn("#12", text)
        self.assertIn("#17", text)
        self.assertIn("#22", text)
        self.assertIn("fix/issue-<n>", text)
        self.assertIn("owner/repo", text)

    def test_no_gfi_template_uses_generic_steps(self):
        opp = {"title": "owner/repo", "primary_language": "go"}
        text = build_strategy(opp, has_gfi=False)
        self.assertIn("CONTRIBUTING.md", text)
        self.assertIn("owner/repo", text)
        # Falls back to default runtime hint for the requested language.
        self.assertIn("Go", text)

    def test_outreach_mentions_first_issue_for_gfi(self):
        opp = {"title": "owner/repo", "top_issues": [{"number": 7, "title": ""}]}
        subject, body = build_outreach(opp, has_gfi=True)
        self.assertIn("issue #7", subject)
        self.assertIn("issue #7", body)
        self.assertIn("@owner", body)

    def test_outreach_without_gfi_asks_for_direction(self):
        subject, body = build_outreach({"title": "owner/repo"}, has_gfi=False)
        self.assertIn("Contribution inquiry: owner/repo", subject)
        self.assertIn("beginner-friendly", body)

    def test_attach_strategy_does_not_mutate_caller_opp(self):
        opp = {
            "title": "owner/repo",
            "organization": "owner",
            "url": "https://github.com/owner/repo",
            "source": "github",
            "category": "oss",
            "stars": 1_000,
            "primary_language": "python",
            "top_issues": [{"number": 1, "title": "x"}],
            "last_activity": datetime.now(timezone.utc).isoformat(),
            "score": 0.5,
        }
        snapshot = dict(opp)
        out = attach_strategy(opp)
        # Returned object carries the new fields.
        self.assertEqual(out["difficulty"], "easy")
        self.assertIn("#1", out["reachout_strategy"])
        self.assertEqual(out["organization"], "owner")
        self.assertIn("reachout_subject", out)
        self.assertIn("reachout_body", out)
        # Caller's dict is untouched.
        self.assertEqual(opp, snapshot)


# ---------------------------------------------------------------------------
# bs4 trending parser — feed a static HTML fragment so we don't depend on the
# live page.
# ---------------------------------------------------------------------------
SAMPLE_TRENDING_HTML = """
<html>
  <body>
    <!-- Modern GitHub trending DOM (Box-row class dropped, /forks replaces /network/members). -->
    <article>
      <h2>
        <a href="/python/cpython">python / cpython</a>
      </h2>
      <p>The Python programming language interpreter.</p>
      <div>
        <span itemprop="programmingLanguage">Python</span>
        <a href="/python/cpython/stargazers">62,341</a>
        <a href="/python/cpython/forks">3.2k</a>
      </div>
    </article>
    <!-- Nav-noise articles: single-segment /trending AND 2-segment /sponsors/foo
         — both must be filtered, even though the loose "/" gate would let
         /sponsors/foo through. -->
    <article>
      <h2><a href="/trending">trending</a></h2>
      <p>Explore trending repos.</p>
    </article>
    <article>
      <h2><a href="/sponsors/alirezarezvani">sponsor alirezarezvani</a></h2>
    </article>
    <article>
      <h2>
        <a href="/cli/cli">cli / cli</a>
      </h2>
      <p>GitHub's official command line tool.</p>
      <div>
        <span itemprop="programmingLanguage">Go</span>
        <a href="/cli/cli/stargazers">36,807</a>
        <a href="/cli/cli/forks">3,400</a>
      </div>
    </article>
  </body>
</html>
"""


class TrendingParserTests(unittest.TestCase):
    def test_extracts_owner_repo_description_and_stars(self):
        rows = github_trending._parse_html(SAMPLE_TRENDING_HTML, language="python", limit=25)
        # 2 trending repos; the "/trending" nav-noise article is filtered out.
        self.assertEqual(len(rows), 2)
        first = rows[0]
        self.assertEqual(first["title"], "python/cpython")
        self.assertEqual(first["url"], "https://github.com/python/cpython")
        self.assertEqual(first["primary_language"], "Python")
        self.assertEqual(first["stars"], 62_341)
        # The forks anchor says "3.2k" — the unit-aware parser must keep it as 3200.
        self.assertEqual(first["forks"], 3_200)
        self.assertIn("Python programming", first["description"])

    def test_nav_noise_articles_are_filtered(self):
        rows = github_trending._parse_html(SAMPLE_TRENDING_HTML, language="python", limit=25)
        titles = {r["title"] for r in rows}
        # Single-segment nav (/trending) and 2-segment nav (/sponsors/foo) both
        # rejected by _REPO_PATH_RE; only proper owner/repo rows survive.
        self.assertNotIn("trending", titles)
        self.assertNotIn("sponsors/alirezarezvani", titles)
        self.assertEqual(titles, {"python/cpython", "cli/cli"})

    def test_k_suffix_does_not_fall_back_to_unit_digit(self):
        # Regression for the digit-only parser: "12.4m" must become 12_400_000.
        self.assertEqual(github_trending._parse_star_count("12.4m"), 12_400_000)
        self.assertEqual(github_trending._parse_star_count("3.2k"), 3_200)
        self.assertEqual(github_trending._parse_star_count("62,341"), 62_341)
        self.assertEqual(github_trending._parse_star_count(""), 0)


# ---------------------------------------------------------------------------
# Good-First-Issues API — stub httpx.get so we don't burn the 60/hour budget.
# ---------------------------------------------------------------------------
SAMPLE_GFI_PAYLOAD = {
    "total_count": 2,
    "incomplete_results": False,
    "items": [
        {
            "number": 4,
            "title": "Add a docs page for X",
            "html_url": "https://github.com/owner/repo/issues/4",
            "repository_url": "https://api.github.com/repos/owner/repo",
            "labels": [{"name": "good first issue"}, {"name": "documentation"}],
            "updated_at": "2026-07-05T12:00:00Z",
        },
        {
            "number": 9,
            "title": "Fix flaky test",
            "html_url": "https://github.com/owner/repo/issues/9",
            "repository_url": "https://api.github.com/repos/owner/repo",
            "labels": [{"name": "good first issue"}],
            "updated_at": "2026-07-04T12:00:00Z",
        },
        {
            "number": 12,
            "title": "Add CLI flag",
            "html_url": "https://github.com/another/repo/issues/12",
            "repository_url": "https://api.github.com/repos/another/repo",
            "labels": [{"name": "good first issue"}],
            "updated_at": "2026-07-04T13:00:00Z",
        },
    ],
}


class GoodFirstIssuesTests(unittest.TestCase):
    def setUp(self):
        github_issues._cached_search.cache_clear()

    def test_groups_issues_by_repository(self):
        response = Mock(status_code=200)
        response.json.return_value = SAMPLE_GFI_PAYLOAD
        with patch("pipeline.nodes.oss.github_issues.httpx.get", return_value=response):
            rows = github_issues.scan(limit=10, language="python")

        self.assertEqual(len(rows), 2)  # owner/repo + another/repo

        owner_row = next(r for r in rows if "owner/repo" in r["title"])
        self.assertEqual(len(owner_row["top_issues"]), 2)
        issue_numbers = sorted(i["number"] for i in owner_row["top_issues"])
        self.assertEqual(issue_numbers, [4, 9])
        # Score: the +0.05 bump for GFI rows happens downstream in attach_strategy.
        self.assertEqual(owner_row["score"], 0.8)
        # Description reflects the *real* count across all GFI hits, not the top-3 slice.
        self.assertIn("2 open good-first-issues", owner_row["description"])

    def test_rate_limit_returns_empty(self):
        response = Mock(status_code=403)
        with patch("pipeline.nodes.oss.github_issues.httpx.get", return_value=response):
            rows = github_issues.scan(limit=10, language="python")
        self.assertEqual(rows, [])

    def test_anonymous_request_omits_authorization_header(self):
        # Default (unset env): no Authorization header ⇒ 60/hr limit applies.
        response = Mock(status_code=200)
        response.json.return_value = SAMPLE_GFI_PAYLOAD
        with patch("pipeline.nodes.oss.github_issues.httpx.get", return_value=response), \
             patch.object(github_issues, "_GITHUB_TOKEN", ""):
            # Clear cached answer so the mock fires.
            github_issues._cached_search.cache_clear()
            github_issues.scan(limit=10, language="python")
            headers = github_issues.httpx.get.call_args.kwargs["headers"]
            self.assertNotIn("Authorization", headers)

    def test_authenticated_request_includes_bearer_header(self):
        # With token: Authorization: Bearer <token> is present ⇒ 5,000/hr limit.
        response = Mock(status_code=200)
        response.json.return_value = SAMPLE_GFI_PAYLOAD
        with patch("pipeline.nodes.oss.github_issues.httpx.get", return_value=response), \
             patch.object(github_issues, "_GITHUB_TOKEN", "ghp_testtoken123"):
            github_issues._cached_search.cache_clear()
            github_issues.scan(limit=10, language="python")
            headers = github_issues.httpx.get.call_args.kwargs["headers"]
            self.assertEqual(headers.get("Authorization"), "Bearer ghp_testtoken123")


# ---------------------------------------------------------------------------
# Runner — combines both sources and returns the standardized shape.
# ---------------------------------------------------------------------------
class RunnerCombinationTests(unittest.TestCase):
    def setUp(self):
        github_issues._cached_search.cache_clear()

    def test_trending_row_absorbs_gfi_top_issues_when_same_repo(self):
        trending_row = {
            "id": "github:owner/repo",
            "source": "github",
            "category": "oss",
            "title": "owner/repo",
            "organization": "owner",
            "url": "https://github.com/owner/repo",
            "location": "GitHub",
            "tags": ["oss", "github", "python"],
            "description": "A trending Python project.",
            "published": datetime.now(timezone.utc).isoformat(),
            "salary": None,
            "status": "review",
            "score": 0.7,
            "stars": 1_500,
            "forks": 200,
            "primary_language": "Python",
            "last_activity": datetime.now(timezone.utc).isoformat(),
        }
        gfi_row = {
            "id": "github-issues:owner/repo",
            "source": "github_issues",
            "category": "oss",
            "title": "owner/repo",
            "organization": "owner",
            "url": "https://github.com/owner/repo",
            "location": "GitHub",
            "tags": ["oss", "github", "good-first-issue"],
            "description": "open good-first-issues",
            "published": datetime.now(timezone.utc).isoformat(),
            "salary": None,
            "status": "review",
            "score": 0.8,
            "top_issues": [{"number": 1, "title": "easy win", "url": "https://github.com/owner/repo/issues/1"}],
            "last_activity": datetime.now(timezone.utc).isoformat(),
            "primary_language": "",
        }

        # Patch the runner's local aliases (NOT the source modules) so the stub
        # actually reaches the runner call sites.
        with patch("pipeline.nodes.oss.runner.trending_scan", return_value=[trending_row]), \
             patch("pipeline.nodes.oss.runner.gfi_scan", return_value=[gfi_row]):
            out = scan_oss(delta_hours=24, limit=50, languages=["python"])

        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["title"], "owner/repo")
        self.assertEqual(out[0]["stars"], 1_500)
        self.assertEqual(len(out[0]["top_issues"]), 1)
        self.assertEqual(out[0]["difficulty"], "easy")
        self.assertIn("#1", out[0]["reachout_strategy"])

    def test_gfi_only_repo_included_when_not_in_trending(self):
        gfi_row = {
            "id": "github-issues:other/repo",
            "source": "github_issues",
            "category": "oss",
            "title": "other/repo",
            "organization": "other",
            "url": "https://github.com/other/repo",
            "location": "GitHub",
            "tags": ["oss", "github", "good-first-issue"],
            "description": "open good-first-issue",
            "published": datetime.now(timezone.utc).isoformat(),
            "salary": None,
            "status": "review",
            "score": 0.8,
            "top_issues": [{"number": 1, "title": "starter", "url": "https://github.com/other/repo/issues/1"}],
            "last_activity": datetime.now(timezone.utc).isoformat(),
            "primary_language": "Python",
        }
        with patch("pipeline.nodes.oss.runner.trending_scan", return_value=[]), \
             patch("pipeline.nodes.oss.runner.gfi_scan", return_value=[gfi_row]):
            out = scan_oss(delta_hours=24, limit=50, languages=["python"])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["title"], "other/repo")
        self.assertEqual(out[0]["difficulty"], "easy")


if __name__ == "__main__":
    unittest.main()
