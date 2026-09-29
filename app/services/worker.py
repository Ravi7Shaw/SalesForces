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
        self.lock = threading.RLock()
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
            orphans = session.query(IngestionJob).filter(IngestionJob.status.in_(["pending", "running", "pausing", "cancelling"])).all()
            for job in orphans:
                log.warning("recovering orphaned %s job %s", job.status, job.id)
                job.status = "interrupted"
                job.current_object = None
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
                    objects_json=json.dumps(list(dict.fromkeys(objects))),
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
        with self.lock:
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
        with self.lock:
            session = SessionLocal()
            try:
                job = self._get(session, job_id)
                if not job:
                    return NOT_FOUND
                if job.status in {"cancelled", "cancelling"}:
                    return OK
                if job.status in TERMINAL:
                    return CONFLICT
                job.status = "cancelling" if job.status in {"running", "pausing"} else "cancelled"
                if job.status == "cancelled":
                    job.finished_at = _now()
                session.commit()
                return OK
            finally:
                session.close()

    def resume(self, job_id: str) -> str:
        with self.lock:
            session = SessionLocal()
            try:
                job = self._get(session, job_id)
                if not job:
                    return NOT_FOUND
                if job.status not in RESUMABLE:
                    return CONFLICT
                job.status = "pending"
                job.error = None
                job.finished_at = None
                session.commit()
            finally:
                session.close()
            self._spawn(job_id)
            return OK

    def _spawn(self, job_id: str):
        with self.lock:
            # A paused queued job can still have a thread waiting for a slot.
            existing = self.threads.get(job_id)
            if existing and existing.is_alive():
                return
            t = threading.Thread(target=self._run, args=(job_id,), daemon=True, name=f"job-{job_id[:8]}")
            self.threads[job_id] = t
            t.start()

    # ---------- execution ----------------------------------------------------
    def _run(self, job_id: str):
        acquired = False
        try:
            acquired = self.slots.acquire(timeout=settings.job_timeout_seconds)
            if not acquired:
                with self.lock, SessionLocal() as session:
                    job = self._get(session, job_id)
                    if job and job.status == "pending":
                        job.status = "failed"
                        job.error = "Timed out waiting for a free worker slot"
                        job.finished_at = _now()
                        session.commit()
                return
            self._execute(job_id)
        except Exception as exc:
            log.exception("job %s crashed", job_id)
            self._fail(job_id, str(exc))
        finally:
            if acquired:
                self.slots.release()
            with self.lock:
                self.threads.pop(job_id, None)
                # Resume may have arrived after this thread acknowledged pause,
                # but before it exited. Start the replacement only now.
                with SessionLocal() as session:
                    job = self._get(session, job_id)
                    pending = job is not None and job.status == "pending"
                if pending:
                    self._spawn(job_id)

    def _execute(self, job_id: str):
        with self.lock, SessionLocal() as session:
            job = self._get(session, job_id)
            if not job or job.status != "pending":
                return
            objects = list(dict.fromkeys(json.loads(job.objects_json)))
            completed = list(dict.fromkeys(json.loads(job.completed_objects_json)))
            # Repair checkpoints written by versions that committed the object
            # and aggregate progress separately. Keep legacy JSON-only entries.
            checkpoints = session.query(JobObject).filter_by(job_id=job_id, state="JobComplete").all()
            for checkpoint in checkpoints:
                if checkpoint.object_name not in completed:
                    completed.append(checkpoint.object_name)
                    job.total_rows += checkpoint.rows
            job.objects_json = json.dumps(objects)
            job.completed_objects_json = json.dumps(completed)
            job.processed_objects = len(completed)
            job.status = "running"
            job.started_at = job.started_at or _now()
            job.finished_at = None
            session.commit()

        sf = SalesforceBulkClient()
        storage = MinioStorage()
        clickhouse = ClickHouseStore()
        today = date.today().isoformat()

        for obj in objects:
            if obj in completed:
                continue

            with self.lock:
                if self._should_stop(job_id):
                    return
                self._set_current(job_id, obj)
                self._upsert_job_object(job_id, obj, state="InProgress", started_at=_now(), finished_at=None, error=None)

            try:
                sf_job_id, csv_text = sf.run(obj, self._records_per_object(job_id))
                key = f"salesforce/{obj}/{self._org_id(job_id)}/{today}/{obj}_{job_id}.csv"
                storage.put_text(key, csv_text)

                landed = storage.get_text(key)  # load from MinIO, per the acceptance criteria
                rows = csv_rows(obj, landed, organisation_id=self._org_id(job_id))
                clickhouse.insert_rows(obj, rows)

                self._complete_object(job_id, obj, sf_job_id, key, len(rows))
                completed.append(obj)

            except Exception as exc:
                log.exception("job %s object %s failed", job_id, obj)
                self._upsert_job_object(job_id, obj, state="Failed", error=str(exc), finished_at=_now())
                self._fail(job_id, f"{obj}: {exc}")
                return

        with self.lock:
            if not self._should_stop(job_id):
                self._set_status(job_id, "completed", finished_at=_now(), current_object=None)

    def _fail(self, job_id: str, error: str):
        with self.lock, SessionLocal() as session:
            job = self._get(session, job_id)
            if job and job.status in {"pending", "running"}:
                job.status = "failed"
                job.error = error
                job.finished_at = _now()
                job.current_object = None
                session.commit()
            else:
                self._should_stop(job_id)

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
            return job.status != "running"
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

    def _complete_object(self, job_id: str, obj: str, sf_job_id: str, key: str, rows: int):
        # Object state and aggregate progress must become durable together.
        with self.lock, SessionLocal() as session:
            job = self._get(session, job_id)
            checkpoint = session.query(JobObject).filter_by(job_id=job_id, object_name=obj).one()
            completed = json.loads(job.completed_objects_json)
            if obj not in completed:
                completed.append(obj)
                job.total_rows += rows
            checkpoint.state = "JobComplete"
            checkpoint.salesforce_job_id = sf_job_id
            checkpoint.rows = rows
            checkpoint.minio_key = key
            checkpoint.finished_at = _now()
            checkpoint.error = None
            job.completed_objects_json = json.dumps(completed)
            job.processed_objects = len(completed)
            session.commit()


manager = JobManager()
