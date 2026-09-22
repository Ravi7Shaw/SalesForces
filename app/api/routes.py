import json
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.db import SessionLocal
from app.models.job import IngestionJob
from app.services.clickhouse import ClickHouseStore, OBJECT_COLUMNS, table_name
from app.services.salesforce import OBJECTS
from app.services.storage import MinioStorage
from app.services.worker import manager

router = APIRouter(prefix="/api/v1")


class SyncRequest(BaseModel):
    organization_id: str = Field(min_length=1, max_length=128)
    objects: list[str] = Field(default_factory=lambda: OBJECTS.copy(), min_length=1)
    records_per_object: int = Field(default=1000, ge=1, le=100000)


@router.post("/jobs/sync", status_code=202)
def sync(req: SyncRequest):
    unknown = set(req.objects) - set(OBJECTS)
    if unknown:
        raise HTTPException(400, f"Unsupported objects: {sorted(unknown)}")
    job_id = manager.start(req.organization_id, req.objects, req.records_per_object)
    return {"job_id": job_id, "status": "pending"}


@router.get("/jobs")
def jobs():
    session = SessionLocal()
    rows = session.query(IngestionJob).order_by(IngestionJob.created_at.desc()).limit(100).all()
    result = [serialize(x) for x in rows]
    session.close()
    return result


@router.get("/jobs/{job_id}")
def status(job_id: str):
    session = SessionLocal()
    job = session.get(IngestionJob, job_id)
    result = serialize(job) if job else None
    session.close()
    if not result:
        raise HTTPException(404, "Job not found")
    return result


@router.post("/jobs/{job_id}/pause")
def pause(job_id: str):
    if not manager.pause(job_id):
        raise HTTPException(404, "Job not found or not pausable")
    return {"job_id": job_id, "status": "paused"}


@router.post("/jobs/{job_id}/resume")
def resume(job_id: str):
    if not manager.resume(job_id):
        raise HTTPException(409, "Job is not paused")
    return {"job_id": job_id, "status": "running"}


@router.post("/jobs/{job_id}/cancel")
def cancel(job_id: str):
    if not manager.cancel(job_id):
        raise HTTPException(404, "Job not found")
    return {"job_id": job_id, "status": "cancelled"}


@router.get("/objects")
def objects():
    return OBJECTS


@router.get("/storage")
def storage(prefix: str = "salesforce/"):
    return MinioStorage().list_objects(prefix)


@router.get("/clickhouse/{object_name}")
def clickhouse_object(object_name: str):
    if object_name not in OBJECTS:
        raise HTTPException(404, "Unknown Salesforce object")
    ch = ClickHouseStore()
    ch.ensure_table(object_name)
    count = ch.count(object_name)
    return {"object": object_name, "table": table_name(object_name), "columns": OBJECT_COLUMNS[object_name], "rows": count}


def serialize(j: IngestionJob):
    return {
        "job_id": j.id,
        "organization_id": j.organization_id,
        "status": j.status,
        "objects": json.loads(j.objects_json),
        "completed_objects": json.loads(j.completed_objects_json or "[]"),
        "current_object": j.current_object,
        "processed_objects": j.processed_objects,
        "total_rows": j.total_rows,
        "error": j.error,
        "created_at": j.created_at,
        "started_at": j.started_at,
        "finished_at": j.finished_at,
        "updated_at": j.updated_at,
    }
