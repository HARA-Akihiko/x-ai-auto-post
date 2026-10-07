import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.models import Base, DraftStatus, JobRun, JobStatus, PostDraft, PostSchedule, utcnow
from app.scheduler import worker


def schedule(time="08:00", zone="Asia/Tokyo", enabled=True):
    return SimpleNamespace(id=1, time=time, timezone=zone, enabled=enabled)


def test_explicit_timezone_and_max_catchup_grace():
    assert worker.due_slots(datetime(2026, 10, 7, 23, 4, tzinfo=timezone.utc), [schedule()]) == [
        "scheduled:1:2026-10-07T23:00+00:00",
    ]
    assert worker.due_slots(datetime(2026, 10, 7, 23, 6, tzinfo=timezone.utc), [schedule()]) == []
    assert worker.due_slots(datetime(2026, 10, 7, 22, 59, tzinfo=timezone.utc), [schedule()]) == []
    assert worker.due_slots(datetime(2026, 10, 7, 23, 0, tzinfo=timezone.utc),
                            [schedule(enabled=False)]) == []


def test_dst_nonexistent_times_are_not_scheduled():
    assert worker.due_slots(
        datetime(2026, 3, 8, 7, 30, tzinfo=timezone.utc), [schedule("02:30", "America/New_York")],
    ) == []


def test_worker_persists_scheduled_slots_and_processes_manual_queue(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session:
        session.add(PostSchedule(time="08:00", timezone="Asia/Tokyo"))
        session.add(JobRun(slot="manual:requested", status=JobStatus.QUEUED))
        session.commit()
    seen = []

    async def process(slot, *args):
        seen.append(slot)
        with factory() as session:
            run = session.scalar(select(JobRun).where(JobRun.slot == slot))
            run.status = JobStatus.SUCCESS
            session.commit()

    monkeypatch.setattr(worker, "publish_post_job", process)

    async def run():
        for _ in range(2):
            await worker.worker_tick(
                datetime(2026, 10, 7, 23, 2, tzinfo=timezone.utc),
                SimpleNamespace(), factory, None, None, None, None,
            )
    try:
        asyncio.run(run())
        assert seen == ["manual:requested", "scheduled:1:2026-10-07T23:00+00:00"]
        with factory() as session:
            assert len(session.scalars(select(JobRun)).all()) == 2
    finally:
        engine.dispose()


def test_worker_reconciles_restart_uncertainty_before_new_jobs(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session:
        session.add(PostDraft(slot="old", text="old", status=DraftStatus.PUBLISHING, publishing_at=utcnow()))
        session.add(JobRun(slot="manual:new", status=JobStatus.QUEUED))
        session.commit()
    reconcile = AsyncMock()
    process = AsyncMock()
    monkeypatch.setattr(worker, "publish_draft", reconcile)
    monkeypatch.setattr(worker, "publish_post_job", process)
    try:
        asyncio.run(worker.worker_tick(
            datetime.now(timezone.utc), SimpleNamespace(), factory, None, None, None, None,
        ))
        reconcile.assert_awaited_once()
        process.assert_not_awaited()
    finally:
        engine.dispose()


def test_restart_does_not_publish_expired_scheduled_jobs(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session:
        session.add(JobRun(slot="scheduled:1:2026-10-07T23:00+00:00", status=JobStatus.RUNNING))
        session.add(JobRun(slot="manual:old-request", status=JobStatus.QUEUED))
        session.commit()
    process = AsyncMock()
    monkeypatch.setattr(worker, "publish_post_job", process)
    try:
        asyncio.run(worker.worker_tick(
            datetime(2026, 10, 7, 23, 10, tzinfo=timezone.utc),
            SimpleNamespace(), factory, None, None, None, None,
        ))
        assert process.await_count == 1
        assert process.call_args.args[0] == "manual:old-request"
        with factory() as session:
            stale = session.scalar(select(JobRun).where(JobRun.slot.like("scheduled:%")))
            assert stale.status == JobStatus.SKIPPED
            assert stale.error_code == "schedule_expired"
    finally:
        engine.dispose()
