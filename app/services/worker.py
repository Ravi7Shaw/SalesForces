"""Background execution of ingestion jobs.

Each job runs on its own thread, gated by a semaphore so at most
``settings.max_concurrent_jobs`` run at once (extra jobs stay "pending" until
a slot frees up). Progress is checkpointed to Postgres after every object via
``JobObject`` rows, so a resumed job skips whatever already finished.

Crash recovery: on interpreter exit a job stuck "running" is not "paused", so
the original /resume (which only accepted "paused") could never reach it. This
version adds a startup sweep (see app.main) that marks orphaned "running" jobs
"interrupted", and /resume accepts "paused", "interrupted" and "failed".
"""
import json
import logging
import threading
import uuid
from datetime import date, datetime, timezone

from app.config import settings
from app.db import SessionLocal
from app.models.job import IngestionJob, JobObject
from app.services.clickhouse import ClickHouseStore
from app.services.salesforce import SalesforceBulkClient
from app.services.schema import csv_rows
from app.services.storage import MinioStorage

log = logging.getLogger("ingest")

OK, NOT_FOUND, CONFLICT = "ok", "not_found", "conflict"
TERMINAL = {"completed", "failed", "cancelled"}
# States /resume accepts. "interrupted" = process died mid-run (see startup sweep in app.main).
RESUMABLE = {"paused", "interrupted", "failed"}
STOP_REQUESTED = {"pausing", "cancelling"}


def _now():
    return datetime.now(timezone.utc)


class JobManager:
    def __init__(self):
        self.threads: dict[str, threading.Thread] = {}
        self.lock = threading.Lock()
        self.slots = threading.Semaphore(settings.max_concurrent_jobs)

    # ---------- helpers ----------------------------------------------------
    def _get(self, session, job_id: str) -> IngestionJob | None:
        return session.get(IngestionJob, job_id)

    def _set_status(self, job_id: str, status: str, **fields):
        session = SessionLocal()
        try:
            job = self._get(session, job_id)
            if not job:
                return
            job.status = status
            for k, v in fields.items():
                setattr(job, k, v)
            session.commit()
        finally:
            session.close()

    def startup_recovery(self):
        """Call once at process start: any job left "running" belongs to a dead process."""
        session = SessionLocal()
        try:
            orphans = session.query(IngestionJob).filter(IngestionJob.status.in_(["running", "pausing", "cancelling"])).all()
            for job in orphans:
                job.status = "interrupted"
                job.current_object = None
                log.warning("job %s was %s at startup, marked interrupted", job.id, job.status)
            session.commit()
            ids = [j.id for j in orphans]
        finally:
            session.close()
        if settings.auto_resume_on_startup:
            for job_id in ids:
                self.resume(job_id)
        return ids

    # ---------- lifecycle ----------------------------------------------------
    def start(self, organization_id: str, objects: list[str], records_per_object: int) -> str:
        job_id = uuid.uuid4().hex
        session = SessionLocal()
        try:
            session.add(
                IngestionJob(
                    id=job_id,
                    organization_id=organization_id,
                    status="pending",
                    objects_json=json.dumps(objects),
                    completed_objects_json="[]",
                    records_per_object=records_per_object,
                )
            )
            session.commit()
        finally:
            session.close()
        self._spawn(job_id)
        return job_id

    def pause(self, job_id: str) -> str:
        session = SessionLocal()
        try:
            job = self._get(session, job_id)
            if not job:
                return NOT_FOUND
            if job.status not in {"pending", "running"}:
                return CONFLICT
            # Checked between objects by the worker loop; can't interrupt an in-flight Salesforce call.
            job.status = "pausing" if job.status == "running" else "paused"
            session.commit()
            return OK
        finally:
            session.close()

    def cancel(self, job_id: str) -> str:
        session = SessionLocal()
        try:
            job = self._get(session, job_id)
            if not job:
                return NOT_FOUND
            if job.status in TERMINAL:
                return CONFLICT
            job.status = "cancelling" if job.status == "running" else "cancelled"
            if job.status == "cancelled":
                job.finished_at = _now()
            session.commit()
            return OK
        finally:
            session.close()

    def resume(self, job_id: str) -> str:
        session = SessionLocal()
        try:
            job = self._get(session, job_id)
            if not job:
                return NOT_FOUND
            if job.status not in RESUMABLE:
                return CONFLICT
            job.status = "pending"
            job.error = None
            session.commit()
        finally:
            session.close()
        self._spawn(job_id)
        return OK

    def _spawn(self, job_id: str):
        t = threading.Thread(target=self._run, args=(job_id,), daemon=True, name=f"job-{job_id[:8]}")
        with self.lock:
            self.threads[job_id] = t
        t.start()

    # ---------- execution ----------------------------------------------------
    def _run(self, job_id: str):
        acquired = self.slots.acquire(timeout=settings.job_timeout_seconds)
        if not acquired:
            self._set_status(job_id, "failed", error="Timed out waiting for a free worker slot")
            return
        try:
            self._execute(job_id)
        except Exception as exc:  # belt-and-braces: never leave a job stuck "running"
            log.exception("job %s crashed", job_id)
            self._set_status(job_id, "failed", error=str(exc), finished_at=_now())
        finally:
            self.slots.release()

    def _execute(self, job_id: str):
        session = SessionLocal()
        try:
            job = self._get(session, job_id)
            if not job:
                return
            objects: list[str] = json.loads(job.objects_json)
            completed: list[str] = json.loads(job.completed_objects_json)
            job.status = "running"
            job.started_at = job.started_at or _now()
            session.commit()
        finally:
            session.close()

        sf = SalesforceBulkClient()
        storage = MinioStorage()
        clickhouse = ClickHouseStore()
        today = date.today().isoformat()

        for obj in objects:
            if obj in completed:
                continue

            if self._should_stop(job_id):
                return  # pause()/cancel() already set the final status

            self._set_current(job_id, obj)
            self._upsert_job_object(job_id, obj, state="InProgress", started_at=_now())

            try:
                sf_job_id, csv_text = sf.run(obj, self._records_per_object(job_id))
                key = f"salesforce/{obj}/{self._org_id(job_id)}/{today}/{obj}_{job_id}.csv"
                storage.put_text(key, csv_text)

                landed = storage.get_text(key)  # load from MinIO, per the acceptance criteria
                rows = csv_rows(obj, landed, organisation_id=self._org_id(job_id))
                clickhouse.insert_rows(obj, rows)

                self._upsert_job_object(
                    job_id, obj, state="JobComplete", salesforce_job_id=sf_job_id,
                    rows=len(rows), minio_key=key, finished_at=_now(), error=None,
                )
                completed.append(obj)
                self._advance(job_id, obj, completed, rows_added=len(rows))

            except Exception as exc:
                log.exception("job %s object %s failed", job_id, obj)
                self._upsert_job_object(job_id, obj, state="Failed", error=str(exc), finished_at=_now())
                self._set_status(job_id, "failed", error=f"{obj}: {exc}", finished_at=_now(), current_object=None)
                return

        self._set_status(job_id, "completed", finished_at=_now(), current_object=None)

    # ---------- small persistence helpers ----------------------------------------------------
    def _should_stop(self, job_id: str) -> bool:
        session = SessionLocal()
        try:
            job = self._get(session, job_id)
            if not job:
                return True
            if job.status == "pausing":
                job.status = "paused"
                job.current_object = None
                session.commit()
                return True
            if job.status == "cancelling":
                job.status = "cancelled"
                job.current_object = None
                job.finished_at = _now()
                session.commit()
                return True
            return False
        finally:
            session.close()

    def _records_per_object(self, job_id: str) -> int:
        session = SessionLocal()
        try:
            return self._get(session, job_id).records_per_object
        finally:
            session.close()

    def _org_id(self, job_id: str) -> str:
        session = SessionLocal()
        try:
            return self._get(session, job_id).organization_id
        finally:
            session.close()

    def _set_current(self, job_id: str, obj: str):
        self._set_status(job_id, "running", current_object=obj)

    def _upsert_job_object(self, job_id: str, obj: str, **fields) -> JobObject:
        session = SessionLocal()
        try:
            row = session.query(JobObject).filter_by(job_id=job_id, object_name=obj).one_or_none()
            if not row:
                row = JobObject(job_id=job_id, object_name=obj)
                session.add(row)
            for k, v in fields.items():
                setattr(row, k, v)
            session.commit()
            session.refresh(row)
            return row
        finally:
            session.close()

    def _advance(self, job_id: str, obj: str, completed: list[str], rows_added: int):
        session = SessionLocal()
        try:
            job = self._get(session, job_id)
            job.completed_objects_json = json.dumps(completed)
            job.processed_objects = len(completed)
            job.total_rows = (job.total_rows or 0) + rows_added
            session.commit()
        finally:
            session.close()


manager = JobManager()
