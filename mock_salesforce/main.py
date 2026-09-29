"""Synthetic Salesforce sandbox: real OAuth2 refresh-token flow + Bulk API v2 (query) semantics,
including Sforce-Locator result paging, so the exact same client code exercises real orgs too.
"""
import csv
import io
import re
import uuid

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import PlainTextResponse

app = FastAPI(title="Salesforce Bulk API v2 Mock")
jobs: dict[str, dict] = {}
TOKENS = {"mock-access-token"}
PAGE_SIZE_CAP = 2000  # force multi-page results even for smallish requests, to exercise paging

# Real Salesforce API names -> our object keys (the ingestion service sends the real names).
API_NAMES = {
    "Account": "Accounts", "Contact": "Contacts", "Opportunity": "Opportunities", "Lead": "Leads",
    "Task": "Tasks", "Case": "Cases", "Product2": "Products", "PricebookEntry": "PricebookEntries",
    "Contract": "Contracts", "Asset": "Assets",
}

OBJECT_FIELDS = {
    "Accounts": ["Id", "Name", "Industry", "Website"],
    "Contacts": ["Id", "FirstName", "LastName", "Email", "AccountId"],
    "Opportunities": ["Id", "Name", "Amount", "StageName", "CloseDate", "AccountId"],
    "Leads": ["Id", "FirstName", "LastName", "Company", "Email", "Status"],
    "Tasks": ["Id", "Subject", "Status", "ActivityDate", "OwnerId"],
    "Cases": ["Id", "Subject", "Status", "Priority", "AccountId"],
    "Products": ["Id", "Name", "ProductCode", "Family", "IsActive"],
    "PricebookEntries": ["Id", "Product2Id", "UnitPrice", "IsActive"],
    "Contracts": ["Id", "AccountId", "Status", "StartDate", "EndDate"],
    "Assets": ["Id", "Name", "AccountId", "Product2Id", "Status"],
}


def default_value(field: str, i: int, obj: str) -> str:
    values = {
        "Industry": "Technology", "Website": "https://example.com", "FirstName": "Test",
        "LastName": f"User{i}", "Email": f"user{i}@example.com", "AccountId": f"ACC-{(i % 100) + 1:08d}",
        "Amount": str((i + 1) * 100.5), "StageName": "Prospecting", "CloseDate": "2026-12-31",
        "Company": "Example Corp", "Status": "Open", "Subject": f"Task {i + 1}",
        "ActivityDate": "2026-09-22", "OwnerId": "USR-00000001", "Priority": "Normal",
        "ProductCode": f"SKU-{i + 1}", "Family": "Default", "IsActive": "true",
        "Product2Id": f"PRO-{(i % 100) + 1:08d}", "UnitPrice": str((i + 1) * 10),
        "StartDate": "2026-01-01", "EndDate": "2027-01-01", "Name": f"{obj} {i + 1}",
    }
    return values.get(field, "")


def render_csv(obj: str, start: int, end: int) -> str:
    fields = OBJECT_FIELDS[obj]
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=fields)
    writer.writeheader()
    for i in range(start, end):
        row = {field: default_value(field, i, obj) for field in fields}
        row["Id"] = f"{obj[:3].upper()}-{i + 1:08d}"
        writer.writerow(row)
    return out.getvalue()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/services/oauth2/token")
def token(grant_type: str | None = None, client_id: str | None = None, refresh_token: str | None = None):
    # FastAPI reads x-www-form-urlencoded fields as query/body params of the same name.
    if grant_type != "refresh_token" or not client_id or not refresh_token:
        raise HTTPException(400, "invalid_grant")
    access_token = uuid.uuid4().hex
    TOKENS.add(access_token)
    return {"access_token": access_token, "token_type": "Bearer", "instance_url": "http://mock-salesforce:9000"}


def _check_auth(authorization: str | None):
    token_value = (authorization or "").removeprefix("Bearer ").strip()
    if token_value not in TOKENS:
        raise HTTPException(401, "invalid token")


@app.post("/services/data/v60.0/jobs/query")
def create(payload: dict, authorization: str | None = Header(None)):
    _check_auth(authorization)
    query = payload.get("query", "")
    m = re.search(r"\bFROM\s+(\w+)", query, re.I)
    obj = API_NAMES.get(m.group(1)) if m else None
    if not obj:
        raise HTTPException(400, f"object not found in query: {query!r}")
    match = re.search(r"\bLIMIT\s+(\d+)\b", query, re.I)
    count = min(int(match.group(1)) if match else 1000, 100000)
    job_id = uuid.uuid4().hex
    jobs[job_id] = {"object": obj, "count": count, "state": "JobComplete"}
    return {"id": job_id, "state": "UploadComplete", "object": obj}


@app.get("/services/data/v60.0/jobs/query/{jid}")
def status(jid: str, authorization: str | None = Header(None)):
    _check_auth(authorization)
    if jid not in jobs:
        raise HTTPException(404, "job not found")
    return jobs[jid]


@app.get("/services/data/v60.0/jobs/query/{jid}/results")
def results(jid: str, authorization: str | None = Header(None), locator: str | None = None, maxRecords: int = 10000):
    _check_auth(authorization)
    job = jobs.get(jid)
    if not job:
        raise HTTPException(404, "job not found")

    page_size = min(maxRecords, PAGE_SIZE_CAP)
    start = int(locator) if locator else 0
    end = min(start + page_size, job["count"])
    body = render_csv(job["object"], start, end)

    headers = {}
    if end < job["count"]:
        headers["Sforce-Locator"] = str(end)  # more pages to fetch
    else:
        headers["Sforce-Locator"] = "null"
    headers["Sforce-NumberOfRecords"] = str(end - start)
    return PlainTextResponse(body, media_type="text/csv", headers=headers)
