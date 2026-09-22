import csv
import io
import time
from datetime import datetime, timezone

import httpx

from app.config import settings
from app.services.retry import retry_call

OBJECTS = [
    "Accounts", "Contacts", "Opportunities", "Leads", "Tasks",
    "Cases", "Products", "PricebookEntries", "Contracts", "Assets",
]


class SalesforceBulkClient:
    """Small Salesforce Bulk API v2 query client with mock/real authentication."""

    def __init__(self):
        self.base = settings.salesforce_base_url.rstrip("/")
        self.client = httpx.Client(timeout=httpx.Timeout(60.0, connect=10.0))

    def token(self) -> str:
        if settings.salesforce_mode == "mock":
            return "mock-token"

        response = self.client.post(
            f"{self.base}/services/oauth2/token",
            data={
                "grant_type": "refresh_token",
                "client_id": settings.salesforce_client_id,
                "client_secret": settings.salesforce_client_secret,
                "refresh_token": settings.salesforce_refresh_token,
            },
        )
        response.raise_for_status()
        return response.json()["access_token"]

    def _headers(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def create_query_job(self, token: str, obj: str, records: int) -> str:
        query = f"SELECT FIELDS(ALL) FROM {obj} LIMIT {int(records)}"

        def request():
            response = self.client.post(
                f"{self.base}/services/data/v60.0/jobs/query",
                headers=self._headers(token),
                json={"operation": "query", "query": query, "contentType": "CSV"},
            )
            response.raise_for_status()
            return response.json()["id"]

        return retry_call(request)

    def wait_for_job(self, token: str, job_id: str) -> dict:
        for _ in range(300):
            def request():
                response = self.client.get(
                    f"{self.base}/services/data/v60.0/jobs/query/{job_id}",
                    headers=self._headers(token),
                )
                response.raise_for_status()
                return response.json()

            data = retry_call(request)
            if data["state"] in {"JobComplete", "Failed", "Aborted"}:
                return data
            time.sleep(0.2)
        raise TimeoutError(f"Salesforce Bulk job {job_id} did not finish within 60 seconds")

    def results(self, token: str, job_id: str) -> str:
        def request():
            response = self.client.get(
                f"{self.base}/services/data/v60.0/jobs/query/{job_id}/results",
                headers=self._headers(token),
            )
            response.raise_for_status()
            return response.text

        return retry_call(request)

    def run(self, obj: str, records: int) -> tuple[str, str]:
        token = self.token()
        job_id = self.create_query_job(token, obj, records)
        state = self.wait_for_job(token, job_id)
        if state["state"] != "JobComplete":
            raise RuntimeError(state.get("errorMessage", f"Salesforce job ended in {state['state']}"))
        return job_id, self.results(token, job_id)


def csv_rows(obj: str, text: str, organisation_id: str):
    reader = csv.DictReader(io.StringIO(text))
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    out = []
    for row in reader:
        if obj == "Accounts":
            out.append([row.get("Id", ""), row.get("Name", ""), row.get("Industry"), row.get("Website"), organisation_id, now])
        elif obj == "Contacts":
            out.append([row.get("Id", ""), row.get("FirstName", ""), row.get("LastName", ""), row.get("Email"), row.get("AccountId"), organisation_id, now])
        elif obj == "Opportunities":
            out.append([row.get("Id", ""), row.get("Name", ""), float(row.get("Amount") or 0), row.get("StageName", ""), row.get("AccountId"), organisation_id, now])
        elif obj == "Leads":
            out.append([row.get("Id", ""), row.get("FirstName", ""), row.get("LastName", ""), row.get("Company"), row.get("Email"), row.get("Status", ""), organisation_id, now])
        elif obj == "Tasks":
            out.append([row.get("Id", ""), row.get("Subject", ""), row.get("Status", ""), row.get("ActivityDate", ""), row.get("OwnerId"), organisation_id, now])
        elif obj == "Cases":
            out.append([row.get("Id", ""), row.get("Subject", ""), row.get("Status", ""), row.get("Priority", ""), row.get("AccountId"), organisation_id, now])
        elif obj == "Products":
            out.append([row.get("Id", ""), row.get("Name", ""), row.get("ProductCode", ""), row.get("Family"), str(row.get("IsActive", "true")).lower() == "true", organisation_id, now])
        elif obj == "PricebookEntries":
            out.append([row.get("Id", ""), row.get("Product2Id", ""), float(row.get("UnitPrice") or 0), str(row.get("IsActive", "true")).lower() == "true", organisation_id, now])
        elif obj == "Contracts":
            out.append([row.get("Id", ""), row.get("AccountId"), row.get("Status", ""), row.get("StartDate", ""), row.get("EndDate", ""), organisation_id, now])
        elif obj == "Assets":
            out.append([row.get("Id", ""), row.get("Name", ""), row.get("AccountId"), row.get("Product2Id"), row.get("Status", ""), organisation_id, now])
    return out
