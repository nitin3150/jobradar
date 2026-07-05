FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl && rm -rf /var/lib/apt/lists/*

# `pip install -e .` runs *before* the source tree is COPY'd in this Dockerfile,
# so setuptools auto-discovery at install time finds no Python packages and
# registers no .pth on sys.path. At runtime, `from app.config import settings`
# in /app/alembic/env.py fails with ModuleNotFoundError. Setting PYTHONPATH=/app
# is the smallest fix that doesn't bust the pip-install build layer on every
# source-code change.
ENV PYTHONPATH=/app

COPY pyproject.toml .
RUN pip install --no-cache-dir -e .
RUN playwright install chromium --with-deps

COPY . .
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
