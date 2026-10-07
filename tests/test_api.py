from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.models import Base, DraftStatus, JobRun, PostDraft, PostSchedule


def make_client():
    from app.main import create_app

    settings = Settings(
        _env_file=None,
        database_url="postgresql+psycopg://localhost/test",
        token_encryption_key=Fernet.generate_key().decode(),
        admin_api_token="test-admin-" + "a" * 32,
    )
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    return TestClient(create_app(settings, factory)), factory, {
        "Authorization": "Bearer " + settings.admin_api_token.get_secret_value()
    }


def test_management_routes_require_authentication():
    client, _, _ = make_client()
    with client:
        assert client.get("/health").status_code == 200
        for path in ("/articles", "/sources", "/posts", "/schedules", "/jobs/runs",
                     "/auth/chatgpt/status", "/auth/x/login"):
            assert client.get(path).status_code == 401
        assert client.post("/jobs/run-now", json={"idempotency_key": "one"}).status_code == 401


def test_schedule_replacement_is_validated_and_persisted():
    client, factory, headers = make_client()
    with client:
        invalid = {"times": ["25:00"], "timezone": "Asia/Tokyo"}
        assert client.put("/schedules", headers=headers, json=invalid).status_code == 422
        assert client.put("/schedules", headers=headers, json={
            "times": ["08:00", "13:00", "19:00"], "timezone": "Asia/Tokyo"
        }).status_code == 200
        with factory() as session:
            assert len(session.scalars(select(PostSchedule)).all()) == 3
        assert len(client.get("/schedules", headers=headers).json()) == 3
        assert client.put("/schedules", headers=headers, json={
            "times": ["08:00", "08:00"], "timezone": "Asia/Tokyo"
        }).status_code == 422


def test_manual_job_is_queued_once_and_not_executed_by_api():
    client, factory, headers = make_client()
    with client:
        for _ in range(2):
            response = client.post("/jobs/run-now", headers=headers,
                                   json={"idempotency_key": "same-click"})
            assert response.status_code == 202
            assert response.json()["status"] == "queued"
        with factory() as session:
            runs = session.scalars(select(JobRun)).all()
            assert len(runs) == 1
            assert runs[0].slot == "manual:same-click"


def test_errors_and_oauth_status_never_expose_credentials():
    client, _, headers = make_client()
    with client:
        response = client.get("/auth/chatgpt/status", headers=headers)
        assert response.status_code == 200
        assert "access_token" not in response.text
        assert "refresh_token" not in response.text
        response = client.get("/auth/x/callback?state=invalid&code=secret")
        assert response.status_code in (400, 401)
        assert "secret" not in response.text


def test_draft_publish_only_queues_and_missing_draft_is_rejected():
    client, factory, headers = make_client()
    with factory() as session:
        draft = PostDraft(slot="preview:one", text="draft preview", status=DraftStatus.GENERATED)
        session.add(draft)
        session.commit()
        draft_id = draft.id
    with client:
        assert client.post("/posts/9999/publish", headers=headers).status_code == 404
        for _ in range(2):
            assert client.post(f"/posts/{draft_id}/publish", headers=headers).status_code == 202
        with factory() as session:
            assert session.get(PostDraft, draft_id).status == DraftStatus.GENERATED
            runs = session.scalars(select(JobRun)).all()
            assert len(runs) == 1
            assert runs[0].draft_id == draft_id


def test_generate_with_no_sources_does_not_call_external_ai():
    client, _, headers = make_client()
    with client:
        response = client.post("/posts/generate", headers=headers)
        assert response.status_code == 409
        assert response.json()["error_code"] == "no_candidates"
