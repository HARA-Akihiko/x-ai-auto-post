import asyncio
import os
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, delete, select, text
from sqlalchemy.orm import sessionmaker

from app.models import Base, DraftStatus, JobRun, JobStatus, PostDraft, PostSchedule, utcnow
from app.scheduler import worker


def schedule(time="08:00", zone="Asia/Tokyo", enabled=True):
    return SimpleNamespace(id=1, time=time, timezone=zone, enabled=enabled)


def test_explicit_timezone_and_max_catchup_grace():
    assert worker.due_slots(datetime(2026, 10, 7, 23, 4, tzinfo=timezone.utc), [schedule()]) == [
        "scheduled:2026-10-07T23:00+00:00",
    ]
    assert worker.due_slots(datetime(2026, 10, 7, 23, 6, tzinfo=timezone.utc), [schedule()]) == []
    assert worker.due_slots(datetime(2026, 10, 7, 22, 59, tzinfo=timezone.utc), [schedule()]) == []
    assert worker.due_slots(datetime(2026, 10, 7, 23, 0, tzinfo=timezone.utc),
                            [schedule(enabled=False)]) == []


def test_dst_nonexistent_times_are_not_scheduled():
    assert worker.due_slots(
        datetime(2026, 3, 8, 7, 30, tzinfo=timezone.utc), [schedule("02:30", "America/New_York")],
    ) == []


def test_recreated_schedule_rows_and_equivalent_timezones_share_stable_slot():
    original = schedule()
    recreated = schedule()
    recreated.id = 99
    equivalent = schedule("23:00", "UTC")
    equivalent.id = 100
    now = datetime(2026, 10, 7, 23, 2, tzinfo=timezone.utc)
    expected = ["scheduled:2026-10-07T23:00+00:00"]
    assert worker.due_slots(now, [original]) == expected
    assert worker.due_slots(now, [recreated]) == expected
    assert worker.due_slots(now, [original, recreated, equivalent]) == expected


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
        for tick in range(2):
            await worker.worker_tick(
                datetime(2026, 10, 7, 23, 2, tzinfo=timezone.utc),
                SimpleNamespace(), factory, None, None, None, None,
            )
            if tick == 0:
                with factory() as session:
                    session.execute(delete(PostSchedule))
                    session.add(PostSchedule(id=99, time="08:00", timezone="Asia/Tokyo"))
                    session.commit()
    try:
        asyncio.run(run())
        assert seen == ["manual:requested", "scheduled:2026-10-07T23:00+00:00"]
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
        session.add(JobRun(slot="scheduled:2026-10-07T23:00+00:00", status=JobStatus.RUNNING))
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


def test_queued_draft_request_publishes_existing_text_without_generation():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session:
        draft = PostDraft(slot="generated:one", text="推論が改善", status=DraftStatus.GENERATED)
        session.add(draft)
        session.flush()
        request_slot = f"draft:{draft.id}"
        session.add(JobRun(slot=request_slot, draft_id=draft.id, status=JobStatus.QUEUED))
        session.commit()
        draft_id = draft.id
    collector = SimpleNamespace(collect=AsyncMock())
    analyzer = SimpleNamespace(analyze=AsyncMock())
    generator = SimpleNamespace(generate=AsyncMock())
    service = SimpleNamespace(publish=AsyncMock(return_value="123"), reconcile=AsyncMock())
    try:
        asyncio.run(worker.worker_tick(
            datetime.now(timezone.utc), SimpleNamespace(), factory,
            collector, analyzer, generator, service,
        ))
        collector.collect.assert_not_awaited()
        analyzer.analyze.assert_not_awaited()
        generator.generate.assert_not_awaited()
        service.publish.assert_awaited_once_with("推論が改善")
        with factory() as session:
            assert session.get(PostDraft, draft_id).status == DraftStatus.PUBLISHED
            assert session.scalar(select(JobRun).where(JobRun.slot == request_slot)).status == JobStatus.SUCCESS
    finally:
        engine.dispose()


def test_draft_request_uncertainty_tracks_reconciliation_without_repost():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session:
        draft = PostDraft(slot="generated:uncertain", text="推論が改善", status=DraftStatus.GENERATED)
        session.add(draft)
        session.flush()
        request_slot = f"draft:{draft.id}"
        session.add(JobRun(slot=request_slot, draft_id=draft.id, status=JobStatus.QUEUED))
        session.commit()
    service = SimpleNamespace(publish=AsyncMock(side_effect=TimeoutError()),
                              reconcile=AsyncMock(return_value=None))

    async def tick():
        await worker.worker_tick(
            datetime.now(timezone.utc), SimpleNamespace(), factory, None, None, None, service,
        )

    try:
        asyncio.run(tick())
        with factory() as session:
            assert session.scalar(select(JobRun).where(JobRun.slot == request_slot)).status == JobStatus.UNCERTAIN
        asyncio.run(tick())
        service.reconcile.return_value = "456"
        asyncio.run(tick())
        service.publish.assert_awaited_once()
        assert service.reconcile.await_count == 2
        with factory() as session:
            assert session.scalar(select(JobRun).where(JobRun.slot == request_slot)).status == JobStatus.SUCCESS
    finally:
        engine.dispose()


def test_initialize_schedules_once_preserves_admin_disabled_empty_schedule():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    settings = SimpleNamespace(post_times="08:00,13:00,19:00", timezone="Asia/Tokyo")
    try:
        worker.initialize_schedules(settings, factory)
        worker.initialize_schedules(settings, factory)
        with factory() as session:
            schedules = session.scalars(select(PostSchedule).order_by(PostSchedule.time)).all()
            assert [schedule.time for schedule in schedules] == ["08:00", "13:00", "19:00"]
            assert all(schedule.timezone == "Asia/Tokyo" for schedule in schedules)
            assert len(session.scalars(select(JobRun)).all()) == 1
            session.execute(delete(PostSchedule))
            session.commit()
        worker.initialize_schedules(settings, factory)
        with factory() as session:
            assert session.scalar(select(PostSchedule.id)) is None
    finally:
        engine.dispose()


@pytest.mark.integration
@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not configured")
def test_postgres_schedule_initialization_transaction_lock_and_idempotency():
    schema = "test_schedule_" + uuid.uuid4().hex
    engine = create_engine(os.environ["TEST_DATABASE_URL"], hide_parameters=True)
    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    scoped = engine.execution_options(schema_translate_map={None: schema})
    factory = sessionmaker(scoped, expire_on_commit=False)
    settings = SimpleNamespace(post_times="08:00,19:00", timezone="Asia/Tokyo")
    try:
        Base.metadata.create_all(scoped)
        with engine.begin() as connection:
            connection.execute(text("SELECT pg_advisory_xact_lock(:key)"),
                               {"key": worker.SCHEDULE_INIT_LOCK})
            worker.initialize_schedules(settings, factory)
            with factory() as session:
                assert session.scalar(select(PostSchedule.id)) is None
        worker.initialize_schedules(settings, factory)
        worker.initialize_schedules(settings, factory)
        with factory() as session:
            assert len(session.scalars(select(PostSchedule)).all()) == 2
            assert len(session.scalars(select(JobRun)).all()) == 1
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        engine.dispose()


def test_standalone_worker_uses_apscheduler_with_explicit_nonoverlap_policy(monkeypatch):
    from app.services import auth, openai

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    configured = SimpleNamespace(timezone="Asia/Tokyo", post_times="08:00,19:00")
    captured = {}

    class Scheduler:
        def __init__(self, **kwargs):
            captured["timezone"] = str(kwargs["timezone"])

        def add_job(self, function, trigger, **kwargs):
            captured["function"] = function
            captured["trigger"] = trigger
            captured.update(kwargs)

        def start(self):
            with factory() as session:
                assert len(session.scalars(select(PostSchedule)).all()) == 2
            captured["started"] = True
            asyncio.get_running_loop().call_soon(asyncio.current_task().cancel)

        def shutdown(self, **kwargs):
            captured["shutdown"] = kwargs

    monkeypatch.setattr(worker, "get_settings", lambda: configured)
    monkeypatch.setattr(worker, "get_session_factory", lambda: factory)
    monkeypatch.setattr(worker, "AsyncIOScheduler", Scheduler)
    monkeypatch.setattr(auth, "OAuthService", lambda *args: object())
    monkeypatch.setattr(openai, "ResponsesClient", lambda *args: object())
    try:
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(worker.run_worker())
        assert captured["timezone"] == "Asia/Tokyo"
        assert captured["trigger"] == "interval"
        assert captured["minutes"] == 1
        assert captured["max_instances"] == 1
        assert captured["coalesce"] is True
        assert captured["misfire_grace_time"] == 300
        assert captured["next_run_time"].tzinfo is not None
        assert captured["started"]
        assert captured["shutdown"] == {"wait": False}
    finally:
        engine.dispose()
