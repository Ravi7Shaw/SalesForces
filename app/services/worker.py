import json
import threading
import time
import uuid
from datetime import datetime, timezone

from app.db import SessionLocal
from app.models.job import IngestionJob
from app.services.clickhouse import ClickHouseStore
from app.services.salesforce import SalesforceBulkClient, csv_rows
from app.services.storage import MinioStorage


class JobManager:
    def __init__(self):
        self.threads: dict[str, threading.Thread] = {}
        self.lock = threading.Lock()

    def start(self, organization_id: str, objects: list[str], records_per_object: int) -> str:
        job_id = uuid.uuid4().hex
        session = SessionLocal()
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
        session.close()
        self._spawn(job_id)
        return job_id

    def _spawn(self, job_id: str):
        with self.lock:
            thread = self.threads.get(job_id)
            if thread and thread.is_alive():
                return
            thread = threading.Thread(target=self.run, args=(job_id,), daemon=True, name=f"ingest-{job_id[:8]}")
            self.threads[job_id] = thread
            thread.start()

    def pause(self, job_id: str):
        session = SessionLocal()
        job = session.get(IngestionJob, job_id)
        if not job:
            session.close()
            return False
        if job.status in {"pending", "running"}:
            job.status = "paused"
            session.commit()
        session.close()
        return True

    def resume(self, job_id: str):
        session = SessionLocal()
        job = session.get(IngestionJob, job_id)
        if not job:
            session.close()
            return False
        if job.status == "paused":
            job.status = "running"
            session.commit()
            session.close()
            self._spawn(job_id)
            return True
        session.close()
        return False

    def cancel(self, job_id: str):
        session = SessionLocal()
        job = session.get(IngestionJob, job_id)
        if not job:
            session.close()
            return False
        job.status = "cancelled"
        job.finished_at = datetime.now(timezone.utc)
        session.commit()
        session.close()
        return True

    def run(self, job_id: str):
        session = SessionLocal()
        job = session.get(IngestionJob, job_id)
        if not job or job.status == "cancelled":
            session.close()
            return
        if job.status == "pending":
            job.status = "running"
            job.started_at = datetime.now(timezone.utc)
            session.commit()
        session.close()

        sf = SalesforceBulkClient()
        storage = MinioStorage()
        clickhouse = ClickHouseStore()

        try:
            session = SessionLocal()
            job = session.get(IngestionJob, job_id)
            objects = json.loads(job.objects_json)
            completed = set(json.loads(job.completed_objects_json or "[]"))
            session.close()

            for obj in objects:
                session = SessionLocal()
                job = session.get(IngestionJob, job_id)
                current_status = job.status
                session.close()

                while current_status == "paused":
                    time.sleep(0.25)
                    session = SessionLocal()
                    job = session.get(IngestionJob, job_id)
                    current_status = job.status
                    session.close()

                if current_status == "cancelled":
                    return
                if obj in completed:
                    continue

                session = SessionLocal()
                job = session.get(IngestionJob, job_id)
                job.current_object = obj
                session.commit()
                session.close()

                _, raw = sf.run(obj, job.records_per_object)
                date_part = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                key = f"salesforce/{obj.lower()}/{job.organization_id}/{date_part}/{job_id}.csv"
                storage.put_text(key, raw)

                rows = csv_rows(obj, raw, job.organization_id)
                clickhouse.insert_rows(obj, rows)

                session = SessionLocal()
                job = session.get(IngestionJob, job_id)
                completed.add(obj)
                job.completed_objects_json = json.dumps(sorted(completed))
                job.processed_objects = len(completed)
                job.total_rows = (job.total_rows or 0) + len(rows)
                job.current_object = None
                session.commit()
                session.close()

            clickhouse.create_views()
            session = SessionLocal()
            job = session.get(IngestionJob, job_id)
            if job and job.status != "cancelled":
                job.status = "completed"
                job.current_object = None
                job.finished_at = datetime.now(timezone.utc)
                session.commit()
            session.close()
        except Exception as exc:
            session = SessionLocal()
            job = session.get(IngestionJob, job_id)
            if job:
                job.status = "failed"
                job.error = str(exc)
                job.finished_at = datetime.now(timezone.utc)
                session.commit()
            session.close()


manager = JobManager()
