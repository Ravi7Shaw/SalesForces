"""Configuration checks need the Compose CLI, but no Docker daemon."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def compose():
    if not shutil.which("docker"):
        pytest.skip("Docker Compose CLI is not installed")
    probe = subprocess.run(["docker", "compose", "version"], capture_output=True)
    if probe.returncode:
        pytest.skip("Docker Compose CLI is not installed")

    def render(path):
        # Do not let the caller's credentials/settings override synthetic inputs.
        env = {k: v for k, v in os.environ.items() if not k.startswith((
            "SALESFORCE_", "API_KEY", "HMAC_", "AUTH_", "MAX_CONCURRENT_", "AUTO_RESUME_",
            "MINIO_", "CLICKHOUSE_", "JOB_TIMEOUT_", "BULK_PAGE_"))}
        return json.loads(subprocess.check_output(
            ["docker", "compose", "--env-file", str(path), "config", "--format", "json"], cwd=ROOT, env=env))
    return render


def test_example_environment_preserves_container_addresses(compose):
    config = compose(ROOT / ".env.example")
    api = config["services"]["api"]["environment"]
    assert api["SALESFORCE_BASE_URL"] == "http://mock-salesforce:9000"
    assert api["MINIO_ENDPOINT"] == "minio:9000"
    assert api["CLICKHOUSE_HOST"] == "clickhouse"
    assert "@postgres:5432/" in api["DATABASE_URL"]


def test_environment_overrides_reach_container(compose, tmp_path):
    overrides = {
        "API_KEY": "synthetic-key", "HMAC_SECRET": "", "AUTH_ENABLED": "false",
        "SALESFORCE_MODE": "real", "SALESFORCE_BASE_URL": "https://synthetic.invalid",
        "SALESFORCE_LOGIN_URL": "https://login.synthetic.invalid", "SALESFORCE_API_VERSION": "v61.0",
        "SALESFORCE_CLIENT_ID": "synthetic-client", "SALESFORCE_CLIENT_SECRET": "synthetic-secret",
        "SALESFORCE_REFRESH_TOKEN": "synthetic-refresh", "MAX_CONCURRENT_JOBS": "3",
        "AUTO_RESUME_ON_STARTUP": "true", "AUTH_MAX_SKEW_SECONDS": "60", "BULK_PAGE_SIZE": "100",
        "JOB_TIMEOUT_SECONDS": "30", "MINIO_ACCESS_KEY": "synthetic-user",
        "MINIO_SECRET_KEY": "synthetic-password", "CLICKHOUSE_USER": "synthetic-user",
        "CLICKHOUSE_PASSWORD": "synthetic-password", "MINIO_BUCKET": "synthetic-bucket",
    }
    path = tmp_path / "compose.env"
    path.write_text("\n".join(f"{k}={v}" for k, v in overrides.items()))
    services = compose(path)["services"]
    api = services["api"]["environment"]
    for key, value in overrides.items():
        assert api[key] == value, key
    assert services["minio"]["environment"]["MINIO_ROOT_PASSWORD"] == overrides["MINIO_SECRET_KEY"]
    assert services["clickhouse"]["environment"]["CLICKHOUSE_PASSWORD"] == overrides["CLICKHOUSE_PASSWORD"]
