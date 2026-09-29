"""Covers the review's headline bug: a job stuck "running" after a crash could
never be resumed (resume() only accepted "paused"), even though the per-object
checkpoint needed to resume it already existed. See app/services/worker.py.
"""
import json
import time
import threading
import uuid

import pytest

from app.services import worker as worker_module
from app.db import SessionLocal
from app.models.job import IngestionJob, JobObject
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
    manager = JobManager()
    yield manager
    assert _wait_for(lambda: not manager.threads), "worker threads did not exit"


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


@pytest.mark.parametrize("action, expected", [("pause", "paused"), ("cancel", "cancelled")])
def test_queued_job_does_not_run_after_stop(manager, action, expected):
    manager.slots = threading.Semaphore(0)
    job_id = manager.start("org", ["Accounts"], 1)
    thread = manager.threads[job_id]
    try:
        assert getattr(manager, action)(job_id) == "ok"
    finally:
        manager.slots.release()
    thread.join(5)
    assert not thread.is_alive()
    assert _status(job_id) == expected
    assert FakeSalesforce.calls == []


def test_resume_queued_job_reuses_waiting_thread(manager):
    manager.slots = threading.Semaphore(0)
    job_id = manager.start("org", ["Accounts"], 1)
    thread = manager.threads[job_id]
    try:
        assert manager.pause(job_id) == "ok"
        assert manager.resume(job_id) == "ok"
        assert manager.threads[job_id] is thread
    finally:
        manager.slots.release()
    thread.join(5)
    assert _status(job_id) == "completed"
    assert FakeSalesforce.calls == ["Accounts"]


@pytest.mark.parametrize("first", ["pause", "cancel"])
@pytest.mark.parametrize("objects", [["Accounts"], ["Accounts", "Contacts"]])
def test_repeated_stop_during_fetch_is_honored(manager, monkeypatch, first, objects):
    entered, release = threading.Event(), threading.Event()

    class BlockingSalesforce(FakeSalesforce):
        def run(self, obj, records):
            entered.set()
            assert release.wait(5)
            return super().run(obj, records)

    monkeypatch.setattr(worker_module, "SalesforceBulkClient", BlockingSalesforce)
    job_id = manager.start("org", objects, 1)
    try:
        assert entered.wait(5)
        assert getattr(manager, first)(job_id) == "ok"
        assert manager.cancel(job_id) == "ok"
        assert manager.cancel(job_id) == "ok"
    finally:
        release.set()
    assert _wait_for(lambda: _status(job_id) == "cancelled")
    assert FakeSalesforce.calls == ["Accounts"]
    assert manager.cancel(job_id) == "ok"


def test_resume_while_paused_worker_is_exiting(manager, monkeypatch):
    exiting, release = threading.Event(), threading.Event()
    execute = manager._execute

    def stop_then_exit(job_id):
        execute(job_id)
        if _status(job_id) == "paused":
            exiting.set()
            assert release.wait(5)

    class PauseAfterFirst(FakeSalesforce):
        def run(self, obj, records):
            if obj == "Accounts":
                assert manager.pause(job_id) == "ok"
            return super().run(obj, records)

    monkeypatch.setattr(manager, "_execute", stop_then_exit)
    monkeypatch.setattr(worker_module, "SalesforceBulkClient", PauseAfterFirst)
    # Hold the manager lock until the closure has received the job id.
    with manager.lock:
        job_id = manager.start("org", ["Accounts", "Contacts"], 1)
    try:
        assert exiting.wait(5)
        assert manager.resume(job_id) == "ok"
    finally:
        release.set()
    assert _wait_for(lambda: _status(job_id) == "completed")
    assert FakeSalesforce.calls == ["Accounts", "Contacts"]


@pytest.mark.parametrize("automatic", [False, True])
def test_pending_job_recovered_after_restart(manager, monkeypatch, automatic):
    job_id = uuid.uuid4().hex
    with SessionLocal() as session:
        session.add(IngestionJob(id=job_id, organization_id="org", status="pending", objects_json='["Accounts"]'))
        session.commit()
    monkeypatch.setattr(worker_module.settings, "auto_resume_on_startup", automatic)
    assert job_id in manager.startup_recovery()
    if not automatic:
        assert _status(job_id) == "interrupted"
        assert manager.resume(job_id) == "ok"
    assert _wait_for(lambda: _status(job_id) == "completed")
    assert FakeSalesforce.calls == ["Accounts"]


def test_legacy_split_checkpoint_is_reconciled_on_resume(manager):
    job_id = uuid.uuid4().hex
    with SessionLocal() as session:
        session.add(IngestionJob(id=job_id, organization_id="org", status="interrupted",
                                 objects_json='["Accounts", "Contacts"]'))
        session.flush()
        session.add(JobObject(job_id=job_id, object_name="Accounts", state="JobComplete", rows=5))
        session.commit()
    assert manager.resume(job_id) == "ok"
    assert _wait_for(lambda: _status(job_id) == "completed")
    assert FakeSalesforce.calls == ["Contacts"]
    with SessionLocal() as session:
        job = session.get(IngestionJob, job_id)
        assert job.processed_objects == 2
        assert job.total_rows == 1005


def test_checkpoint_and_progress_roll_back_together(manager, monkeypatch):
    from sqlalchemy import event
    from sqlalchemy.orm import Session

    # Direct execution keeps the injected crash entirely on the test thread.
    monkeypatch.setattr(manager, "_spawn", lambda _: None)
    job_id = manager.start("org", ["Accounts"], 1)

    class Crash(BaseException):
        pass

    def crash_before_commit(session):
        if any(isinstance(row, JobObject) and row.state == "JobComplete" for row in session.dirty):
            raise Crash()

    event.listen(Session, "before_commit", crash_before_commit)
    try:
        with pytest.raises(Crash):
            manager._execute(job_id)
    finally:
        event.remove(Session, "before_commit", crash_before_commit)
    with SessionLocal() as session:
        job = session.get(IngestionJob, job_id)
        checkpoint = session.query(JobObject).filter_by(job_id=job_id).one()
        assert checkpoint.state == "InProgress"
        assert json.loads(job.completed_objects_json) == []
        assert job.processed_objects == job.total_rows == 0
    manager.startup_recovery()
    manager.resume(job_id)
    manager._execute(job_id)
    with SessionLocal() as session:
        job = session.get(IngestionJob, job_id)
        checkpoint = session.query(JobObject).filter_by(job_id=job_id).one()
        assert checkpoint.state == "JobComplete"
        assert job.status == "completed"
        assert job.total_rows == job.processed_objects == 1


def test_duplicate_objects_are_normalized(manager):
    job_id = manager.start("org", ["Accounts", "Accounts"], 2)
    assert _wait_for(lambda: _status(job_id) == "completed")
    with SessionLocal() as session:
        job = session.get(IngestionJob, job_id)
        assert json.loads(job.objects_json) == ["Accounts"]
        assert job.processed_objects == 1
        assert job.total_rows == 2
