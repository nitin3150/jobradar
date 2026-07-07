import unittest
from unittest.mock import Mock

from pipeline.nodes.jobs_boards.cleanup_missing_orgs import (
    board_api_url,
    choose_target,
    plan_moves,
    response_matches_board,
)


def make_response(status, payload=None, raises=False):
    response = Mock()
    response.status_code = status
    if raises:
        response.json.side_effect = ValueError("not json")
    else:
        response.json.return_value = payload
    return response


class BoardUrlTests(unittest.TestCase):
    def test_urls_target_the_supported_board_apis(self):
        self.assertEqual(
            board_api_url("ashby", "openai"),
            "https://api.ashbyhq.com/posting-api/job-board/openai?includeCompensation=true",
        )
        self.assertEqual(
            board_api_url("greenhouse", "openai"),
            "https://boards-api.greenhouse.io/v1/boards/openai/jobs",
        )
        self.assertEqual(
            board_api_url("lever", "openai"),
            "https://api.lever.co/v0/postings/openai?mode=json",
        )

    def test_unknown_board_raises(self):
        with self.assertRaises(ValueError):
            board_api_url("workday", "openai")


class ResponseShapeTests(unittest.TestCase):
    def test_live_board_with_jobs_matches(self):
        self.assertTrue(response_matches_board("ashby", make_response(200, {"jobs": [{"id": "1"}]})))
        self.assertTrue(response_matches_board("greenhouse", make_response(200, {"jobs": []})))
        self.assertTrue(response_matches_board("lever", make_response(200, [{"id": "1"}])))

    def test_error_status_never_matches(self):
        self.assertFalse(response_matches_board("ashby", make_response(404, {"jobs": []})))
        self.assertFalse(response_matches_board("lever", make_response(500, [])))

    def test_wrong_shape_does_not_match(self):
        # 200 but not the board's documented shape (e.g. a slug collision / error page).
        self.assertFalse(response_matches_board("ashby", make_response(200, {"error": "nope"})))
        self.assertFalse(response_matches_board("lever", make_response(200, {"jobs": []})))
        self.assertFalse(response_matches_board("greenhouse", make_response(200, None, raises=True)))


class TargetSelectionTests(unittest.TestCase):
    def test_priority_prefers_ashby_then_greenhouse_then_lever(self):
        self.assertEqual(choose_target(["lever", "greenhouse", "ashby"]), "ashby")
        self.assertEqual(choose_target(["lever", "greenhouse"]), "greenhouse")
        self.assertEqual(choose_target(["lever"]), "lever")

    def test_no_reachable_board_returns_none(self):
        self.assertIsNone(choose_target([]))


class PlanMovesTests(unittest.TestCase):
    def test_splits_moves_from_careers_only(self):
        probe = {
            "reachable-multi": ["greenhouse", "lever"],
            "reachable-one": ["lever"],
            "careers-only": [],
        }
        moves, careers = plan_moves(probe)
        by_org = {move["org"]: move["target"] for move in moves}
        self.assertEqual(by_org["reachable-multi"], "greenhouse")
        self.assertEqual(by_org["reachable-one"], "lever")
        self.assertEqual(careers, ["careers-only"])


if __name__ == "__main__":
    unittest.main()
