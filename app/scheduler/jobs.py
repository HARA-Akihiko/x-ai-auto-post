import asyncio
import hashlib
from contextlib import asynccontextmanager

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.models import (
    DraftSource, DraftStatus, JobRun, JobStatus, PostDraft, PublishedPost, utcnow,
)
from app.services.articles import candidates
from app.services.auth import ServiceError
from app.services.posts import post_hash, validate_post


_test_locks = {}
PUBLISHER_LOCK = 723816352


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
    started = job.started_at
    if started is not None:
        if started.tzinfo is None:
            started = started.replace(tzinfo=job.finished_at.tzinfo)
        job.duration = max(0, int((job.finished_at - started).total_seconds()))


async def _generate(slot, settings, session_factory, collector, analyzer, generator, lease):
    with session_factory() as session:
        existing = session.scalar(select(PostDraft).where(PostDraft.slot == slot))
        if existing is not None:
            return existing
        job = _job(session, slot)
        if job.status in {JobStatus.SUCCESS, JobStatus.SKIPPED, JobStatus.FAILED, JobStatus.UNCERTAIN}:
            return None
        job.status = JobStatus.RUNNING
        job.started_at = job.started_at or utcnow()
        session.commit()
        try:
            await collector.collect(session)
            article = await analyzer.analyze(candidates(session, settings))
            if article is None:
                _finish(job, JobStatus.SKIPPED)
                session.commit()
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
            session.commit()
            session.refresh(draft)
            session.expunge(draft)
            return draft
        except IntegrityError:
            session.rollback()
            job = _job(session, slot)
            _finish(job, JobStatus.SKIPPED, "duplicate_post")
            session.commit()
            return None
        except Exception as exc:
            session.rollback()
            job = _job(session, slot)
            _finish(job, JobStatus.FAILED, getattr(exc, "error_code", "generation_failed"))
            session.commit()
            return None


def _uncertain(session_factory, draft_id, code):
    with session_factory() as session:
        draft = session.get(PostDraft, draft_id)
        draft.error_code = code
        job = _job(session, draft.slot)
        job.draft_id = draft.id
        _finish(job, JobStatus.UNCERTAIN, code)
        session.commit()


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
        session.commit()


async def _publish(draft_id, settings, session_factory, x_service, lease):
    with session_factory() as session:
        draft = session.get(PostDraft, draft_id)
        if draft is None or draft.status in {DraftStatus.PUBLISHED, DraftStatus.FAILED, DraftStatus.CANCELLED}:
            return
        was_publishing = draft.status == DraftStatus.PUBLISHING
        if not was_publishing:
            try:
                validate_post(draft.text)
                lease.check()
            except Exception as exc:
                draft.status = DraftStatus.FAILED
                draft.error_code = getattr(exc, "error_code", "invalid_post")
                _finish(_job(session, draft.slot), JobStatus.FAILED, draft.error_code)
                session.commit()
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
                session.commit()
                return
            draft.status = DraftStatus.PUBLISHING
            draft.publishing_at = utcnow()
            job = _job(session, draft.slot)
            job.draft_id = draft.id
            job.status = JobStatus.UNCERTAIN
            job.started_at = job.started_at or utcnow()
            job.error_code = "publish_in_flight"
            session.commit()
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
            if code == "x_rejected":
                with session_factory() as session:
                    draft = session.get(PostDraft, draft_id)
                    draft.status = DraftStatus.FAILED
                    draft.error_code = code
                    _finish(_job(session, draft.slot), JobStatus.FAILED, code)
                    session.commit()
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
        return await _generate(slot, settings, session_factory, collector, analyzer, generator, lease)


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
