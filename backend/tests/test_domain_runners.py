"""Unit tests for the per-domain scanner runners (Funding, NGOs, Remote, OSS).

The runners look up source functions in their own module's globals:
``funding.runner.scan_funding`` references ``ph_scan`` (set via the
``from .producthunt import scan as ph_scan`` import). So to stub the
network without hitting the real ProductHunt / ReliefWeb / HN endpoints,
we patch the alias *in the runner module's namespace*.

Tests clean up in ``tearDown`` so subsequent test classes see the real
source functions again.
"""
import unittest
from datetime import datetime, timedelta, timezone

from pipeline.nodes.funding import runner as funding_runner
from pipeline.nodes.funding.runner import scan_funding
from pipeline.nodes.ngos import runner as ngos_runner
from pipeline.nodes.ngos.runner import scan_ngos
from pipeline.nodes.oss import runner as oss_runner
from pipeline.nodes.oss.runner import scan_oss
from pipeline.nodes.remote import runner as remote_runner
from pipeline.nodes.remote.runner import scan_remote

# Runner-local aliases — these are the bound global names the runners actually call.
RUNNER_ALIASES = {
    "ph_scan": (funding_runner, "ph_scan"),
    "sg_scan": (funding_runner, "sg_scan"),
    "ngo_scan": (ngos_runner, "ngo_scan"),
    "hn_scan": (remote_runner, "hn_scan"),
    "remotive_scan": (remote_runner, "remotive_scan"),
    "remoteok_scan": (remote_runner, "remoteok_scan"),
    "github_trending_scan": (oss_runner, "github_trending_scan"),
}


REQUIRED_FIELDS = {"id", "source", "category", "title", "organization", "url", "published"}


def _opp(source: str, category: str, *, hours_ago: float = 1.0, title: str | None = None, url: str | None = None):
    ts = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    title = title or f"{source} opp"
    url = url or f"https://example.com/{source}/{title}"
    return {
        "id": f"{source}:{url}",
        "source": source,
        "category": category,
        "title": title,
        "organization": "TestOrg",
        "url": url,
        "location": "Remote",
        "tags": [category],
        "description": "",
        "published": ts.isoformat(),
        "salary": None,
        "status": "review",
        "score": 0.5,
    }


def _filter_by_source(opportunities, source):
    return [o for o in opportunities if o["source"] == source]


class DomainRunnerMixIn:
    """Patches the runner module's local alias, then restores in tearDown."""

    def setUp(self) -> None:
        self._patches: list[tuple[object, str, object]] = []

    def tearDown(self) -> None:
        for module, attr, original in self._patches:
            setattr(module, attr, original)

    def _stub(self, alias_name: str, replacement):
        module, attr = RUNNER_ALIASES[alias_name]
        self._patches.append((module, attr, getattr(module, attr)))
        setattr(module, attr, replacement)


class ShapeTests(DomainRunnerMixIn, unittest.TestCase):
    """Every domain must emit rows with the standardized opportunity shape."""

    def _stub_all(self):
        self._stub("ph_scan", lambda limit=10: [_opp("producthunt", "funding")])
        self._stub("sg_scan", lambda limit=10: [_opp("startupsgallery", "funding")])
        self._stub("ngo_scan", lambda limit=10, sources=None: [_opp("reliefweb", "ngo")])
        self._stub("hn_scan", lambda limit=10: [_opp("hackernews", "remote")])
        self._stub("remotive_scan", lambda limit=10: [_opp("remotive", "remote")])
        self._stub("remoteok_scan", lambda limit=10: [_opp("remoteok", "remote")])
        self._stub("github_trending_scan", lambda limit=10, language="python": [_opp("github", "oss")])

    def _assert_shape(self, rows):
        self.assertIsInstance(rows, list)
        self.assertGreater(len(rows), 0, "expected at least one stubbed row")
        for row in rows:
            missing = REQUIRED_FIELDS - set(row.keys())
            self.assertFalse(missing, f"opportunity missing fields: {missing} ({row})")

    def test_funding_shape(self):
        self._stub_all()
        self._assert_shape(scan_funding(delta_hours=168))

    def test_ngos_shape(self):
        self._stub_all()
        self._assert_shape(scan_ngos(delta_hours=168))

    def test_remote_shape(self):
        self._stub_all()
        self._assert_shape(scan_remote(delta_hours=168))

    def test_oss_shape(self):
        self._stub_all()
        self._assert_shape(scan_oss(delta_hours=168))


class FundingFilterTests(DomainRunnerMixIn, unittest.TestCase):
    def test_keeps_recent_and_drops_old(self):
        opp_recent_ph = _opp("producthunt", "funding", hours_ago=2)
        opp_old_ph = _opp("producthunt", "funding", hours_ago=72)
        opp_recent_sg = _opp("startupsgallery", "funding", hours_ago=1)
        only_ph = [opp_recent_ph, opp_old_ph]
        only_sg = [opp_recent_sg]

        self._stub("ph_scan", lambda limit=10: only_ph)
        self._stub("sg_scan", lambda limit=10: only_sg)

        out = scan_funding(delta_hours=24, limit=50)
        self.assertEqual(len(out), 2, f"expected 2 in 24h window, got {len(out)}: {out}")
        urls = {o["url"] for o in out}
        self.assertIn(opp_recent_ph["url"], urls)
        self.assertIn(opp_recent_sg["url"], urls)
        self.assertNotIn(opp_old_ph["url"], urls)


class RemoteFilterTests(DomainRunnerMixIn, unittest.TestCase):
    def test_keeps_recent_and_drops_old(self):
        opp_recent_remotive = _opp("remotive", "remote", hours_ago=2)
        opp_old_remotive = _opp("remotive", "remote", hours_ago=200)
        opp_recent_hn = _opp("hackernews", "remote", hours_ago=1)
        self._stub("remotive_scan", lambda limit=10: [opp_recent_remotive, opp_old_remotive])
        self._stub("hn_scan", lambda limit=10: [opp_recent_hn])
        self._stub("remoteok_scan", lambda limit=10: [])

        out = scan_remote(delta_hours=24, limit=50)
        urls = {o["url"] for o in out}
        self.assertIn(opp_recent_remotive["url"], urls)
        self.assertIn(opp_recent_hn["url"], urls)
        self.assertNotIn(opp_old_remotive["url"], urls)


class NgoFilterTests(DomainRunnerMixIn, unittest.TestCase):
    def test_drops_old_listings(self):
        recent = _opp("reliefweb", "ngo", hours_ago=5)
        old = _opp("reliefweb", "ngo", hours_ago=120)
        self._stub("ngo_scan", lambda limit=10, sources=None: [recent, old])

        out = scan_ngos(delta_hours=72, limit=50)
        urls = {o["url"] for o in out}
        self.assertIn(recent["url"], urls)
        self.assertNotIn(old["url"], urls)


class OssFilterTests(DomainRunnerMixIn, unittest.TestCase):
    def test_unknown_published_passes_by_default(self):
        # GitHub trending has no per-repo timestamps; unknown-age rows must pass.
        opp = _opp("github", "oss", hours_ago=0)
        opp["published"] = None
        self._stub("github_trending_scan", lambda limit=10, language="python": [opp])

        out = scan_oss(delta_hours=24, limit=10)
        self.assertEqual(len(out), 1)


if __name__ == "__main__":
    unittest.main()
