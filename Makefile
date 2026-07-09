# JobRadar — repo-root Makefile.
#
# Why this file exists
# ====================
# Starlette transitively imports ``python-multipart`` to wire up its
# multipart-form parser; without it, FastAPI's TestClient fails at
# module-import time during test collection. When the parser's import
# fails it produces a low-grade ``ModuleNotFoundError`` that's easy to
# silence — and a separate, louder schema-blocker traceback (a
# ``company_id NOT NULL`` violation, a ``jobs.status`` enum cast
# mismatch) tends to dominate the operator's terminal. The two
# failures then look like one, with the schema side-effect blamed for
# what was actually a missing-dep problem.
#
# Centralising the install in a pinned Makefile target — and pulling
# it OUT of ``backend/requirements.txt`` so ``uv pip install -e .``
# never strays into an unpinned 0.0.x release — closes that masking.
# Any path that invokes the Makefile (``make bootstrap``, ``make
# pre-commit``, ``make test``) installs the dep *first*, so the parser
# is never the silent failure that lets the schema side-effect hide.
#
# POSIX-make
# ==========
# Targets use only POSIX-make features so stock BSD ``make`` on macOS
# works without ``gmake`` — no ``$(shell …)``, no ``.SHELLFLAGS``, no
# conditional includes. Recipes use literal TAB characters; non-tab
# indentation is a hard error on BSD make.

.PHONY: bootstrap pre-commit install-multipart test help

# Pinned floor ``>=0.0.9`` excludes the broken early releases; ``-q``
# keeps pip's resolver chatter off the operator's terminal. Idempotent
# — pip reports "Requirement already satisfied" and exits promptly on
# a satisfying version, so calling this from ``bootstrap`` /
# ``pre-commit`` / ``test`` only costs one resolver round-trip after
# the first run.
install-multipart:
	pip install -q "python-multipart>=0.0.9"

# Conventional names — pre-commit hooks and "set up my fresh clone"
# scripts typically invoke one of these. Both depend on the install
# target so Make's dependency graph de-duplicates the underlying
# pip call.
bootstrap: install-multipart
pre-commit: install-multipart

# Canonical Python test entry point — matches the pattern documented
# in ``backend/README.md`` (``cd backend; python -m unittest discover
# tests -v``). The ``cd backend`` step is load-bearing: the test
# modules import top-level packages (``from db import models as
# db_models``, ``from routes.jobs import _seed_job_rows``) that
# resolve only when ``backend/`` is the working directory.
test: install-multipart
	cd backend && JOBRADAR_TEST_DB=1 python -m unittest discover -s tests -v

help:
	@printf "JobRadar repo-root Makefile targets:\n"
	@printf "  install-multipart  pip install -q 'python-multipart>=0.0.9'.\n"
	@printf "  bootstrap          alias for install-multipart.\n"
	@printf "  pre-commit         alias for install-multipart.\n"
	@printf "  test               install-multipart, then 'cd backend && JOBRADAR_TEST_DB=1 python -m unittest discover'.\n"
