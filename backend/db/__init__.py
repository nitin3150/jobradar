"""Backend ``db`` package — SQLAlchemy models + Alembic migrations.

This file is intentionally a bare marker; importing :mod:`backend.db.models`
brings the declarative ``Base`` and every mapped class into scope. Routes
that want to query the database should import from :mod:`backend.db.models`,
not from here, so that Alembic's autogenerate can introspect metadata
without booting FastAPI.
"""
