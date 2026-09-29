"""Covers the review's headline bug: a job stuck "running" after a crash could
never be resumed (resume() only accepted "paused"), even though the per-object
checkpoint needed to resume it already existed. See app/services/worker.py.
"""
import json
import time

import pytest

from app.services import worker as worker_module
from app.db import SessionLocal
from app.models.job import IngestionJob
from app.services.worker import RESUMABLE, JobManager


class FakeSalesforce:
    """Records every object it's asked to fetch, so a test can assert a completed
    object is never re-fetched after a resume."""

    calls: list[str] = []
    delay: float = 0.0

    def __init__(self, *a, **kw):
        pass

    def run(self, obj: str, records: int):
        from app.services.schema import SCHEMAS

        FakeSalesforce.calls.append(obj)
        if FakeSalesforce.delay:
            time.sleep(FakeSalesforce.delay)
        def dummy(f, i):
            if f.sf == "Id":
                return f"A{i}"
            base = f.type.removeprefix("Nullable(").removesuffix(")")
            return {"Float64": "1.5", "Bool": "true", "Date": "2026-01-01"}.get(base, "x")

        header = ",".join(f.sf for f in SCHEMAS[obj].fields)
        body = "\n".join(",".join(dummy(f, i) for f in SCHEMAS[obj].fields) for i in range(records))
        return f"sf-job-{obj}", f"{header}\n{body}\n"


class FakeStorage:
    def __init__(self, *a, **kw):
        self._data: dict[str, str] = {}

    def put_text(self, key, text):
        self._data[key] = text
        return f"s3://bucket/{key}"

    def get_text(self, key):
        return self._data[key]


class FakeClickHouse:
    def __init__(self, *a, **kw):
        self.inserted: dict[str, int] = {}

    def insert_rows(self, obj, rows):
        self.inserted[obj] = self.inserted.get(obj, 0) + len(rows)


@pytest.fixture
def manager(client, monkeypatch):
    # ``client`` fixture runs the app lifespan first, so tables exist.
    monkeypatch.setattr(worker_module, "SalesforceBulkClient", FakeSalesforce)
    monkeypatch.setattr(worker_module, "MinioStorage", FakeStorage)
    monkeypatch.setattr(worker_module, "ClickHouseStore", FakeClickHouse)
    FakeSalesforce.calls = []
    FakeSalesforce.delay = 0.0
    return JobManager()


def _wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _status(job_id):
    session = SessionLocal()
    try:
        return session.get(IngestionJob, job_id).status
    finally:
        session.close()


def test_full_run_completes_and_inserts_every_object(manager):
    job_id = manager.start("org1", ["Accounts", "Contacts"], 5)
    assert _wait_for(lambda: _status(job_id) == "completed")

    session = SessionLocal()
    job = session.get(IngestionJob, job_id)
    assert json.loads(job.completed_objects_json) == ["Accounts", "Contacts"]
    assert job.total_rows == 10
    session.close()


def test_pause_then_resume_does_not_redo_finished_objects(manager):
    FakeSalesforce.delay = 0.15  # slow enough to pause mid-second-object
    job_id = manager.start("org1", ["Accounts", "Contacts", "Leads"], 3)

    assert _wait_for(lambda: len(FakeSalesforce.calls) >= 1)
    assert manager.pause(job_id) == "ok"
    assert _wait_for(lambda: _status(job_id) == "paused")

    session = SessionLocal()
    job = session.get(IngestionJob, job_id)
    completed_at_pause = json.loads(job.completed_objects_json)
    session.close()
    assert 1 <= len(completed_at_pause) <= 2  # stopped between objects, not mid-object

    FakeSalesforce.delay = 0.0
    assert manager.resume(job_id) == "ok"
    assert _wait_for(lambda: _status(job_id) == "completed")

    # every object fetched exactly once across the whole pause/resume cycle
    assert sorted(FakeSalesforce.calls) == sorted(["Accounts", "Contacts", "Leads"])


def test_crash_recovery_lets_a_stuck_running_job_resume(manager):
    """Simulates the exact bug reported: the process dies while a job is "running"."""
    session = SessionLocal()
    session.add(
        IngestionJob(
            id="crashed-job",
            organization_id="org1",
            status="running",  # as if the worker thread just vanished
            objects_json=json.dumps(["Accounts", "Contacts"]),
            completed_objects_json=json.dumps(["Accounts"]),  # Accounts had already landed
            current_object="Contacts",
        )
    )
    session.commit()
    session.close()

    # Before recovery, resume correctly refuses a "running" job (it might not really be dead).
    assert manager.resume("crashed-job") == "conflict"

    recovered = manager.startup_recovery()
    assert "crashed-job" in recovered
    assert _status("crashed-job") == "interrupted"
    assert "interrupted" in RESUMABLE

    assert manager.resume("crashed-job") == "ok"
    assert _wait_for(lambda: _status("crashed-job") == "completed")
    # Accounts was already checkpointed as done, so only Contacts should be re-fetched.
    assert FakeSalesforce.calls == ["Contacts"]


def test_cancel_cannot_revive_a_completed_job(manager):
    job_id = manager.start("org1", ["Accounts"], 2)
    assert _wait_for(lambda: _status(job_id) == "completed")
    assert manager.cancel(job_id) == "conflict"
    assert _status(job_id) == "completed"
