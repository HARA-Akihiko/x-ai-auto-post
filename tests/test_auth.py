import asyncio
import base64
import hashlib
import os
import uuid
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.models import Base, ChatGPTCredential, OAuthState, XCredential, utcnow
from app.services.auth import OAuthService, ServiceError


@pytest.fixture
def settings():
    return Settings(
        database_url="sqlite://",
        token_encryption_key=Fernet.generate_key().decode(),
        admin_api_token="a" * 32,
        chatgpt_client_id="registered-client",
        chatgpt_host_id="registered-host",
        chatgpt_resource="https://api.openai.com/v1",
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


def state_for(auth, provider="chatgpt"):
    return parse_qs(urlparse(auth.login(provider)).query)["state"][0]


def token_payload(**overrides):
    return dict(access_token="access-secret", refresh_token="refresh-secret",
                id_token="opaque-id-secret", expires_in=3600,
                scope="openid offline_access chatgpt.tokens.use.direct", **overrides)


def test_pkce_encryption_and_replay(settings, factory):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=token_payload())
    auth = service(settings, factory, handler)
    url = auth.login("chatgpt")
    params = parse_qs(urlparse(url).query)
    state = params["state"][0]
    assert params["code_challenge_method"] == ["S256"]
    assert params["ext_agent_host_id"] == ["registered-host"]
    assert params["resource"] == ["https://api.openai.com/v1"]
    assert "host_id" not in params
    with factory() as session:
        stored = session.get(OAuthState, hashlib.sha256(state.encode()).hexdigest())
        assert stored is not None and state not in stored.verifier
    run(auth.callback("chatgpt", state, "code-secret"))
    sent_verifier = parse_qs(requests[0].content.decode())["code_verifier"][0]
    assert params["code_challenge"] == [
        base64.urlsafe_b64encode(hashlib.sha256(sent_verifier.encode()).digest()).rstrip(b"=").decode()]
    assert run(auth.access_token("chatgpt")) == "access-secret"
    with factory() as session:
        row = session.get(ChatGPTCredential, 1)
        cipher = Fernet(settings.token_encryption_key.get_secret_value())
        assert cipher.decrypt(row.access_token.encode()) == b"access-secret"
        assert cipher.decrypt(row.refresh_token.encode()) == b"refresh-secret"
        assert cipher.decrypt(row.id_token.encode()) == b"opaque-id-secret"
    assert "secret" not in str(auth.status("chatgpt"))
    with pytest.raises(ServiceError, match="oauth_state_invalid"):
        run(auth.callback("chatgpt", state, "another-code"))
    assert len(requests) == 1
    auth.disconnect("chatgpt")
    assert auth.status("chatgpt")["connected"] is False
    with pytest.raises(ServiceError, match="oauth_not_connected"):
        run(auth.access_token("chatgpt"))


def test_live_expiry_and_refresh_rotation(settings, factory):
    calls = []
    def handler(request):
        calls.append(parse_qs(request.content.decode()))
        payload = token_payload()
        if len(calls) > 1:
            payload.update(access_token="new-access", refresh_token="rotated-refresh")
        return httpx.Response(200, json=payload)
    auth = service(settings, factory, handler)
    run(auth.callback("chatgpt", state_for(auth), "code"))
    assert run(auth.access_token("chatgpt")) == "access-secret"
    assert len(calls) == 1
    with factory.begin() as session:
        session.get(ChatGPTCredential, 1).expires_at = utcnow() - timedelta(seconds=1)
    assert run(auth.access_token("chatgpt")) == "new-access"
    assert calls[-1]["refresh_token"] == ["refresh-secret"]
    with factory() as session:
        row = session.get(ChatGPTCredential, 1)
        assert Fernet(settings.token_encryption_key.get_secret_value()).decrypt(
            row.refresh_token.encode()) == b"rotated-refresh"


@pytest.mark.parametrize("change,error", [
    ({"expires_in": "bad"}, "oauth_token_invalid"),
    ({"expires_in": True}, "oauth_token_invalid"),
    ({"expires_in": 10 ** 1000}, "oauth_token_invalid"),
    ({"scope": "openid"}, "oauth_scope_missing"),
    ({"scope": None}, "oauth_scope_missing"),
    ({"client_id": "unbound"}, "oauth_client_mismatch"),
])
def test_fail_closed(settings, factory, change, error):
    payload = token_payload()
    payload.update(change)
    auth = service(settings, factory, lambda request: httpx.Response(200, json=payload))
    state = state_for(auth)
    with pytest.raises(ServiceError, match=error):
        run(auth.callback("chatgpt", state, "code"))
    assert not auth.status("chatgpt")["connected"]
    with pytest.raises(ServiceError, match="oauth_state_invalid"):
        run(auth.callback("chatgpt", state, "code"))


def test_expired_state_and_api_error(settings, factory):
    auth = service(settings, factory, lambda request: httpx.Response(
        401, json={"error": "invalid_grant", "description": "secret-body"}))
    state = state_for(auth)
    with pytest.raises(ServiceError) as caught:
        run(auth.callback("chatgpt", state, "code"))
    assert caught.value.error_code == "oauth_authorization_failed"
    assert "secret-body" not in str(caught.value)
    state = state_for(auth)
    with factory.begin() as session:
        session.scalars(select(OAuthState)).one().expires_at = utcnow() - timedelta(seconds=1)
    with pytest.raises(ServiceError, match="oauth_state_invalid"):
        run(auth.callback("chatgpt", state, "code"))


def test_required_registration_and_x_scopes(settings, factory):
    auth = service(settings, factory, lambda request: httpx.Response(500))
    settings.chatgpt_host_id = ""
    with pytest.raises(ServiceError, match="oauth_not_configured"):
        auth.login("chatgpt")
    params = parse_qs(urlparse(auth.login("x")).query)
    assert set(params["scope"][0].split()) == {
        "tweet.read", "tweet.write", "users.read", "offline.access"}


def test_binding_and_scope_checked_on_every_use(settings, factory):
    def handler(request):
        data = parse_qs(request.content.decode())
        assert data["client_id"] == ["registered-client"]
        assert data["ext_agent_host_id"] == ["registered-host"]
        assert data["resource"] == ["https://api.openai.com/v1"]
        return httpx.Response(200, json={**token_payload(), "ext_agent_host_id": "bound-host"})
    auth = service(settings, factory, handler)
    state = state_for(auth)
    settings.chatgpt_client_id = "changed-client"
    settings.chatgpt_resource = "https://api.example/changed"
    run(auth.callback("chatgpt", state, "code"))
    assert auth.status("chatgpt")["client_id"] == "registered-client"
    assert auth.status("chatgpt")["host_id"] == "bound-host"
    with factory.begin() as session:
        session.get(ChatGPTCredential, 1).scope = "openid"
    with pytest.raises(ServiceError, match="oauth_scope_missing"):
        run(auth.access_token("chatgpt"))


def test_token_transport_failure_consumes_state(settings, factory):
    calls = []
    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("contains-sensitive-info", request=request)
    auth = service(settings, factory, handler)
    state = state_for(auth)
    with pytest.raises(ServiceError) as caught:
        run(auth.callback("chatgpt", state, "code"))
    assert caught.value.retryable
    assert "sensitive" not in str(caught.value)
    with pytest.raises(ServiceError, match="oauth_state_invalid"):
        run(auth.callback("chatgpt", state, "code"))
    assert len(calls) == 1


def test_disconnect_invalidates_pending_login(settings, factory):
    auth = service(settings, factory, lambda request: pytest.fail("must not send"))
    state = state_for(auth)
    auth.disconnect("chatgpt")
    with pytest.raises(ServiceError, match="oauth_state_invalid"):
        run(auth.callback("chatgpt", state, "code"))


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
    with factory.begin() as session:
        session.get(XCredential, 1).expires_at = utcnow() - timedelta(seconds=1)
    assert run(auth.access_token("x")) == "x-new-access"
    assert calls[-1]["refresh_token"] == ["x-refresh"]
    with factory() as session:
        row = session.get(XCredential, 1)
        assert Fernet(settings.token_encryption_key.get_secret_value()).decrypt(
            row.refresh_token.encode()) == b"x-rotated-refresh"


def test_refresh_preserves_bound_host_and_rotates_metadata(settings, factory):
    calls = []
    def handler(request):
        data = parse_qs(request.content.decode())
        calls.append(data)
        assert data["client_id"] == ["registered-client"]
        assert data["resource"] == ["https://api.openai.com/v1"]
        assert data["ext_agent_host_id"] == [
            "registered-host" if len(calls) == 1 else "bound-host"]
        return httpx.Response(200, json={
            **token_payload(), "ext_agent_host_id": "bound-host" if len(calls) == 1 else "rotated-host",
            "refresh_token": "first-refresh" if len(calls) == 1 else "new-refresh",
        })
    auth = service(settings, factory, handler)
    run(auth.callback("chatgpt", state_for(auth), "code"))
    settings.chatgpt_client_id = "changed-client"
    settings.chatgpt_host_id = "changed-host"
    with factory.begin() as session:
        session.get(ChatGPTCredential, 1).expires_at = utcnow() - timedelta(seconds=1)
    assert run(auth.access_token("chatgpt")) == "access-secret"
    assert calls[-1]["refresh_token"] == ["first-refresh"]
    assert auth.status("chatgpt")["host_id"] == "rotated-host"


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
            return httpx.Response(200, json={**token_payload(),
                                           "access_token": "rotated-access",
                                           "refresh_token": "rotated-refresh"})
        auth = service(settings, factory, handler)
        async def scenario():
            state = state_for(auth)
            callbacks = await asyncio.wait_for(asyncio.gather(
                auth.callback("chatgpt", state, "code"),
                auth.callback("chatgpt", state, "code"), return_exceptions=True), timeout=10)
            assert sum(result is None for result in callbacks) == 1
            rejected = [result for result in callbacks if isinstance(result, ServiceError)]
            assert len(rejected) == 1 and rejected[0].error_code == "oauth_state_invalid"
            assert len(calls) == 1
            with factory.begin() as session:
                session.get(ChatGPTCredential, 1).expires_at = utcnow() - timedelta(seconds=1)
            calls.clear()
            result = await asyncio.wait_for(asyncio.gather(
                auth.access_token("chatgpt"), auth.access_token("chatgpt")), timeout=10)
            assert result == ["rotated-access", "rotated-access"]
            assert len(calls) == 1
            with factory.begin() as session:
                session.get(ChatGPTCredential, 1).expires_at = utcnow() - timedelta(seconds=1)
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
