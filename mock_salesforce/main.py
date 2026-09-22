import csv
import io
import re
import uuid

from fastapi import FastAPI, Header, HTTPException

app = FastAPI(title="Salesforce Bulk API v2 Mock")
jobs = {}

OBJECT_FIELDS = {
    "Accounts": ["Id", "Name", "Industry", "Website"],
    "Contacts": ["Id", "FirstName", "LastName", "Email", "AccountId"],
    "Opportunities": ["Id", "Name", "Amount", "StageName", "AccountId"],
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
        "Amount": str((i + 1) * 100.5), "StageName": "Prospecting", "Company": "Example Corp",
        "Status": "Open", "Subject": f"Task {i + 1}", "ActivityDate": "2026-09-22",
        "OwnerId": "USR-00000001", "Priority": "Normal", "ProductCode": f"SKU-{i + 1}",
        "Family": "Default", "IsActive": "true", "Product2Id": f"PRO-{(i % 100) + 1:08d}",
        "UnitPrice": str((i + 1) * 10), "StartDate": "2026-01-01", "EndDate": "2027-01-01",
        "Name": f"{obj} {i + 1}",
    }
    return values.get(field, "")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/services/data/v60.0/jobs/query")
def create(payload: dict, authorization: str | None = Header(None)):
    if authorization != "Bearer mock-token":
        raise HTTPException(401, "invalid token")
    query = payload.get("query", "")
    obj = next((o for o in OBJECT_FIELDS if re.search(rf"\bFROM\s+{o}\b", query, re.I)), None)
    if not obj:
        raise HTTPException(400, "object not found")
    match = re.search(r"\bLIMIT\s+(\d+)\b", query, re.I)
    count = min(int(match.group(1)) if match else 1000, 100000)
    job_id = uuid.uuid4().hex
    jobs[job_id] = {"object": obj, "count": count, "state": "JobComplete"}
    return {"id": job_id, "state": "UploadComplete"}


@app.get("/services/data/v60.0/jobs/query/{jid}")
def status(jid: str):
    if jid not in jobs:
        raise HTTPException(404, "job not found")
    return jobs[jid]


@app.get("/services/data/v60.0/jobs/query/{jid}/results")
def results(jid: str, authorization: str | None = Header(None)):
    if authorization != "Bearer mock-token":
        raise HTTPException(401, "invalid token")
    job = jobs.get(jid)
    if not job:
        raise HTTPException(404, "job not found")

    fields = OBJECT_FIELDS[job["object"]]
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=fields)
    writer.writeheader()
    for i in range(job["count"]):
        row = {field: default_value(field, i, job["object"]) for field in fields}
        row["Id"] = f"{job['object'][:3].upper()}-{i + 1:08d}"
        writer.writerow(row)
    return out.getvalue()
