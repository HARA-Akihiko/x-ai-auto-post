from datetime import datetime, timezone
from enum import StrEnum

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class DraftStatus(StrEnum):
    PENDING = "pending"
    GENERATED = "generated"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    SKIPPED = "skipped"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


def enum_type(cls):
    return Enum(cls, values_callable=lambda values: [item.value for item in values],
                native_enum=False, create_constraint=True)


class CredentialMixin:
    id: Mapped[int] = mapped_column(primary_key=True)
    access_token: Mapped[str] = mapped_column(Text)
    refresh_token: Mapped[str | None] = mapped_column(Text)
    id_token: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    scope: Mapped[str] = mapped_column(Text, default="")
    client_id: Mapped[str] = mapped_column(String(255), default="")
    host_id: Mapped[str] = mapped_column(String(255), default="")


class ChatGPTCredential(CredentialMixin, Base):
    __tablename__ = "chatgpt_credentials"


class XCredential(CredentialMixin, Base):
    __tablename__ = "x_credentials"


class OAuthState(Base):
    __tablename__ = "oauth_states"
    state_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(16))
    verifier: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Source(Base):
    __tablename__ = "sources"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    url: Mapped[str] = mapped_column(Text, unique=True)
    priority: Mapped[int] = mapped_column(Integer, default=10)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class Article(Base):
    __tablename__ = "articles"
    id: Mapped[int] = mapped_column(primary_key=True)
    url: Mapped[str] = mapped_column(Text)
    canonical_url: Mapped[str] = mapped_column(Text, unique=True)
    title: Mapped[str] = mapped_column(Text)
    source_name: Mapped[str] = mapped_column(String(100))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    summary: Mapped[str] = mapped_column(Text, default="")
    content_hash: Mapped[str] = mapped_column(String(64), unique=True)
    score: Mapped[int] = mapped_column(Integer, default=0)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PostSchedule(Base):
    __tablename__ = "post_schedules"
    id: Mapped[int] = mapped_column(primary_key=True)
    time: Mapped[str] = mapped_column(String(5), unique=True)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Tokyo")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class PostDraft(Base):
    __tablename__ = "post_drafts"
    id: Mapped[int] = mapped_column(primary_key=True)
    slot: Mapped[str] = mapped_column(String(128), unique=True)
    text: Mapped[str] = mapped_column(Text, default="")
    content_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    status: Mapped[DraftStatus] = mapped_column(enum_type(DraftStatus), default=DraftStatus.PENDING)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    publishing_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(64))


class DraftSource(Base):
    __tablename__ = "draft_sources"
    draft_id: Mapped[int] = mapped_column(ForeignKey("post_drafts.id"), primary_key=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"), primary_key=True)


class PublishedPost(Base):
    __tablename__ = "published_posts"
    id: Mapped[int] = mapped_column(primary_key=True)
    draft_id: Mapped[int] = mapped_column(ForeignKey("post_drafts.id"), unique=True)
    x_post_id: Mapped[str] = mapped_column(String(64), unique=True)
    text: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64), unique=True)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class JobRun(Base):
    __tablename__ = "job_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    slot: Mapped[str] = mapped_column(String(128), unique=True)
    status: Mapped[JobStatus] = mapped_column(enum_type(JobStatus), default=JobStatus.QUEUED)
    draft_id: Mapped[int | None] = mapped_column(ForeignKey("post_drafts.id"))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(64))
    duration: Mapped[int | None] = mapped_column(Integer)
