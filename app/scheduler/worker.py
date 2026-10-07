import asyncio
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.core.db import get_session_factory
from app.models import DraftStatus, JobRun, JobStatus, PostDraft, PostSchedule
from app.scheduler.jobs import _commit, _finish, publish_draft, publish_post_job
from app.services.articles import ArticleAnalyzer, ArticleCollector
from app.services.posts import PostGenerator, XPostService


logger = logging.getLogger(__name__)
CATCHUP_GRACE = timedelta(minutes=5)
SCHEDULE_INIT_LOCK = 723816353
SCHEDULE_INIT_SLOT = "system:schedules:initialized"


def initialize_schedules(settings, session_factory):
    if not getattr(settings, "post_times", None):
        return
    with session_factory() as session:
        if session.get_bind().dialect.name == "postgresql":
            if not session.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"),
                                  {"key": SCHEDULE_INIT_LOCK}):
                return
        if session.scalar(select(JobRun.id).where(JobRun.slot == SCHEDULE_INIT_SLOT)) is not None:
            return
        if session.scalar(select(PostSchedule.id).limit(1)) is None:
            for time in settings.post_times.split(","):
                session.add(PostSchedule(time=time, timezone=settings.timezone, enabled=True))
        now = datetime.now(timezone.utc)
        session.add(JobRun(
            slot=SCHEDULE_INIT_SLOT, status=JobStatus.SUCCESS,
            started_at=now, finished_at=now, duration=0,
        ))
        _commit(session)


def due_slots(now, schedules):
    if now.tzinfo is None:
        raise ValueError("scheduler requires an aware datetime")
    slots = []
    for schedule in schedules:
        if not schedule.enabled:
            continue
        zone = ZoneInfo(schedule.timezone)
        local = now.astimezone(zone)
        hour, minute = map(int, schedule.time.split(":"))
        for day in (local.date(), (local - timedelta(days=1)).date()):
            # Test both folds; skip imaginary wall-clock times during DST gaps.
            seen = set()
            for fold in (0, 1):
                planned = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone, fold=fold)
                instant = planned.astimezone(timezone.utc)
                if instant in seen or instant.astimezone(zone).replace(tzinfo=None) != planned.replace(tzinfo=None):
                    continue
                seen.add(instant)
                if timedelta(0) <= now.astimezone(timezone.utc) - instant <= CATCHUP_GRACE:
                    slots.append(f"scheduled:{instant.isoformat(timespec='minutes')}")
    return sorted(set(slots))


def _expired_schedule(slot, now):
    if not slot.startswith("scheduled:"):
        return False
    try:
        value = slot.removeprefix("scheduled:")
        try:
            planned = datetime.fromisoformat(value)
        except ValueError:
            # Recognize durable requests queued by the older row-id key format.
            planned = datetime.fromisoformat(value.split(":", 1)[1])
        if planned.tzinfo is None:
            return True
        return now.astimezone(timezone.utc) - planned > CATCHUP_GRACE
    except (IndexError, ValueError):
        return True


def _sync_publish_request(session, job):
    draft = session.get(PostDraft, job.draft_id) if job.draft_id is not None else None
    if draft is None:
        _finish(job, JobStatus.FAILED, "draft_not_found")
    elif draft.status == DraftStatus.PUBLISHED:
        _finish(job, JobStatus.SUCCESS)
    elif draft.status == DraftStatus.PUBLISHING:
        _finish(job, JobStatus.UNCERTAIN, draft.error_code or "publish_in_flight")
    elif draft.status == DraftStatus.FAILED:
        _finish(job, JobStatus.FAILED, draft.error_code or "draft_failed")
    elif draft.status == DraftStatus.CANCELLED:
        _finish(job, JobStatus.SKIPPED, draft.error_code or "draft_cancelled")
    elif job.status == JobStatus.RUNNING:
        # A competing worker may own the publisher lock. Leave the durable
        # request queued rather than pretending an unattempted send succeeded.
        job.status = JobStatus.QUEUED


async def worker_tick(now, settings, session_factory, collector, analyzer, generator, x_service):
    initialize_schedules(settings, session_factory)
    with session_factory() as session:
        unresolved = session.scalars(select(PostDraft.id).where(
            PostDraft.status == DraftStatus.PUBLISHING,
        ).order_by(PostDraft.id)).all()
    for draft_id in unresolved:
        await publish_draft(draft_id, settings, session_factory, x_service)
    with session_factory() as session:
        requests = session.scalars(select(JobRun).where(
            JobRun.slot.startswith("draft:"),
            JobRun.status.in_([JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.UNCERTAIN]),
        )).all()
        for job in requests:
            _sync_publish_request(session, job)
        _commit(session)
        if session.scalar(select(PostDraft.id).where(
            PostDraft.status == DraftStatus.PUBLISHING,
        ).limit(1)) is not None:
            return
        schedules = session.scalars(select(PostSchedule).where(PostSchedule.enabled.is_(True))).all()
        for slot in due_slots(now, schedules):
            if session.scalar(select(JobRun.id).where(JobRun.slot == slot)) is None:
                try:
                    with session.begin_nested():
                        session.add(JobRun(slot=slot, status=JobStatus.QUEUED))
                        session.flush()
                except IntegrityError:
                    pass
        queued = session.scalars(select(JobRun).where(
            JobRun.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
        ).order_by(JobRun.id)).all()
        requests = []
        for job in queued:
            if job.slot.startswith("generated:") or job.error_code == "preview_generation":
                # Preview work can be interrupted before its completion commit;
                # it is never a durable instruction to send an X post.
                continue
            if _expired_schedule(job.slot, now):
                _finish(job, JobStatus.SKIPPED, "schedule_expired")
            else:
                requests.append((job.id, job.slot, job.draft_id))
        _commit(session)
    for job_id, slot, draft_id in requests:
        if slot.startswith("draft:"):
            with session_factory() as session:
                job = session.get(JobRun, job_id)
                job.status = JobStatus.RUNNING
                job.started_at = job.started_at or now
                _commit(session)
            try:
                if draft_id is not None:
                    await publish_draft(draft_id, settings, session_factory, x_service)
            finally:
                with session_factory() as session:
                    _sync_publish_request(session, session.get(JobRun, job_id))
                    _commit(session)
        else:
            await publish_post_job(slot, settings, session_factory, collector, analyzer, generator, x_service)


async def run_worker():
    from app.services.auth import OAuthService
    from app.services.openai import ResponsesClient

    settings = get_settings()
    session_factory = get_session_factory()
    async with httpx.AsyncClient(follow_redirects=False) as client:
        auth = OAuthService(settings, session_factory, client)
        responses = ResponsesClient(settings, auth, client)
        collector = ArticleCollector(settings, client)
        analyzer = ArticleAnalyzer(responses)
        generator = PostGenerator(settings, responses)
        x_service = XPostService(settings, auth, client)
        initialize_schedules(settings, session_factory)
        scheduler = AsyncIOScheduler(timezone=ZoneInfo(settings.timezone))

        async def tick():
            try:
                await worker_tick(
                    datetime.now(timezone.utc), settings, session_factory,
                    collector, analyzer, generator, x_service,
                )
            except Exception:
                # Never log exception messages, HTTP bodies or tokens.
                logger.error("worker_tick_failed")

        scheduler.add_job(
            tick, "interval", minutes=1, id="database-posting-jobs",
            max_instances=1, coalesce=True, misfire_grace_time=300,
            next_run_time=datetime.now(timezone.utc),
        )
        scheduler.start()
        try:
            await asyncio.Event().wait()
        finally:
            scheduler.shutdown(wait=False)


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
