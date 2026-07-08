"""Tests for :mod:`routes.resumes` — exercises the wire shape the React
``ResumesModal`` consumes.

Patterns mirror :mod:`tests.test_companies` (in-memory seeded store,
``_seed()`` resets between tests via deep-copy).
"""
from __future__ import annotations

import io
import unittest

from fastapi.testclient import TestClient

from main import app
from routes.resumes import (
    MAX_BYTES,
    _RESUME_BYTES,
    _RESUMES_DB,
    _seed,
)


class _ResumesTestCase(unittest.TestCase):
    def setUp(self) -> None:
        _seed()
        self.client = TestClient(app)


def _upload(client, filename: str, contents: bytes, *, tags: str | None = None,
            is_default: bool = False) -> dict:
    files = {"file": (filename, contents, "application/pdf")}
    data: dict[str, str] = {}
    if tags is not None:
        data["tags"] = tags
    if is_default:
        # The frontend sends a string ``"true"``; FastAPI parses it to bool.
        data["is_default"] = "true"
    return client.post("/api/resumes", files=files, data=data).json()


# ---------------------------------------------------------------------------
class TestListSeeded(_ResumesTestCase):
    def test_get_returns_two_seed_records(self) -> None:
        r = self.client.get("/api/resumes")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["total"], 2)
        ids = {item["id"] for item in body["resumes"]}
        self.assertEqual(ids, {"r_seed_1", "r_seed_2"})

    def test_get_envelope_shape(self) -> None:
        body = self.client.get("/api/resumes").json()
        for resume in body["resumes"]:
            self.assertIn("id", resume)
            self.assertIn("name", resume)
            self.assertIn("size_bytes", resume)
            self.assertIn("uploaded_at", resume)
            self.assertIsInstance(resume["tags"], list)
            self.assertIsInstance(resume["is_default"], bool)


# ---------------------------------------------------------------------------
class TestUpload(_ResumesTestCase):
    def test_upload_returns_201_with_id_and_reflects_metadata(self) -> None:
        payload = _upload(self.client, "new.pdf", b"%PDF-FAKE", tags="ml,backend",
                          is_default=True)
        self.assertIn("id", payload)
        self.assertEqual(payload["name"], "new.pdf")
        self.assertEqual(payload["size_bytes"], len(b"%PDF-FAKE"))
        self.assertTrue(payload["is_default"])
        self.assertEqual(payload["tags"], ["ml", "backend"])
        # Bytes are stored so ``/download`` will stream them back.
        self.assertIn(payload["id"], _RESUME_BYTES)


class TestUploadOversized(_ResumesTestCase):
    def test_upload_over_10mb_returns_413(self) -> None:
        big = b"x" * (MAX_BYTES + 1)
        r = self.client.post(
            "/api/resumes",
            files={"file": ("huge.pdf", big, "application/pdf")},
        )
        self.assertEqual(r.status_code, 413, r.text)
        self.assertIn("mb", r.json()["detail"].lower())


class TestUploadIsDefaultDemotesSeed(_ResumesTestCase):
    def test_uploading_a_new_default_clears_the_old_one(self) -> None:
        # Seeded record ``r_seed_1`` is the existing default.
        self.assertTrue(_RESUMES_DB["r_seed_1"]["is_default"])
        _upload(self.client, "new.pdf", b"%PDF", tags="ml", is_default=True)
        self.assertFalse(_RESUMES_DB["r_seed_1"]["is_default"])


# ---------------------------------------------------------------------------
class TestPatch(_ResumesTestCase):
    def test_patch_normalizes_tags_trims_dedupes_drop_blanks(self) -> None:
        r = self.client.patch(
            "/api/resumes/r_seed_2",
            json={"tags": ["  ml  ", "", "ml", "backend", " backend ", ""]},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["tags"], ["ml", "backend"])

    def test_patch_is_default_demotes_existing_default(self) -> None:
        r = self.client.patch(
            "/api/resumes/r_seed_2", json={"is_default": True},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["is_default"])
        # Seeded r_seed_1 was the prior default; should be flipped off.
        body_after = self.client.get("/api/resumes").json()
        for item in body_after["resumes"]:
            if item["id"] == "r_seed_1":
                self.assertFalse(item["is_default"])

    def test_patch_missing_returns_404(self) -> None:
        r = self.client.patch(
            "/api/resumes/does-not-exist", json={"tags": ["x"]},
        )
        self.assertEqual(r.status_code, 404, r.text)


# ---------------------------------------------------------------------------
class TestDelete(_ResumesTestCase):
    def test_delete_seed_returns_204_and_removes_record_and_bytes(self) -> None:
        r = self.client.delete("/api/resumes/r_seed_1")
        self.assertEqual(r.status_code, 204, r.text)
        self.assertEqual(r.content, b"")
        self.assertNotIn("r_seed_1", _RESUMES_DB)
        self.assertNotIn("r_seed_1", _RESUME_BYTES)

    def test_delete_missing_returns_404(self) -> None:
        r = self.client.delete("/api/resumes/does-not-exist")
        self.assertEqual(r.status_code, 404, r.text)


# ---------------------------------------------------------------------------
class TestDownload(_ResumesTestCase):
    def test_download_for_seeded_metadata_returns_410(self) -> None:
        # Seeded records have metadata but no bytes; the frontend should
        # see a clean ``410 Gone`` rather than a 5xx-encoded "missing bytes".
        r = self.client.get("/api/resumes/r_seed_1/download")
        self.assertEqual(r.status_code, 410, r.text)

    def test_download_for_uploaded_resume_streams_bytes_back(self) -> None:
        upload_bytes = b"%PDF-1.4 fake contents"
        created = _upload(self.client, "spec.pdf", upload_bytes)
        r = self.client.get(f"/api/resumes/{created['id']}/download")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.content, upload_bytes)
        cd = r.headers["content-disposition"]
        self.assertIn("attachment", cd)
        self.assertIn("spec.pdf", cd)


if __name__ == "__main__":
    unittest.main()
