"""Salesforce Bulk API v2 (query) client.

Auth is always the OAuth2 refresh-token flow. "Mock" and "real" mode differ only
by SALESFORCE_BASE_URL, so the exact same code path is exercised in tests.
"""
import time
from collections.abc import Callable

import httpx

from app.config import settings
from app.services.retry import retry_call
from app.services.schema import OBJECTS, csv_rows, soql  # noqa: F401  (re-exported)

TERMINAL_STATES = {"JobComplete", "Failed", "Aborted"}


class SalesforceBulkClient:
    def __init__(
        self,
        client: httpx.Client | None = None,
        base_url: str | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.base = (base_url or settings.salesforce_base_url).rstrip("/")
        self.login_url = (settings.salesforce_login_url or self.base).rstrip("/")
        self.client = client or httpx.Client(timeout=httpx.Timeout(60.0, connect=10.0))
        self._sleep = sleep
        self._token: str | None = None
        self._api = f"/services/data/{settings.salesforce_api_version}"

    # ---- auth -----------------------------------------------------------
    def authenticate(self) -> str:
        def request():
            response = self.client.post(
                f"{self.login_url}/services/oauth2/token",
                data={
                    "grant_type": "refresh_token",
                    "client_id": settings.salesforce_client_id,
                    "client_secret": settings.salesforce_client_secret,
                    "refresh_token": settings.salesforce_refresh_token,
                },
            )
            response.raise_for_status()
            return response.json()

        data = retry_call(request, sleep=self._sleep)
        self._token = data["access_token"]
        if data.get("instance_url"):  # real orgs tell us which instance to call
            self.base = data["instance_url"].rstrip("/")
        return self._token

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """Authenticated request with backoff; re-authenticates once on 401 (expired token)."""
        for attempt in (0, 1):
            if not self._token:
                self.authenticate()

            def call():
                response = self.client.request(
                    method, f"{self.base}{path}", headers={"Authorization": f"Bearer {self._token}"}, **kwargs
                )
                response.raise_for_status()
                return response

            try:
                return retry_call(call, sleep=self._sleep)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 401 and attempt == 0:
                    self._token = None
                    continue
                raise
        raise RuntimeError("unreachable")

    # ---- Bulk API v2 ----------------------------------------------------
    def create_query_job(self, obj: str, records: int) -> str:
        response = self._request(
            "POST",
            f"{self._api}/jobs/query",
            json={"operation": "query", "query": soql(obj, records), "contentType": "CSV"},
        )
        return response.json()["id"]

    def wait_for_job(self, job_id: str) -> dict:
        waited, delay = 0.0, 0.2
        while waited <= settings.job_timeout_seconds:
            data = self._request("GET", f"{self._api}/jobs/query/{job_id}").json()
            if data["state"] in TERMINAL_STATES:
                return data
            self._sleep(delay)
            waited += delay
            delay = min(delay * 1.5, 5.0)
        raise TimeoutError(f"Salesforce Bulk job {job_id} did not finish within {settings.job_timeout_seconds}s")

    def results(self, job_id: str) -> str:
        """Fetch every results page (Sforce-Locator paging) and return one CSV document."""
        parts: list[str] = []
        header: str | None = None
        locator: str | None = None
        while True:
            params = {"maxRecords": settings.bulk_page_size}
            if locator:
                params["locator"] = locator
            response = self._request("GET", f"{self._api}/jobs/query/{job_id}/results", params=params)
            text = response.text
            if text and not text.endswith("\n"):
                text += "\n"
            if header is None:
                header = text.split("\n", 1)[0]
            elif text.split("\n", 1)[0] == header:
                text = text.split("\n", 1)[1] if "\n" in text else ""  # repeated header on later pages
            parts.append(text)
            locator = response.headers.get("Sforce-Locator")
            if not locator or locator.lower() == "null":
                break
        return "".join(parts)

    def run(self, obj: str, records: int) -> tuple[str, str]:
        job_id = self.create_query_job(obj, records)
        state = self.wait_for_job(job_id)
        if state["state"] != "JobComplete":
            raise RuntimeError(state.get("errorMessage") or f"Salesforce job {job_id} ended in {state['state']}")
        return job_id, self.results(job_id)
