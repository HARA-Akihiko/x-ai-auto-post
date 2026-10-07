import asyncio
import hashlib
import json
import logging
from contextlib import asynccontextmanager

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import object_session

from app.models import (
    DraftSource, DraftStatus, JobRun, JobStatus, PostDraft, PublishedPost, utcnow,
)
from app.services.articles import candidates
from app.services.auth import ServiceError
from app.services.posts import post_hash, validate_post


_test_locks = {}
PUBLISHER_LOCK = 723816352
MAX_RECONCILE_ATTEMPTS = 3
logger = logging.getLogger(__name__)


def _slot_key(slot):
    return int.from_bytes(hashlib.sha256(slot.encode()).digest()[:8], "big", signed=True)


class _Lease:
    def __init__(self, connection=None):
        self.connection = connection

    def check(self):
        if self.connection is not None:
            if self.connection.invalidated or self.connection.closed:
                raise ServiceError("publisher_lock_lost")
            try:
                self.connection.execute(text("SELECT 1"))
                self.connection.commit()
            except Exception as exc:
                self.connection.invalidate()
                raise ServiceError("publisher_lock_lost") from exc


@asynccontextmanager
async def _locks(session_factory, slot):
    with session_factory() as session:
        engine = session.get_bind()
    if engine.dialect.name != "postgresql":
        # SQLite is only a test backend; production db.get_engine requires PostgreSQL.
        lock = _test_locks.setdefault((engine, asyncio.get_running_loop()), asyncio.Lock())
        if lock.locked():
            yield None
            return
        async with lock:
            yield _Lease()
        return
    connection = engine.connect()
    acquired = []
    try:
        for key in (PUBLISHER_LOCK, _slot_key(slot)):
            if not connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key}):
                yield None
                return
            acquired.append(key)
        connection.commit()
        yield _Lease(connection)
    finally:
        # Never reconnect an invalidated connection: its former backend already
        # released the session locks and a replacement backend would own none.
        if not connection.closed and not connection.invalidated:
            try:
                connection.rollback()
                for key in reversed(acquired):
                    connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
                connection.commit()
            except Exception:
                connection.invalidate()
        connection.close()


def _job(session, slot):
    job = session.scalar(select(JobRun).where(JobRun.slot == slot))
    if job is None:
        job = JobRun(slot=slot, status=JobStatus.QUEUED)
        session.add(job)
        session.flush()
    return job


def _finish(job, status, error_code=None):
    job.status = status
    job.error_code = error_code
    job.finished_at = utcnow()
    job.duration = 0
    started = job.started_at
    if started is not None:
        if started.tzinfo is None:
            started = started.replace(tzinfo=job.finished_at.tzinfo)
        job.duration = max(0, int((job.finished_at - started).total_seconds()))
    session = object_session(job)
    article_id = None
    if session is not None and job.draft_id is not None:
        article_id = session.scalar(select(DraftSource.article_id).where(
            DraftSource.draft_id == job.draft_id,
        ).limit(1))
    record = {
        "event": "job_completed", "job_run_id": job.id, "slot": job.slot,
        "article_id": article_id, "draft_id": job.draft_id,
        "status": status.value, "duration": job.duration, "error_code": error_code,
    }
    if session is not None:
        session.info.setdefault("posting_completion_logs", []).append(record)
    else:
        logger.info(json.dumps(record, ensure_ascii=False))


def _commit(session):
    try:
        session.commit()
    except Exception:
        session.info.pop("posting_completion_logs", None)
        raise
    for record in session.info.pop("posting_completion_logs", []):
        logger.info(json.dumps(record, ensure_ascii=False))


async def _generate(
    slot, settings, session_factory, collector, analyzer, generator, lease, *, preview_only=False,
):
    with session_factory() as session:
        existing = session.scalar(select(PostDraft).where(PostDraft.slot == slot))
        if existing is not None:
            return existing
        if session.scalar(select(PostDraft.id).where(
            PostDraft.status == DraftStatus.PUBLISHING,
        ).limit(1)) is not None:
            return None
        job = _job(session, slot)
        if job.status in {JobStatus.SUCCESS, JobStatus.SKIPPED, JobStatus.FAILED, JobStatus.UNCERTAIN}:
            return None
        job.status = JobStatus.RUNNING
        job.error_code = "preview_generation" if preview_only else None
        job.started_at = job.started_at or utcnow()
        _commit(session)
        try:
            await collector.collect(session)
            available = candidates(session, settings)
            if not available and getattr(settings, "web_search_enabled", False):
                await collector.search_fallback(session, analyzer.responses_client)
                available = candidates(session, settings)
            article = await analyzer.analyze(available)
            if article is None:
                _finish(job, JobStatus.SKIPPED)
                _commit(session)
                return None
            text_value = await generator.generate(article)
            validate_post(text_value)
            lease.check()
            draft = PostDraft(
                slot=slot, text=text_value, content_hash=post_hash(text_value), status=DraftStatus.GENERATED,
            )
            with session.begin_nested():
                session.add(draft)
                session.flush()
                session.add(DraftSource(draft_id=draft.id, article_id=article.id))
                job.draft_id = draft.id
                session.flush()
            if preview_only:
                _finish(job, JobStatus.SUCCESS)
            _commit(session)
            session.refresh(draft)
            session.expunge(draft)
            return draft
        except IntegrityError:
            session.rollback()
            job = _job(session, slot)
            _finish(job, JobStatus.SKIPPED, "duplicate_post")
            _commit(session)
            return None
        except Exception as exc:
            session.rollback()
            job = _job(session, slot)
            _finish(job, JobStatus.FAILED, getattr(exc, "error_code", "generation_failed"))
            _commit(session)
            return None


def _uncertain(session_factory, draft_id, code):
    with session_factory() as session:
        draft = session.get(PostDraft, draft_id)
        if draft.reconcile_attempts >= MAX_RECONCILE_ATTEMPTS:
            code = "publish_manual_review"
        draft.error_code = code
        job = _job(session, draft.slot)
        job.draft_id = draft.id
        _finish(job, JobStatus.UNCERTAIN, code)
        _commit(session)


def _record_success(session_factory, draft_id, x_id):
    with session_factory() as session:
        draft = session.get(PostDraft, draft_id)
        existing = session.scalar(select(PublishedPost).where(PublishedPost.draft_id == draft_id))
        if existing is None:
            session.add(PublishedPost(
                draft_id=draft.id, x_post_id=x_id, text=draft.text,
                content_hash=draft.content_hash or post_hash(draft.text),
            ))
        draft.status = DraftStatus.PUBLISHED
        draft.error_code = None
        job = _job(session, draft.slot)
        job.draft_id = draft.id
        _finish(job, JobStatus.SUCCESS)
        _commit(session)


async def _publish(draft_id, settings, session_factory, x_service, lease):
    with session_factory() as session:
        draft = session.get(PostDraft, draft_id)
        if draft is None or draft.status in {DraftStatus.PUBLISHED, DraftStatus.FAILED, DraftStatus.CANCELLED}:
            return
        was_publishing = draft.status == DraftStatus.PUBLISHING
        if was_publishing:
            if draft.reconcile_attempts >= MAX_RECONCILE_ATTEMPTS:
                if draft.error_code != "publish_manual_review":
                    draft.error_code = "publish_manual_review"
                    job = _job(session, draft.slot)
                    job.draft_id = draft.id
                    _finish(job, JobStatus.UNCERTAIN, draft.error_code)
                    _commit(session)
                return
            # Persist the budget before any external GET, including failures
            # and process interruptions, so restarting cannot reset the limit.
            draft.reconcile_attempts += 1
            _commit(session)
        else:
            try:
                validate_post(draft.text)
                lease.check()
            except Exception as exc:
                draft.status = DraftStatus.FAILED
                draft.error_code = getattr(exc, "error_code", "invalid_post")
                _finish(_job(session, draft.slot), JobStatus.FAILED, draft.error_code)
                _commit(session)
                return
            # A generated draft must not claim an article already sent or in flight.
            article_ids = select(DraftSource.article_id).where(DraftSource.draft_id == draft.id)
            conflict = session.scalar(select(PostDraft.id).join(
                DraftSource, DraftSource.draft_id == PostDraft.id,
            ).where(
                DraftSource.article_id.in_(article_ids), PostDraft.id != draft.id,
                PostDraft.status.in_([DraftStatus.PUBLISHING, DraftStatus.PUBLISHED]),
            ).limit(1))
            if conflict is not None:
                draft.status = DraftStatus.CANCELLED
                _finish(_job(session, draft.slot), JobStatus.SKIPPED, "duplicate_article")
                _commit(session)
                return
            draft.status = DraftStatus.PUBLISHING
            draft.publishing_at = utcnow()
            job = _job(session, draft.slot)
            job.draft_id = draft.id
            job.status = JobStatus.UNCERTAIN
            job.started_at = job.started_at or utcnow()
            job.error_code = "publish_in_flight"
            _commit(session)
        text_value, publishing_at = draft.text, draft.publishing_at
    if was_publishing:
        try:
            lease.check()
            x_id = await x_service.reconcile(text_value, publishing_at)
        except Exception as exc:
            _uncertain(session_factory, draft_id, getattr(exc, "error_code", "reconcile_failed"))
            return
        if x_id is None:
            _uncertain(session_factory, draft_id, "publish_unconfirmed")
            return
    else:
        try:
            lease.check()
            x_id = await x_service.publish(text_value)
        except Exception as exc:
            code = getattr(exc, "error_code", "x_publish_uncertain")
            if code in {"x_rejected", "x_unauthorized", "x_rate_limited"}:
                with session_factory() as session:
                    draft = session.get(PostDraft, draft_id)
                    draft.status = DraftStatus.FAILED
                    draft.error_code = code
                    _finish(_job(session, draft.slot), JobStatus.FAILED, code)
                    _commit(session)
            else:
                _uncertain(session_factory, draft_id, code)
            return
    # If this transaction fails, the separately committed PUBLISHING state
    # survives. The next invocation can only reconcile, never POST again.
    lease.check()
    _record_success(session_factory, draft_id, x_id)


async def generate_draft(slot, settings, session_factory, collector, analyzer, generator):
    async with _locks(session_factory, slot) as lease:
        if lease is None:
            return None
        draft = await _generate(
            slot, settings, session_factory, collector, analyzer, generator, lease, preview_only=True,
        )
        if draft is not None and draft.status == DraftStatus.GENERATED:
            with session_factory() as session:
                job = _job(session, slot)
                if job.status != JobStatus.SUCCESS or job.draft_id != draft.id:
                    job.draft_id = draft.id
                    _finish(job, JobStatus.SUCCESS)
                    _commit(session)
        return draft


async def publish_draft(draft_id, settings, session_factory, x_service):
    with session_factory() as session:
        draft = session.get(PostDraft, draft_id)
        if draft is None:
            return
        slot = draft.slot
    async with _locks(session_factory, slot) as lease:
        if lease is not None:
            await _publish(draft_id, settings, session_factory, x_service, lease)


async def publish_post_job(slot, settings, session_factory, collector, analyzer, generator, x_service):
    async with _locks(session_factory, slot) as lease:
        if lease is None:
            return
        # Reconcile all unresolved external writes before spending feed/AI calls.
        with session_factory() as session:
            pending = session.scalars(select(PostDraft.id).where(
                PostDraft.status == DraftStatus.PUBLISHING,
            ).order_by(PostDraft.id)).all()
        for draft_id in pending:
            await _publish(draft_id, settings, session_factory, x_service, lease)
        with session_factory() as session:
            if session.scalar(select(PostDraft.id).where(
                PostDraft.status == DraftStatus.PUBLISHING,
            ).limit(1)) is not None:
                return
            draft = session.scalar(select(PostDraft).where(PostDraft.slot == slot))
            if draft is not None:
                draft_id = draft.id
            else:
                draft_id = None
        if draft_id is None:
            draft = await _generate(slot, settings, session_factory, collector, analyzer, generator, lease)
            if draft is None:
                return
            draft_id = draft.id
        await _publish(draft_id, settings, session_factory, x_service, lease)
