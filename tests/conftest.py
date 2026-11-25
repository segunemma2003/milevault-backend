import os

os.environ.setdefault("DATABASE_URL", "sqlite://")

from app.services.celery_app import fail_fast_publishing  # noqa: E402

fail_fast_publishing()  # no Redis in tests: scheduling a job must not stall each test ~19s
