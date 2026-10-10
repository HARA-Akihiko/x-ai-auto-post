import asyncio
import base64
import hashlib
import os
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from app.core.config import CHATGPT_ISSUER, CHATGPT_RESOURCE, CHATGPT_TOKEN_URL, Settings
from app.models import Base, ChatGPTCredential, OAuthState, XCredential, utcnow
from app.services import auth as auth_module
from app.services.auth import OAuthService, ServiceError

HOST_ID = "urn:uuid:123e4567-e89b-42d3-a456-426614174000"
ISSUED_CLIENT_ID = "oaiapp_issued"
GRANTED_SCOPES = ["chatgpt.tokens.use.direct", "email", "offline_access", "openid", "profile",
                  "resource.invoke"]


@pytest.fixture
def settings():
    return Settings(
        _env_file=None,
        database_url="sqlite://",
        token_encryption_key=Fernet.generate_key().decode(),
        admin_api_token="a" * 32,
        chatgpt_host_id=HOST_ID,
        chatgpt_model="configured-model",
        x_client_id="registered-x",
    )


@pytest.fixture
def factory():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    yield sessionmaker(engine, expire_on_commit=False)
    engine.dispose()


def run(coro):
    return asyncio.run(coro)


def service(settings, factory, handler):
    return OAuthService(settings, factory, httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def state_for(auth, provider="x"):
    return parse_qs(urlparse(auth.login(provider)).query)["state"][0]


def x_token_payload(**overrides):
    return {"access_token": "x-access-secret", "refresh_token": "x-refresh-secret",
            "expires_in": 7200, "scope": OAuthService.X_SCOPES, "token_type": "bearer", **overrides}


def exported_record(**overrides):
    """Credential record written by `python -m app.cli.chatgpt login`."""
    return {"email": "user@example.com", "issuer": CHATGPT_ISSUER, "subject": "user-subject",
            "client_id": ISSUED_CLIENT_ID,
            "ext_agent_host_id": "urn:uuid:00000000-0000-4000-8000-000000000000",
            "id_token": "id-token-secret", "access_token": "access-secret",
            "refresh_token": "refresh-secret", "token_type": "Bearer", "expires_in": 3600,
            "scopes": GRANTED_SCOPES, "saved_at": datetime.now(timezone.utc).isoformat(),
            **overrides}


def chatgpt_refresh_payload(**overrides):
    return {"access_token": "new-access", "refresh_token": "rotated-refresh",
            "id_token": "refreshed-id-token", "expires_in": 3600, "token_type": "Bearer",
            "scope": " ".join(GRANTED_SCOPES), **overrides}


def decrypt(settings, value):
    return Fernet(settings.token_encryption_key.get_secret_value()).decrypt(value.encode()).decode()


def expire(factory, model=ChatGPTCredential):
    with factory.begin() as session:
        session.get(model, 1).expires_at = utcnow() - timedelta(seconds=1)


# --- X browser flow (FastAPI login/callback) ---------------------------------

def test_pkce_encryption_and_replay(settings, factory):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=x_token_payload())
    auth = service(settings, factory, handler)
    url = auth.login("x")
    params = parse_qs(urlparse(url).query)
    state = params["state"][0]
    assert params["code_challenge_method"] == ["S256"]
    assert "ext_agent_host_id" not in params and "resource" not in params
    assert "host_id" not in params
    with factory() as session:
        stored = session.get(OAuthState, hashlib.sha256(state.encode()).hexdigest())
        assert stored is not None and state not in stored.verifier
    run(auth.callback("x", state, "code-secret"))
    sent_verifier = parse_qs(requests[0].content.decode())["code_verifier"][0]
    assert params["code_challenge"] == [
        base64.urlsafe_b64encode(hashlib.sha256(sent_verifier.encode()).digest()).rstrip(b"=").decode()]
    assert run(auth.access_token("x")) == "x-access-secret"
    with factory() as session:
        row = session.get(XCredential, 1)
        assert decrypt(settings, row.access_token) == "x-access-secret"
        assert decrypt(settings, row.refresh_token) == "x-refresh-secret"
    assert "secret" not in str(auth.status("x"))
    with pytest.raises(ServiceError, match="oauth_state_invalid"):
        run(auth.callback("x", state, "another-code"))
    assert len(requests) == 1
    auth.disconnect("x")
    assert auth.status("x")["connected"] is False
    with pytest.raises(ServiceError, match="oauth_not_connected"):
        run(auth.access_token("x"))


@pytest.mark.parametrize("change,error", [
    ({"expires_in": "bad"}, "oauth_token_invalid"),
    ({"expires_in": True}, "oauth_token_invalid"),
    ({"expires_in": 10 ** 1000}, "oauth_token_invalid"),
    ({"scope": None}, "oauth_scope_missing"),
    ({"client_id": "unbound"}, "oauth_client_mismatch"),
])
def test_fail_closed(settings, factory, change, error):
    payload = x_token_payload()
    payload.update(change)
    auth = service(settings, factory, lambda request: httpx.Response(200, json=payload))
    state = state_for(auth)
    with pytest.raises(ServiceError, match=error):
        run(auth.callback("x", state, "code"))
    assert not auth.status("x")["connected"]
    with pytest.raises(ServiceError, match="oauth_state_invalid"):
        run(auth.callback("x", state, "code"))


def test_expired_state_and_api_error(settings, factory):
    auth = service(settings, factory, lambda request: httpx.Response(
        401, json={"error": "invalid_grant", "description": "secret-body"}))
    state = state_for(auth)
    with pytest.raises(ServiceError) as caught:
        run(auth.callback("x", state, "code"))
    assert caught.value.error_code == "oauth_authorization_failed"
    assert "secret-body" not in str(caught.value)
    state = state_for(auth)
    with factory.begin() as session:
        session.scalars(select(OAuthState)).one().expires_at = utcnow() - timedelta(seconds=1)
    with pytest.raises(ServiceError, match="oauth_state_invalid"):
        run(auth.callback("x", state, "code"))


def test_required_registration_and_x_scopes(settings, factory):
    auth = service(settings, factory, lambda request: httpx.Response(500))
    params = parse_qs(urlparse(auth.login("x")).query)
    assert set(params["scope"][0].split()) == {
        "tweet.read", "tweet.write", "users.read", "offline.access"}
    settings.x_client_id = ""
    with pytest.raises(ServiceError, match="oauth_not_configured"):
        auth.login("x")


def test_chatgpt_browser_flow_is_not_served_by_api_service(settings, factory):
    # ChatGPT sign-in needs a 127.0.0.1 loopback callback, so it runs in the local CLI.
    auth = service(settings, factory, lambda request: pytest.fail("must not send"))
    with pytest.raises(ServiceError, match="oauth_provider_invalid"):
        auth.login("chatgpt")
    with pytest.raises(ServiceError, match="oauth_provider_invalid"):
        run(auth.callback("chatgpt", "state", "code"))


def test_token_transport_failure_consumes_state(settings, factory):
    calls = []
    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("contains-sensitive-info", request=request)
    auth = service(settings, factory, handler)
    state = state_for(auth)
    with pytest.raises(ServiceError) as caught:
        run(auth.callback("x", state, "code"))
    assert caught.value.retryable
    assert "sensitive" not in str(caught.value)
    with pytest.raises(ServiceError, match="oauth_state_invalid"):
        run(auth.callback("x", state, "code"))
    assert len(calls) == 1


def test_disconnect_invalidates_pending_login(settings, factory):
    auth = service(settings, factory, lambda request: pytest.fail("must not send"))
    state = state_for(auth)
    auth.disconnect("x")
    with pytest.raises(ServiceError, match="oauth_state_invalid"):
        run(auth.callback("x", state, "code"))


def test_expired_x_refresh_rotation(settings, factory):
    calls = []
    def handler(request):
        data = parse_qs(request.content.decode())
        calls.append(data)
        assert data["client_id"] == ["registered-x"]
        assert "ext_agent_host_id" not in data and "host_id" not in data
        assert "resource" not in data
        return httpx.Response(200, json={
            "access_token": "x-new-access" if len(calls) > 1 else "x-access",
            "refresh_token": "x-rotated-refresh" if len(calls) > 1 else "x-refresh",
            "scope": OAuthService.X_SCOPES, "expires_in": 3600,
        })
    auth = service(settings, factory, handler)
    run(auth.callback("x", state_for(auth, "x"), "code"))
    assert run(auth.access_token("x")) == "x-access"
    expire(factory, XCredential)
    assert run(auth.access_token("x")) == "x-new-access"
    assert calls[-1]["refresh_token"] == ["x-refresh"]
    with factory() as session:
        row = session.get(XCredential, 1)
        assert decrypt(settings, row.refresh_token) == "x-rotated-refresh"


# --- ChatGPT credentials imported from the local sign-in CLI ------------------

def test_import_stores_encrypted_identity_bound_to_server_host(settings, factory):
    auth = service(settings, factory, lambda request: pytest.fail("must not send"))
    saved_at = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=1)
    auth.import_chatgpt(exported_record(saved_at=saved_at.isoformat()))
    with factory() as session:
        row = session.get(ChatGPTCredential, 1)
        assert decrypt(settings, row.access_token) == "access-secret"
        assert decrypt(settings, row.refresh_token) == "refresh-secret"
        assert decrypt(settings, row.id_token) == "id-token-secret"
        assert (row.client_id, row.subject, row.email, row.issuer) == (
            ISSUED_CLIENT_ID, "user-subject", "user@example.com", CHATGPT_ISSUER)
        # The server keeps its own host ID instead of the laptop's.
        assert row.host_id == HOST_ID
        assert row.scope == " ".join(GRANTED_SCOPES)
        assert row.expires_at.replace(tzinfo=timezone.utc) == saved_at + timedelta(seconds=3600)
    status = auth.status("chatgpt")
    assert status["connected"] is True and status["client_id"] == ISSUED_CLIENT_ID
    assert "secret" not in str(status)


@pytest.mark.parametrize("record", [
    "not-a-dict",
    exported_record(issuer="https://auth.openai.com/"),
    exported_record(subject=""),
    exported_record(subject="s" * 256),
    exported_record(client_id="dynamic_agent_client"),
    exported_record(client_id=""),
    exported_record(client_id="oaiapp\nother"),
    exported_record(email="e" * 321),
    exported_record(access_token=""),
    exported_record(refresh_token=None),
    exported_record(token_type="mac"),
    exported_record(expires_in=0),
    exported_record(expires_in=True),
    exported_record(expires_in=31536001),
    exported_record(scopes=["openid", "offline_access"]),
    exported_record(scopes="chatgpt.tokens.use.direct"),
    exported_record(saved_at="2026-10-11T01:00:00"),
    exported_record(saved_at="yesterday"),
    exported_record(saved_at=(datetime.now(timezone.utc) + timedelta(minutes=6)).isoformat()),
] + [{key: value for key, value in exported_record().items() if key != missing}
     for missing in ("issuer", "subject", "client_id", "access_token", "refresh_token",
                     "expires_in", "scopes", "saved_at")])
def test_import_rejects_invalid_records(settings, factory, record):
    auth = service(settings, factory, lambda request: pytest.fail("must not send"))
    with pytest.raises(ServiceError) as caught:
        auth.import_chatgpt(record)
    assert caught.value.error_code == "oauth_import_invalid"
    assert "secret" not in str(caught.value)
    assert not auth.status("chatgpt")["connected"]


def test_import_accepts_expired_access_token_with_refresh_token(settings, factory):
    auth = service(settings, factory, lambda request: pytest.fail("must not send"))
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    auth.import_chatgpt(exported_record(saved_at=old.isoformat()))
    assert auth.status("chatgpt")["expired"] is True


def test_import_requires_server_host_id(settings, factory):
    settings.chatgpt_host_id = ""
    auth = service(settings, factory, lambda request: pytest.fail("must not send"))
    with pytest.raises(ServiceError, match="oauth_not_configured"):
        auth.import_chatgpt(exported_record())


@pytest.mark.parametrize("change", [{"subject": "another-subject"}, {"client_id": "oaiapp_other"}])
def test_import_rejects_another_connected_registration(settings, factory, change):
    auth = service(settings, factory, lambda request: pytest.fail("must not send"))
    auth.import_chatgpt(exported_record())
    with pytest.raises(ServiceError, match="oauth_account_mismatch"):
        auth.import_chatgpt(exported_record(access_token="other-access", **change))
    with factory() as session:
        assert decrypt(settings, session.get(ChatGPTCredential, 1).access_token) == "access-secret"
    auth.disconnect("chatgpt")
    auth.import_chatgpt(exported_record(access_token="other-access", **change))
    assert auth.status("chatgpt")["connected"] is True


def test_import_replaces_same_registration(settings, factory):
    auth = service(settings, factory, lambda request: pytest.fail("must not send"))
    auth.import_chatgpt(exported_record())
    auth.import_chatgpt(exported_record(access_token="reauthorized-access"))
    assert run(auth.access_token("chatgpt")) == "reauthorized-access"


def test_legacy_credential_requires_reauthorization(settings, factory):
    # Rows saved by the old browser flow have no verified subject.
    with factory.begin() as session:
        cipher = Fernet(settings.token_encryption_key.get_secret_value())
        session.add(ChatGPTCredential(
            id=1, access_token=cipher.encrypt(b"legacy-access").decode(),
            refresh_token=cipher.encrypt(b"legacy-refresh").decode(),
            expires_at=utcnow() + timedelta(hours=1),
            scope="openid offline_access resource.invoke chatgpt.tokens.use.direct",
            client_id="pre-registered", host_id="registered-host"))
    auth = service(settings, factory, lambda request: pytest.fail("must not send"))
    assert auth.status("chatgpt") == {"provider": "chatgpt", "connected": False,
                                      "reauthorization_required": True}
    with pytest.raises(ServiceError, match="oauth_reauthorization_required"):
        run(auth.access_token("chatgpt"))
    auth.import_chatgpt(exported_record())
    assert auth.status("chatgpt")["connected"] is True


def test_disconnect_keeps_registration_and_host(settings, factory):
    auth = service(settings, factory, lambda request: pytest.fail("must not send"))
    auth.import_chatgpt(exported_record())
    auth.disconnect("chatgpt")
    with factory() as session:
        row = session.get(ChatGPTCredential, 1)
        assert (row.access_token, row.refresh_token, row.id_token) == ("", None, None)
        assert (row.client_id, row.subject, row.host_id) == (ISSUED_CLIENT_ID, "user-subject", HOST_ID)
    assert auth.status("chatgpt") == {"provider": "chatgpt", "connected": False}
    with pytest.raises(ServiceError, match="oauth_not_connected"):
        run(auth.access_token("chatgpt"))


def test_live_expiry_and_refresh_rotation(settings, factory):
    calls = []
    def handler(request):
        calls.append(parse_qs(request.content.decode()))
        return httpx.Response(200, json=chatgpt_refresh_payload())
    auth = service(settings, factory, handler)
    auth.import_chatgpt(exported_record())
    assert run(auth.access_token("chatgpt")) == "access-secret"
    assert calls == []
    expire(factory)
    assert run(auth.access_token("chatgpt")) == "new-access"
    assert calls[-1]["refresh_token"] == ["refresh-secret"]
    with factory() as session:
        row = session.get(ChatGPTCredential, 1)
        assert decrypt(settings, row.refresh_token) == "rotated-refresh"


def test_binding_and_scope_checked_on_every_use(settings, factory):
    auth = service(settings, factory, lambda request: pytest.fail("must not send"))
    auth.import_chatgpt(exported_record())
    settings.chatgpt_host_id = "urn:uuid:99999999-9999-4999-8999-999999999999"
    assert auth.status("chatgpt")["client_id"] == ISSUED_CLIENT_ID
    assert auth.status("chatgpt")["host_id"] == HOST_ID
    with factory.begin() as session:
        session.get(ChatGPTCredential, 1).scope = "openid"
    with pytest.raises(ServiceError, match="oauth_scope_missing"):
        run(auth.access_token("chatgpt"))


def test_refresh_uses_issued_client_and_official_parameters_only(settings, factory):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=chatgpt_refresh_payload(
            ext_agent_host_id="urn:uuid:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"))
    auth = service(settings, factory, handler)
    auth.import_chatgpt(exported_record())
    settings.chatgpt_host_id = "urn:uuid:99999999-9999-4999-8999-999999999999"
    expire(factory)
    assert run(auth.access_token("chatgpt")) == "new-access"
    assert str(requests[0].url) == CHATGPT_TOKEN_URL
    assert "authorization" not in requests[0].headers
    assert parse_qs(requests[0].content.decode()) == {
        "grant_type": ["refresh_token"], "refresh_token": ["refresh-secret"],
        "client_id": [ISSUED_CLIENT_ID], "resource": [CHATGPT_RESOURCE]}
    # A token response cannot move the credential to another host.
    assert auth.status("chatgpt")["host_id"] == HOST_ID


@pytest.mark.parametrize("change,error", [
    ({"scope": "openid offline_access"}, "oauth_scope_missing"),
    ({"client_id": "oaiapp_other"}, "oauth_client_mismatch"),
    ({"expires_in": -1}, "oauth_token_invalid"),
])
def test_invalid_refresh_response_keeps_previous_tokens(settings, factory, change, error):
    auth = service(settings, factory, lambda request: httpx.Response(
        200, json=chatgpt_refresh_payload(**change)))
    auth.import_chatgpt(exported_record())
    expire(factory)
    with pytest.raises(ServiceError, match=error):
        run(auth.access_token("chatgpt"))
    with factory() as session:
        assert decrypt(settings, session.get(ChatGPTCredential, 1).refresh_token) == "refresh-secret"


def test_refresh_starts_before_access_token_expiry(settings, factory, monkeypatch):
    # Refresh early so a token cannot expire during a Responses stream (up to 90 seconds).
    now = utcnow()
    monkeypatch.setattr(auth_module, "utcnow", lambda: now)
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=chatgpt_refresh_payload())
    auth = service(settings, factory, handler)
    auth.import_chatgpt(exported_record())
    margin = OAuthService.REFRESH_MARGIN
    with factory.begin() as session:
        session.get(ChatGPTCredential, 1).expires_at = now + margin + timedelta(seconds=1)
    assert run(auth.access_token("chatgpt")) == "access-secret"
    assert calls == []
    with factory.begin() as session:
        session.get(ChatGPTCredential, 1).expires_at = now + margin
    assert run(auth.access_token("chatgpt")) == "new-access"
    assert len(calls) == 1


@pytest.mark.parametrize("body", [
    {"error": "invalid_grant"}, {"error": "invalid_refresh_token"}, {"error": "token_expired"},
    {"error": "refresh_token_expired"}, {"error": "refresh_token_invalidated"},
    {"error": "refresh_token_reused"}, {"error": {"code": "refresh_token_reused"}},
])
def test_unusable_refresh_token_is_cleared_but_registration_is_kept(settings, factory, body):
    auth = service(settings, factory, lambda request: httpx.Response(400, json=body))
    auth.import_chatgpt(exported_record())
    expire(factory)
    with pytest.raises(ServiceError) as caught:
        run(auth.access_token("chatgpt"))
    assert caught.value.error_code == "oauth_reauthorization_required"
    assert not caught.value.retryable
    with factory() as session:
        row = session.get(ChatGPTCredential, 1)
        assert (row.access_token, row.refresh_token, row.id_token) == ("", None, None)
        assert (row.client_id, row.subject, row.host_id) == (ISSUED_CLIENT_ID, "user-subject", HOST_ID)
    assert auth.status("chatgpt")["connected"] is False


@pytest.mark.parametrize("response,error,retryable", [
    (httpx.Response(401, json={"error": "invalid_client"}), "oauth_client_invalid", False),
    (httpx.Response(400, json={"error": "invalid_request"}), "oauth_authorization_failed", False),
    (httpx.Response(400, text="not-json"), "oauth_authorization_failed", False),
    (httpx.Response(503), "oauth_endpoint_error", True),
    (httpx.Response(429), "oauth_endpoint_error", True),
])
def test_other_refresh_failures_keep_tokens(settings, factory, response, error, retryable):
    auth = service(settings, factory, lambda request: response)
    auth.import_chatgpt(exported_record())
    expire(factory)
    with pytest.raises(ServiceError) as caught:
        run(auth.access_token("chatgpt"))
    assert (caught.value.error_code, caught.value.retryable) == (error, retryable)
    with factory() as session:
        assert decrypt(settings, session.get(ChatGPTCredential, 1).refresh_token) == "refresh-secret"


def test_x_refresh_rejection_behavior_is_unchanged(settings, factory):
    responses = [httpx.Response(200, json=x_token_payload()),
                 httpx.Response(400, json={"error": "invalid_grant"})]
    auth = service(settings, factory, lambda request: responses.pop(0))
    run(auth.callback("x", state_for(auth), "code"))
    expire(factory, XCredential)
    with pytest.raises(ServiceError, match="oauth_authorization_failed"):
        run(auth.access_token("x"))
    with factory() as session:
        assert decrypt(settings, session.get(XCredential, 1).refresh_token) == "x-refresh-secret"


def test_cancelled_refresh_still_stores_rotated_refresh_token(settings, factory):
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        async def handler(request):
            started.set()
            await release.wait()
            return httpx.Response(200, json=chatgpt_refresh_payload())
        auth = service(settings, factory, handler)
        auth.import_chatgpt(exported_record())
        expire(factory)
        refreshing = asyncio.create_task(auth.access_token("chatgpt"))
        await started.wait()
        refreshing.cancel()
        await asyncio.sleep(0)
        refreshing.cancel()
        await asyncio.sleep(0.01)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await refreshing
        with factory() as session:
            row = session.get(ChatGPTCredential, 1)
            # OpenAI already rotated the refresh token, so losing it would force re-authorization.
            assert decrypt(settings, row.refresh_token) == "rotated-refresh"
            assert decrypt(settings, row.access_token) == "new-access"
    run(scenario())


# --- PostgreSQL ----------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL unavailable")
def test_postgres_serializes_concurrent_refresh(settings):
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL required for concurrency test")
    schema = "oauth_test_" + uuid.uuid4().hex
    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    scoped = engine.execution_options(schema_translate_map={None: schema})
    try:
        Base.metadata.create_all(scoped)
        factory = sessionmaker(scoped, expire_on_commit=False)
        calls = []
        async def handler(request):
            calls.append(parse_qs(request.content.decode()))
            await asyncio.sleep(0.05)
            if "code" in calls[-1]:
                return httpx.Response(200, json=x_token_payload())
            return httpx.Response(200, json=chatgpt_refresh_payload(access_token="rotated-access"))
        auth = service(settings, factory, handler)
        async def scenario():
            state = state_for(auth)
            callbacks = await asyncio.wait_for(asyncio.gather(
                auth.callback("x", state, "code"),
                auth.callback("x", state, "code"), return_exceptions=True), timeout=10)
            assert sum(result is None for result in callbacks) == 1
            rejected = [result for result in callbacks if isinstance(result, ServiceError)]
            assert len(rejected) == 1 and rejected[0].error_code == "oauth_state_invalid"
            assert len(calls) == 1
            await asyncio.to_thread(auth.import_chatgpt, exported_record())
            expire(factory)
            calls.clear()
            result = await asyncio.wait_for(asyncio.gather(
                auth.access_token("chatgpt"), auth.access_token("chatgpt")), timeout=10)
            assert result == ["rotated-access", "rotated-access"]
            assert len(calls) == 1
            expire(factory)
            calls.clear()
            refreshing = asyncio.create_task(auth.access_token("chatgpt"))
            while not calls:
                await asyncio.sleep(0.001)
            await asyncio.wait_for(asyncio.to_thread(auth.disconnect, "chatgpt"), timeout=10)
            assert await refreshing == "rotated-access"
            assert not auth.status("chatgpt")["connected"]
            with pytest.raises(ServiceError, match="oauth_not_connected"):
                await auth.access_token("chatgpt")
        run(scenario())
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        engine.dispose()


@pytest.fixture
def postgres_factory():
    if not os.getenv("TEST_DATABASE_URL"):
        pytest.skip("TEST_DATABASE_URL unavailable")
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    schema = "oauth_test_" + uuid.uuid4().hex
    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    scoped = engine.execution_options(schema_translate_map={None: schema})
    try:
        Base.metadata.create_all(scoped)
        yield sessionmaker(scoped, expire_on_commit=False)
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        engine.dispose()


def hold_row_lock(factory):
    """Lock the ChatGPT credential row from another connection, like a concurrent refresher."""
    session = factory()
    session.scalars(select(ChatGPTCredential).with_for_update()).one()
    return session


@pytest.mark.integration
def test_postgres_valid_token_is_read_without_waiting_for_refresh_lock(settings, postgres_factory):
    auth = service(settings, postgres_factory, lambda request: pytest.fail("must not refresh"))
    auth.import_chatgpt(exported_record())
    holder = hold_row_lock(postgres_factory)
    try:
        assert run(asyncio.wait_for(auth.access_token("chatgpt"), timeout=2)) == "access-secret"
    finally:
        holder.rollback()
        holder.close()


@pytest.mark.integration
def test_postgres_refresh_lock_wait_is_bounded(settings, postgres_factory, monkeypatch):
    monkeypatch.setattr(OAuthService, "LOCK_WAIT_SECONDS", 0.3)
    auth = service(settings, postgres_factory, lambda request: pytest.fail("must not refresh"))
    auth.import_chatgpt(exported_record())
    expire(postgres_factory)
    holder = hold_row_lock(postgres_factory)
    try:
        with pytest.raises(ServiceError) as caught:
            run(asyncio.wait_for(auth.access_token("chatgpt"), timeout=5))
        assert caught.value.error_code == "oauth_refresh_busy" and caught.value.retryable
    finally:
        holder.rollback()
        holder.close()


@pytest.mark.integration
def test_postgres_cancelled_lock_wait_leaves_no_lock_behind(settings, postgres_factory):
    async def handler(request):
        return httpx.Response(200, json=chatgpt_refresh_payload())
    auth = service(settings, postgres_factory, handler)
    auth.import_chatgpt(exported_record())
    expire(postgres_factory)
    holder = hold_row_lock(postgres_factory)
    async def scenario():
        waiting = asyncio.create_task(auth.access_token("chatgpt"))
        await asyncio.sleep(0.2)
        waiting.cancel()
        await asyncio.sleep(0)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        holder.rollback()
        holder.close()
        # Nothing from the cancelled waiter may still hold the row.
        return await asyncio.wait_for(auth.access_token("chatgpt"), timeout=5)
    try:
        assert run(scenario()) == "new-access"
    finally:
        holder.close()
