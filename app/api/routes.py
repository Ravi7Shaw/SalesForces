import json
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from app.auth import require_auth
from app.db import SessionLocal
from app.models.job import IngestionJob, JobObject
from app.services.clickhouse import ClickHouseStore
from app.services.schema import OBJECTS
from app.services.storage import MinioStorage
from app.services.worker import manager

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_auth)])


class SyncRequest(BaseModel):
    organization_id: str = Field(min_length=1, max_length=128)
    objects: list[str] = Field(default_factory=lambda: OBJECTS.copy(), min_length=1)
    records_per_object: int = Field(default=1000, ge=1, le=100000)

    @field_validator("objects")
    @classmethod
    def unique_objects(cls, objects: list[str]) -> list[str]:
        return list(dict.fromkeys(objects))


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


def _lifecycle(result: str, job_id: str, status: str, conflict_msg: str):
    if result == "not_found":
        raise HTTPException(404, "Job not found")
    if result == "conflict":
        raise HTTPException(409, conflict_msg)
    return {"job_id": job_id, "status": status}


@router.post("/jobs/{job_id}/pause")
def pause(job_id: str):
    return _lifecycle(manager.pause(job_id), job_id, "paused", "Only pending/running jobs can be paused")


@router.post("/jobs/{job_id}/resume")
def resume(job_id: str):
    return _lifecycle(
        manager.resume(job_id), job_id, "running", "Only paused, interrupted or failed jobs can be resumed"
    )


@router.post("/jobs/{job_id}/cancel")
def cancel(job_id: str):
    return _lifecycle(manager.cancel(job_id), job_id, "cancelled", "Job already finished; cannot cancel")


@router.get("/objects")
def objects():
    return OBJECTS


@router.get("/storage")
def storage(prefix: str = "salesforce/"):
    return MinioStorage().list_objects(prefix)


@router.get("/storage/preview")
def storage_preview(key: str):
    """First bytes of a landed CSV, for the MinIO file browser in the dashboard."""
    try:
        return {"key": key, "text": MinioStorage().preview(key)}
    except Exception as exc:
        raise HTTPException(404, f"Could not read {key}: {exc}") from exc


@router.get("/clickhouse/{object_name}")
def clickhouse_object(object_name: str):
    if object_name not in OBJECTS:
        raise HTTPException(404, "Unknown Salesforce object")
    ch = ClickHouseStore()
    ch.ensure_table(object_name)
    info = ch.describe(object_name)
    return info


@router.get("/jobs/{job_id}/objects")
def job_objects(job_id: str):
    """Per-object checkpoint detail (state, row count, landed file, error) backing the progress bars."""
    session = SessionLocal()
    try:
        if not session.get(IngestionJob, job_id):
            raise HTTPException(404, "Job not found")
        rows = session.query(JobObject).filter_by(job_id=job_id).all()
        return [
            {
                "object": r.object_name,
                "state": r.state,
                "salesforce_job_id": r.salesforce_job_id,
                "rows": r.rows,
                "minio_key": r.minio_key,
                "error": r.error,
                "started_at": r.started_at,
                "finished_at": r.finished_at,
            }
            for r in rows
        ]
    finally:
        session.close()


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
