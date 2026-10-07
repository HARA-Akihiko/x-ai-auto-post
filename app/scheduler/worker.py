import asyncio
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.core.db import get_session_factory
from app.models import DraftStatus, JobRun, JobStatus, PostDraft, PostSchedule
from app.scheduler.jobs import publish_draft, publish_post_job
from app.services.articles import ArticleAnalyzer, ArticleCollector
from app.services.posts import PostGenerator, XPostService


logger = logging.getLogger(__name__)
CATCHUP_GRACE = timedelta(minutes=5)


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
                    slots.append(f"scheduled:{schedule.id}:{instant.isoformat(timespec='minutes')}")
    return sorted(set(slots))


def _expired_schedule(slot, now):
    if not slot.startswith("scheduled:"):
        return False
    try:
        planned = datetime.fromisoformat(slot.split(":", 2)[2])
        if planned.tzinfo is None:
            return True
        return now.astimezone(timezone.utc) - planned > CATCHUP_GRACE
    except (IndexError, ValueError):
        return True


async def worker_tick(now, settings, session_factory, collector, analyzer, generator, x_service):
    with session_factory() as session:
        unresolved = session.scalars(select(PostDraft.id).where(
            PostDraft.status == DraftStatus.PUBLISHING,
        ).order_by(PostDraft.id)).all()
    for draft_id in unresolved:
        await publish_draft(draft_id, settings, session_factory, x_service)
    with session_factory() as session:
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
        slots = []
        for job in queued:
            if _expired_schedule(job.slot, now):
                job.status = JobStatus.SKIPPED
                job.error_code = "schedule_expired"
                job.finished_at = now
            else:
                slots.append(job.slot)
        session.commit()
    for slot in slots:
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
        while True:
            try:
                await worker_tick(
                    datetime.now(timezone.utc), settings, session_factory,
                    collector, analyzer, generator, x_service,
                )
            except Exception:
                # Never log exception messages, HTTP bodies or tokens.
                logger.error("worker_tick_failed")
            await asyncio.sleep(60)


def main():
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
