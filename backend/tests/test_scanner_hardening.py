"""Regression tests for the production-hardening fixes to the job-board scanners."""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import httpx

from pipeline.nodes.jobs_boards import ashby, greenhouse, lever
from pipeline.nodes.jobs_boards.runner import UnknownBoardError, execute_fetch, validate_boards
from utils.http import get_json
from utils.seen import _prune, is_new_job


def make_response(status, json_value=None, raises=False, headers=None):
    response = Mock()
    response.status_code = status
    response.request = Mock()
    response.headers = headers or {}
    response.raise_for_status = Mock()
    if raises:
        response.json.side_effect = ValueError("not json")
    else:
        response.json.return_value = json_value
    return response


class FakeClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def get(self, url):
        response = self._responses[self.calls]
        self.calls += 1
        return response


class BoardValidationTests(unittest.TestCase):
    def test_unknown_board_raises(self):
        with self.assertRaises(UnknownBoardError):
            validate_boards(["ashby", "workday"])

    def test_known_boards_pass_through(self):
        self.assertEqual(validate_boards(["ashby", "lever"]), ["ashby", "lever"])


class GetJsonRetryTests(unittest.TestCase):
    def test_retries_on_429_then_succeeds(self):
        client = FakeClient([make_response(429), make_response(429), make_response(200, {"jobs": []})])
        slept = []
        result = get_json(client, "http://x", sleep=slept.append)
        self.assertEqual(result, {"jobs": []})
        self.assertEqual(client.calls, 3)
        self.assertEqual(slept, [1.0, 2.0])  # exponential backoff

    def test_respects_retry_after_header(self):
        client = FakeClient([make_response(429, headers={"retry-after": "7"}), make_response(200, [])])
        slept = []
        get_json(client, "http://x", sleep=slept.append)
        self.assertEqual(slept, [7.0])

    def test_exhausts_retries_and_raises(self):
        client = FakeClient([make_response(503)] * 5)
        with self.assertRaises(httpx.HTTPStatusError):
            get_json(client, "http://x", max_retries=2, sleep=lambda _: None)

    def test_non_json_200_raises_value_error(self):
        client = FakeClient([make_response(200, raises=True)])
        with self.assertRaises(ValueError):
            get_json(client, "http://x")


class ExecuteFetchClassificationTests(unittest.TestCase):
    def _fetcher_raising(self, status):
        def fetcher(slug, *, client, since, seen_ids):
            response = Mock()
            response.status_code = status
            raise httpx.HTTPStatusError("boom", request=Mock(), response=response)
        return fetcher

    def test_404_is_missing(self):
        out = execute_fetch(self._fetcher_raising(404), "ashby", "x", None, frozenset(), None)
        self.assertEqual(out["outcome"], "missing")

    def test_500_is_transient_error_not_missing(self):
        out = execute_fetch(self._fetcher_raising(500), "ashby", "x", None, frozenset(), None)
        self.assertEqual(out["outcome"], "error")

    def test_ok_passes_result_through(self):
        def fetcher(slug, *, client, since, seen_ids):
            return {"jobs": [{"id": "1"}], "new_ids": {"1": "t"}, "latest": "t"}
        out = execute_fetch(fetcher, "ashby", "x", None, frozenset(), None)
        self.assertEqual(out["outcome"], "ok")
        self.assertEqual(out["jobs"], [{"id": "1"}])


class SeenPruneTests(unittest.TestCase):
    def test_prunes_old_keeps_recent_and_legacy(self):
        now = datetime(2026, 7, 6, tzinfo=timezone.utc)
        seen = {
            "old": (now - timedelta(days=90)).isoformat(),
            "recent": (now - timedelta(days=5)).isoformat(),
            "legacy": None,  # pre-existing id, unknown age -> kept
        }
        pruned = _prune(seen, now=now)
        self.assertEqual(sorted(pruned), ["legacy", "recent"])

    def test_is_new_job_checks_membership(self):
        self.assertFalse(is_new_job("1", {"1": "t"}))
        self.assertTrue(is_new_job("2", {"1": "t"}))


class FetcherRobustnessTests(unittest.TestCase):
    """Malformed job entries are skipped, not crashed on (no more KeyError)."""

    def _client_returning(self, payload):
        return FakeClient([make_response(200, payload)])

    def test_ashby_skips_jobs_missing_id_or_timestamp(self):
        payload = {"jobs": [
            {"title": "no id", "publishedAt": "2026-07-01T00:00:00Z"},   # missing id
            {"id": "1", "title": "no ts"},                                # missing publishedAt
            {"id": "2", "title": "good", "publishedAt": "2026-07-01T00:00:00Z", "jobUrl": "u"},
        ]}
        result = ashby.fetch("org", client=self._client_returning(payload), seen_ids=frozenset())
        self.assertEqual([j["id"] for j in result["jobs"]], ["2"])

    def test_greenhouse_and_lever_skip_malformed(self):
        gh = greenhouse.fetch("org", client=self._client_returning(
            {"jobs": [{"title": "no id/ts"}, {"id": 9, "updated_at": "2026-07-01T00:00:00Z", "title": "ok"}]}
        ), seen_ids=frozenset())
        self.assertEqual([j["id"] for j in gh["jobs"]], ["9"])

        lv = lever.fetch("org", client=self._client_returning(
            [{"text": "no id/ts"}, {"id": "L1", "createdAt": 1751328000000, "text": "ok"}]
        ), seen_ids=frozenset())
        self.assertEqual([j["id"] for j in lv["jobs"]], ["L1"])

    def test_seen_ids_dedupes(self):
        payload = {"jobs": [{"id": "1", "publishedAt": "2026-07-01T00:00:00Z", "title": "t"}]}
        result = ashby.fetch("org", client=self._client_returning(payload), seen_ids=frozenset({"1"}))
        self.assertEqual(result["jobs"], [])


if __name__ == "__main__":
    unittest.main()
