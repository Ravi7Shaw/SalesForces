"""Test env vars must be set before app.config (or anything importing it) loads."""
import os
import tempfile

os.environ.setdefault("DATABASE_URL", f"sqlite:///{tempfile.mktemp(suffix='.db')}")
os.environ.setdefault("AUTH_ENABLED", "true")
os.environ.setdefault("API_KEY", "test-key")
os.environ.setdefault("HMAC_SECRET", "test-hmac-secret")
os.environ.setdefault("MAX_CONCURRENT_JOBS", "5")
os.environ.setdefault("AUTO_RESUME_ON_STARTUP", "false")
os.environ.setdefault("SALESFORCE_BASE_URL", "http://unused.invalid")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture
def client():
    with TestClient(app) as c:  # runs the lifespan (create tables, startup recovery)
        yield c


@pytest.fixture
def auth_headers():
    return {"X-API-Key": settings.api_key}


@pytest.fixture(autouse=True)
def _clear_hmac_replay_cache():
    # HMAC signatures are timestamp-second-resolution; without this, two tests
    # that sign the same path within the same wall-clock second would collide.
    from app import auth

    auth._seen.clear()
    yield
    auth._seen.clear()
