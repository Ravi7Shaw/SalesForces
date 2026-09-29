from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/be1"

    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_bucket: str = "salesforce-raw"

    clickhouse_host: str = "localhost"
    clickhouse_port: int = 8123
    clickhouse_user: str = "default"
    clickhouse_password: str = ""

    # "mock" or "real" is informational only: both modes run the same OAuth2
    # refresh-token flow, they just point at a different SALESFORCE_BASE_URL.
    salesforce_mode: str = "mock"
    salesforce_base_url: str = "http://localhost:9000"
    salesforce_login_url: str = ""  # defaults to salesforce_base_url
    salesforce_api_version: str = "v60.0"
    salesforce_client_id: str = ""
    salesforce_client_secret: str = ""
    salesforce_refresh_token: str = ""
    bulk_page_size: int = 50000  # maxRecords per Bulk v2 results page
    job_timeout_seconds: int = 900

    # --- API authentication (see app/auth.py) ---
    auth_enabled: bool = True
    api_key: str = ""  # used by the dashboard / humans (X-API-Key)
    hmac_secret: str = ""  # used by service callers (X-Timestamp + X-Signature)
    auth_max_skew_seconds: int = 300

    # --- worker ---
    max_concurrent_jobs: int = 2
    auto_resume_on_startup: bool = False


settings = Settings()
