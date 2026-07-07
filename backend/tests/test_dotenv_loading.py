"""Unit tests for `backend.main._load_env_files`.

The helper is exercised against a tmpdir + tmp ``.env`` files so the real
operator's ``backend/.env`` / repo-root ``.env`` files are never touched.
"""
import os
import unittest
from pathlib import Path

from main import _load_env_files


def _write(path: Path, content: str) -> None:
    path.write_text(content)


class DotenvLoadingTests(unittest.TestCase):
    def setUp(self):
        # Snapshot os.environ so each test starts from a known baseline.
        self._snapshot = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._snapshot)

    def test_loads_backend_then_root_with_precedence(self):
        # Two distinct keys so we can verify which file each came from;
        # backend/.env wins on the SHARED_KEY conflict.
        backend_dir = Path(self._enter_tmpdir()) / "backend"
        repo_root = backend_dir.parent
        backend_dir.mkdir()
        _write(
            backend_dir / ".env",
            "BACKEND_ONLY_KEY=from-backend\nSHARED_KEY=from-backend\n",
        )
        _write(
            repo_root / ".env",
            "ROOT_ONLY_KEY=from-root\nSHARED_KEY=from-root\n",
        )

        backend_loaded, root_loaded = _load_env_files(
            backend_dir=backend_dir, repo_root=repo_root
        )
        self.assertEqual(backend_loaded, backend_dir / ".env")
        self.assertEqual(root_loaded, repo_root / ".env")
        self.assertEqual(os.environ["BACKEND_ONLY_KEY"], "from-backend")
        self.assertEqual(os.environ["ROOT_ONLY_KEY"], "from-root")
        # Backend wins the conflict because it's loaded first (and we don't
        # call the loader with override=True).
        self.assertEqual(os.environ["SHARED_KEY"], "from-backend")

    def test_missing_files_return_none_paths(self):
        backend_dir = Path(self._enter_tmpdir()) / "backend"
        repo_root = backend_dir.parent
        backend_dir.mkdir()
        # Neither .env exists.
        backend_loaded, root_loaded = _load_env_files(
            backend_dir=backend_dir, repo_root=repo_root
        )
        self.assertIsNone(backend_loaded)
        self.assertIsNone(root_loaded)

    def test_shell_env_wins_by_default(self):
        # Process env wins (override=False), even if backend/.env disagrees.
        os.environ["SHARED_KEY"] = "from-shell"
        backend_dir = Path(self._enter_tmpdir()) / "backend"
        repo_root = backend_dir.parent
        backend_dir.mkdir()
        _write(backend_dir / ".env", "SHARED_KEY=from-backend-file\n")
        _write(repo_root / ".env", "SHARED_KEY=from-root-file\n")
        _load_env_files(backend_dir=backend_dir, repo_root=repo_root)
        self.assertEqual(os.environ["SHARED_KEY"], "from-shell")

    def test_override_true_lets_backend_clobber_shell(self):
        # Explicit override=True is supported for callers that want the
        # files to win over the shell (e.g. per-operator config).
        os.environ["SHARED_KEY"] = "from-shell"
        backend_dir = Path(self._enter_tmpdir()) / "backend"
        repo_root = backend_dir.parent
        backend_dir.mkdir()
        _write(backend_dir / ".env", "SHARED_KEY=from-backend-file\n")
        _load_env_files(backend_dir=backend_dir, repo_root=repo_root, override=True)
        self.assertEqual(os.environ["SHARED_KEY"], "from-backend-file")

    def test_github_token_round_trip(self):
        # End-to-end shape check: write a token, load, assert it lands in
        # os.environ where github_issues._GITHUB_TOKEN will read it.
        backend_dir = Path(self._enter_tmpdir()) / "backend"
        repo_root = backend_dir.parent
        backend_dir.mkdir()
        _write(backend_dir / ".env", "GITHUB_TOKEN=ghp_round_trip_xyz\n")
        _load_env_files(backend_dir=backend_dir, repo_root=repo_root)
        self.assertEqual(os.environ["GITHUB_TOKEN"], "ghp_round_trip_xyz")

    def _enter_tmpdir(self) -> str:
        import tempfile

        tmp = tempfile.mkdtemp(prefix="dotenv_test_")
        self.addCleanup(self._cleanup_tmpdir, tmp)
        return tmp

    @staticmethod
    def _cleanup_tmpdir(path: str) -> None:
        import shutil

        shutil.rmtree(path, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
