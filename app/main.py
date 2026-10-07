import hmac
import logging
import uuid
from contextlib import asynccontextmanager
from typing import Annotated
from zoneinfo import ZoneInfo

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.core.config import Settings, get_settings
from app.core.db import get_session_factory
from app.models import Article, JobRun, JobStatus, PostDraft, PostSchedule, Source

bearer = HTTPBearer(auto_error=False)
logger = logging.getLogger(__name__)


class ScheduleUpdate(BaseModel):
    times: list[str] = Field(min_length=1, max_length=24)
    timezone: str = "Asia/Tokyo"

    @field_validator("times")
    @classmethod
    def valid_times(cls, value: list[str]) -> list[str]:
        Settings.valid_times(",".join(value))
        return value

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ValueError, KeyError) as exc:
            raise ValueError("invalid timezone") from exc
        return value


class RunRequest(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


def serialize(row, fields):
    return {field: getattr(row, field) for field in fields}


def create_app(settings=None, session_factory=None, http_client=None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application):
        from app.services.auth import OAuthService
        from app.services.openai import ResponsesClient
        from app.services.articles import ArticleAnalyzer, ArticleCollector
        from app.services.posts import PostGenerator

        application.state.settings = settings or get_settings()
        application.state.factory = session_factory or get_session_factory()
        client = http_client or httpx.AsyncClient(timeout=30, follow_redirects=False)
        application.state.auth = OAuthService(
            application.state.settings, application.state.factory, client
        )
        responses = ResponsesClient(application.state.settings, application.state.auth, client)
        application.state.collector = ArticleCollector(application.state.settings, client)
        application.state.analyzer = ArticleAnalyzer(responses)
        application.state.generator = PostGenerator(application.state.settings, responses)
        try:
            yield
        finally:
            if http_client is None:
                await client.aclose()

    application = FastAPI(title="AI development article scheduler", lifespan=lifespan,
                          docs_url=None, redoc_url=None, openapi_url=None)

    async def require_admin(
        request: Request,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ):
        expected = request.app.state.settings.admin_api_token.get_secret_value()
        if credentials is None or not hmac.compare_digest(
            credentials.credentials.encode(), expected.encode()
        ):
            raise HTTPException(401, "unauthorized", headers={"WWW-Authenticate": "Bearer"})

    @application.middleware("http")
    async def no_store(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @application.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, exc):
        logger.error("status=failed error_code=database_error")
        return JSONResponse(status_code=503, content={"error_code": "database_error"})

    # The callback is authenticated by its one-use OAuth state, not an API bearer header.
    @application.get("/auth/{provider}/callback")
    async def callback(provider: str, state: str, code: str, request: Request):
        check_provider(provider)
        from app.services.auth import ServiceError

        try:
            await request.app.state.auth.callback(provider, state, code)
        except ServiceError as exc:
            return JSONResponse(status_code=400, content={"error_code": exc.error_code})
        return {"status": "connected"}

    def check_provider(provider):
        if provider not in ("chatgpt", "x"):
            raise HTTPException(404, "unknown provider")

    protected = [Depends(require_admin)]

    @application.get("/health")
    def health(request: Request):
        with request.app.state.factory() as session:
            session.execute(text("SELECT 1"))
        return {"status": "ok"}

    @application.get("/auth/{provider}/status", dependencies=protected)
    def auth_status(provider: str, request: Request):
        check_provider(provider)
        return request.app.state.auth.status(provider)

    @application.get("/auth/{provider}/login", dependencies=protected)
    def login(provider: str, request: Request):
        check_provider(provider)
        from app.services.auth import ServiceError

        try:
            return {"authorization_url": request.app.state.auth.login(provider)}
        except ServiceError as exc:
            return JSONResponse(status_code=400, content={"error_code": exc.error_code})

    @application.delete("/auth/{provider}", dependencies=protected)
    def disconnect(provider: str, request: Request):
        check_provider(provider)
        request.app.state.auth.disconnect(provider)
        return {"status": "disconnected"}

    @application.get("/sources", dependencies=protected)
    def sources(request: Request):
        with request.app.state.factory() as session:
            return [serialize(row, ("id", "name", "url", "priority", "enabled"))
                    for row in session.scalars(select(Source).order_by(Source.id)).all()]

    @application.get("/articles", dependencies=protected)
    def articles(request: Request, limit: int = 50):
        with request.app.state.factory() as session:
            rows = session.scalars(select(Article).order_by(Article.collected_at.desc())
                                   .limit(max(1, min(limit, 100)))).all()
            return [serialize(row, ("id", "canonical_url", "title", "source_name",
                                    "published_at", "summary", "score", "collected_at"))
                    for row in rows]

    @application.post("/articles/collect", dependencies=protected)
    async def collect(request: Request):
        with request.app.state.factory() as session:
            rows = await request.app.state.collector.collect(session)
            session.commit()
        return {"collected": len(rows)}

    @application.get("/schedules", dependencies=protected)
    def schedules(request: Request):
        with request.app.state.factory() as session:
            return [serialize(row, ("id", "time", "timezone", "enabled"))
                    for row in session.scalars(select(PostSchedule).order_by(PostSchedule.time))]

    @application.put("/schedules", dependencies=protected)
    def update_schedules(body: ScheduleUpdate, request: Request):
        with request.app.state.factory() as session, session.begin():
            session.execute(delete(PostSchedule))
            session.add_all([PostSchedule(time=time, timezone=body.timezone)
                             for time in body.times])
        return {"times": body.times, "timezone": body.timezone}

    @application.get("/posts", dependencies=protected)
    def posts(request: Request, limit: int = 50):
        with request.app.state.factory() as session:
            return [serialize(row, ("id", "slot", "text", "status", "created_at",
                                    "reconcile_attempts", "error_code"))
                    for row in session.scalars(select(PostDraft).order_by(PostDraft.id.desc())
                                               .limit(max(1, min(limit, 100))))]

    @application.post("/posts/generate", dependencies=protected)
    async def generate(request: Request):
        from app.scheduler.jobs import generate_draft
        from app.services.auth import ServiceError

        try:
            draft = await generate_draft(
                f"generated:{uuid.uuid4()}", request.app.state.settings,
                request.app.state.factory, request.app.state.collector,
                request.app.state.analyzer, request.app.state.generator,
            )
        except ServiceError as exc:
            return JSONResponse(status_code=400, content={"error_code": exc.error_code})
        if draft is None:
            return JSONResponse(status_code=409, content={"error_code": "no_candidates"})
        return serialize(draft, ("id", "text", "status"))

    def enqueue(request, slot, draft_id=None):
        with request.app.state.factory() as session:
            existing = session.scalar(select(JobRun).where(JobRun.slot == slot))
            if existing:
                return serialize(existing, ("id", "slot", "status"))
            run = JobRun(slot=slot, draft_id=draft_id, status=JobStatus.QUEUED)
            session.add(run)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                run = session.scalar(select(JobRun).where(JobRun.slot == slot))
                if run is None:
                    raise
            return serialize(run, ("id", "slot", "status"))

    @application.post("/posts/{draft_id}/publish", dependencies=protected, status_code=202)
    def publish(draft_id: int, request: Request):
        with request.app.state.factory() as session:
            if session.get(PostDraft, draft_id) is None:
                raise HTTPException(404, "draft not found")
        return enqueue(request, f"draft:{draft_id}", draft_id)

    @application.post("/jobs/run-now", dependencies=protected, status_code=202)
    def run_now(body: RunRequest, request: Request):
        return enqueue(request, f"manual:{body.idempotency_key}")

    @application.get("/jobs/runs", dependencies=protected)
    def runs(request: Request, limit: int = 50):
        with request.app.state.factory() as session:
            return [serialize(row, ("id", "slot", "status", "draft_id", "started_at",
                                    "finished_at", "duration", "error_code"))
                    for row in session.scalars(select(JobRun).order_by(JobRun.id.desc())
                                               .limit(max(1, min(limit, 100))))]

    return application


app = create_app()
