import asyncio
import base64
import hashlib
import hmac
import io
import json
import stat
import threading
import time
import urllib.request
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import jwt
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.cli import chatgpt as cli
from app.core.config import (CHATGPT_AUTHORIZE_URL, CHATGPT_ISSUER, CHATGPT_JWKS_URL,
                             CHATGPT_MODELS_URL, CHATGPT_RESOURCE, CHATGPT_TOKEN_URL, Settings)
from app.models import Base, ChatGPTCredential
from app.services import chatgpt_signin as signin
from app.services.auth import OAuthService, ServiceError

HOST_ID = "urn:uuid:123e4567-e89b-42d3-a456-426614174000"
REDIRECT_URI = "http://127.0.0.1:1455/auth/callback"
ISSUED_CLIENT_ID = "oaiapp_issued"
GRANTED_SCOPE = "chatgpt.tokens.use.direct email offline_access openid profile resource.invoke"
PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def jwks(key=PRIVATE_KEY, kid="key-1"):
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    return {"keys": [{**public, "kid": kid, "use": "sig", "alg": "RS256"}]}


def id_token(nonce, *, key=PRIVATE_KEY, kid="key-1", algorithm="RS256", drop=(), **overrides):
    now = int(time.time())
    claims = {"iss": CHATGPT_ISSUER, "aud": ISSUED_CLIENT_ID, "sub": "user-subject",
              "email": "user@example.com", "iat": now, "exp": now + 3600, "nonce": nonce}
    claims.update(overrides)
    for name in drop:
        claims.pop(name)
    return jwt.encode(claims, key, algorithm=algorithm, headers={"kid": kid})


def token_payload(nonce, **overrides):
    return {"access_token": "access-secret", "refresh_token": "refresh-secret",
            "id_token": id_token(nonce), "token_type": "Bearer", "expires_in": 3600,
            "scope": GRANTED_SCOPE, **overrides}


class FakeOpenAI:
    """OpenAI auth endpoints at the HTTP boundary."""

    def __init__(self, token_response=None, jwks_response=None):
        self.token_requests = []
        self.token_response = token_response
        self.jwks_response = jwks_response
        self.nonce = None

    def handler(self, request):
        url = str(request.url)
        if url == CHATGPT_JWKS_URL and request.method == "GET":
            return self.jwks_response or httpx.Response(200, json=jwks())
        if url == CHATGPT_TOKEN_URL and request.method == "POST":
            self.token_requests.append(parse_qs(request.content.decode()))
            if self.token_response is not None:
                return self.token_response(self) if callable(self.token_response) else self.token_response
            return httpx.Response(200, json=token_payload(self.nonce))
        return httpx.Response(404)

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self.handler))


def begin(client_id=None, consent=False):
    url, attempt = signin.start(REDIRECT_URI, HOST_ID, client_id, consent=consent)
    return parse_qs(urlparse(url).query), attempt


def finish(fake, attempt, query=None, expected_subject=None):
    fake.nonce = attempt.nonce
    query = query if query is not None else {
        "code": "code-secret", "state": attempt.state, "client_id": ISSUED_CLIENT_ID,
        "scope": GRANTED_SCOPE.replace(" ", "+")}
    with fake.client() as client:
        return signin.finish(client, attempt, query, HOST_ID, expected_subject=expected_subject)


# --- authorization request -------------------------------------------------

def test_new_registration_authorization_request_follows_official_flow():
    url, attempt = signin.start(REDIRECT_URI, HOST_ID)
    parsed = urlparse(url)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == CHATGPT_AUTHORIZE_URL
    params = parse_qs(parsed.query)
    assert {key: value[0] for key, value in params.items()} == {
        "response_type": "code", "client_id": "dynamic_agent_client",
        "agent_name_hint": "x-ai-auto-post", "ext_agent_host_id": HOST_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct",
        "resource": CHATGPT_RESOURCE, "state": attempt.state, "nonce": attempt.nonce,
        "code_challenge": base64.urlsafe_b64encode(
            hashlib.sha256(attempt.verifier.encode()).digest()).rstrip(b"=").decode(),
        "code_challenge_method": "S256",
    }
    assert attempt.client_id is None
    # Spaces are percent-encoded so that non-form decoders also read them as spaces.
    assert "+" not in parsed.query and "openid%20profile%20email" in parsed.query
    assert len(attempt.state) >= 43 and len(attempt.nonce) >= 43 and len(attempt.verifier) >= 43
    _, other = signin.start(REDIRECT_URI, HOST_ID)
    assert {other.state, other.nonce, other.verifier}.isdisjoint(
        {attempt.state, attempt.nonce, attempt.verifier})


def test_reauthorization_reuses_issued_client_without_agent_name():
    params, attempt = begin(client_id=ISSUED_CLIENT_ID)
    assert params["client_id"] == [ISSUED_CLIENT_ID]
    assert params["ext_agent_host_id"] == [HOST_ID]
    assert "agent_name_hint" not in params and "prompt" not in params
    assert attempt.client_id == ISSUED_CLIENT_ID


def test_consent_is_requested_only_when_asked():
    params, _ = begin(client_id=ISSUED_CLIENT_ID, consent=True)
    assert params["prompt"] == ["consent"]


def test_callback_uri_uses_loopback_ip_and_fixed_path():
    assert signin.callback_uri(1455) == "http://127.0.0.1:1455/auth/callback"
    assert signin.callback_uri(65535) == "http://127.0.0.1:65535/auth/callback"
    for port in (0, 65536):
        with pytest.raises(ValueError):
            signin.callback_uri(port)


# --- callback and code exchange ---------------------------------------------

def test_new_registration_exchanges_code_with_issued_client_and_returns_record():
    fake = FakeOpenAI()
    _, attempt = begin()
    before = datetime.now(timezone.utc)
    record = finish(fake, attempt)
    assert fake.token_requests == [{
        "grant_type": ["authorization_code"], "code": ["code-secret"],
        "client_id": [ISSUED_CLIENT_ID], "code_verifier": [attempt.verifier],
        "redirect_uri": [REDIRECT_URI], "resource": [CHATGPT_RESOURCE],
    }]
    saved_at = datetime.fromisoformat(record.pop("saved_at"))
    assert before <= saved_at <= datetime.now(timezone.utc)
    assert record.pop("id_token").count(".") == 2
    assert record == {
        "email": "user@example.com", "issuer": CHATGPT_ISSUER, "subject": "user-subject",
        "client_id": ISSUED_CLIENT_ID, "ext_agent_host_id": HOST_ID,
        "access_token": "access-secret", "refresh_token": "refresh-secret",
        "token_type": "Bearer", "expires_in": 3600, "scopes": sorted(GRANTED_SCOPE.split()),
    }


def test_reauthorization_callback_may_omit_client_id():
    fake = FakeOpenAI()
    _, attempt = begin(client_id=ISSUED_CLIENT_ID)
    record = finish(fake, attempt, {"code": "code", "state": attempt.state},
                    expected_subject="user-subject")
    assert fake.token_requests[0]["client_id"] == [ISSUED_CLIENT_ID]
    assert record["client_id"] == ISSUED_CLIENT_ID


@pytest.mark.parametrize("query,error", [
    ({"code": "code", "client_id": ISSUED_CLIENT_ID}, "oauth_state_invalid"),
    ({"code": "code", "state": "forged", "client_id": ISSUED_CLIENT_ID}, "oauth_state_invalid"),
    ({"error": "access_denied", "state": "forged"}, "oauth_state_invalid"),
    ({"error": "access_denied", "state": "{state}"}, "oauth_access_denied"),
    ({"error": "server_error", "state": "{state}"}, "oauth_authorization_failed"),
    ({"state": "{state}", "client_id": ISSUED_CLIENT_ID}, "oauth_authorization_failed"),
    ({"code": "code", "state": "{state}"}, "oauth_registration_incomplete"),
    ({"code": "code", "state": "{state}", "client_id": "dynamic_agent_client"},
     "oauth_registration_incomplete"),
    ({"code": "code", "state": "{state}", "client_id": ""}, "oauth_registration_incomplete"),
])
def test_invalid_new_registration_callback_never_exchanges_code(query, error):
    fake = FakeOpenAI()
    _, attempt = begin()
    query = {key: value.format(state=attempt.state) for key, value in query.items()}
    with pytest.raises(ServiceError, match=error):
        finish(fake, attempt, query)
    assert fake.token_requests == []


def test_reauthorization_rejects_different_client_id():
    fake = FakeOpenAI()
    _, attempt = begin(client_id=ISSUED_CLIENT_ID)
    with pytest.raises(ServiceError, match="oauth_client_mismatch"):
        finish(fake, attempt, {"code": "code", "state": attempt.state, "client_id": "oaiapp_other"})
    assert fake.token_requests == []


def test_reauthorization_rejects_another_account():
    _, attempt = begin(client_id=ISSUED_CLIENT_ID)
    with pytest.raises(ServiceError, match="oauth_account_mismatch"):
        finish(FakeOpenAI(), attempt, expected_subject="previous-subject")


@pytest.mark.parametrize("change,error", [
    ({"scope": "openid profile email offline_access resource.invoke"}, "oauth_scope_missing"),
    ({"scope": None}, "oauth_scope_missing"),
    ({"id_token": None}, "oauth_token_invalid"),
    ({"refresh_token": None}, "oauth_token_invalid"),
    ({"access_token": ""}, "oauth_token_invalid"),
    ({"expires_in": 0}, "oauth_token_invalid"),
    ({"expires_in": True}, "oauth_token_invalid"),
    ({"expires_in": 31536001}, "oauth_token_invalid"),
    ({"token_type": "mac"}, "oauth_token_invalid"),
    ({"client_id": "oaiapp_other"}, "oauth_client_mismatch"),
])
def test_invalid_token_response_is_rejected(change, error):
    def respond(fake):
        payload = token_payload(fake.nonce)
        payload.update(change)
        return httpx.Response(200, json={key: value for key, value in payload.items()
                                         if value is not None})
    _, attempt = begin()
    with pytest.raises(ServiceError, match=error):
        finish(FakeOpenAI(token_response=respond), attempt)


def test_rejected_code_and_transport_errors_do_not_leak_details():
    _, attempt = begin()
    with pytest.raises(ServiceError) as caught:
        finish(FakeOpenAI(token_response=httpx.Response(
            400, json={"error": "invalid_grant", "error_description": "secret-body"})), attempt)
    assert caught.value.error_code == "oauth_authorization_failed"
    assert "secret-body" not in str(caught.value)

    def unreachable(request):
        raise httpx.ConnectError("secret-transport", request=request)
    with httpx.Client(transport=httpx.MockTransport(unreachable)) as client:
        with pytest.raises(ServiceError) as caught:
            signin.finish(client, attempt, {"code": "c", "state": attempt.state,
                                            "client_id": ISSUED_CLIENT_ID}, HOST_ID)
    assert caught.value.error_code == "oauth_transport_error" and caught.value.retryable


# --- ID token validation -----------------------------------------------------

def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def hs256_with_public_key(claims):
    """Key-confusion attack: HMAC-sign with the published RSA public key."""
    secret = PRIVATE_KEY.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    signing_input = (b64url(json.dumps({"alg": "HS256", "kid": "key-1", "typ": "JWT"}).encode())
                     + "." + b64url(json.dumps(claims).encode()))
    signature = hmac.new(secret, signing_input.encode(), hashlib.sha256).digest()
    return signing_input + "." + b64url(signature)


@pytest.mark.parametrize("make_token", [
    pytest.param(lambda nonce: id_token(nonce, key=OTHER_KEY), id="signed-by-another-key"),
    pytest.param(lambda nonce: jwt.encode(
        {"iss": CHATGPT_ISSUER, "aud": ISSUED_CLIENT_ID, "sub": "s", "nonce": nonce,
         "iat": int(time.time()), "exp": int(time.time()) + 60},
        None, algorithm="none", headers={"kid": "key-1"}), id="alg-none"),
    pytest.param(lambda nonce: hs256_with_public_key(
        {"iss": CHATGPT_ISSUER, "aud": ISSUED_CLIENT_ID, "sub": "s", "nonce": nonce,
         "iat": int(time.time()), "exp": int(time.time()) + 60}), id="hs256-confusion"),
    pytest.param(lambda nonce: id_token(nonce, kid="unknown-key"), id="unknown-kid"),
    pytest.param(lambda nonce: id_token(nonce, iss="https://auth.openai.com/"), id="issuer-slash"),
    pytest.param(lambda nonce: id_token(nonce, iss="https://evil.example"), id="issuer"),
    pytest.param(lambda nonce: id_token(nonce, aud="dynamic_agent_client"), id="aud-dynamic"),
    pytest.param(lambda nonce: id_token(nonce, aud="oaiapp_other"), id="aud-other"),
    pytest.param(lambda nonce: id_token(nonce, exp=int(time.time()) - 10), id="expired"),
    pytest.param(lambda nonce: id_token(nonce, drop=("iat",)), id="missing-iat"),
    pytest.param(lambda nonce: id_token("another-nonce"), id="nonce-mismatch"),
    pytest.param(lambda nonce: id_token(nonce, drop=("nonce",)), id="missing-nonce"),
    pytest.param(lambda nonce: id_token(nonce, drop=("sub",)), id="missing-sub"),
    pytest.param(lambda nonce: id_token(nonce, sub=""), id="empty-sub"),
    pytest.param(lambda nonce: "not-a-jwt", id="malformed"),
])
def test_invalid_id_token_is_rejected(make_token):
    def respond(fake):
        return httpx.Response(200, json=token_payload(fake.nonce, id_token=make_token(fake.nonce)))
    _, attempt = begin()
    with pytest.raises(ServiceError, match="oauth_id_token_invalid"):
        finish(FakeOpenAI(token_response=respond), attempt)


def test_id_token_expiry_allows_only_small_clock_skew():
    def respond_with_exp(offset):
        return lambda fake: httpx.Response(200, json=token_payload(
            fake.nonce, id_token=id_token(fake.nonce, exp=int(time.time()) + offset)))
    _, attempt = begin()
    assert finish(FakeOpenAI(token_response=respond_with_exp(-2)), attempt)["subject"]
    _, attempt = begin()
    with pytest.raises(ServiceError, match="oauth_id_token_invalid"):
        finish(FakeOpenAI(token_response=respond_with_exp(-8)), attempt)


@pytest.mark.parametrize("response", [
    httpx.Response(503), httpx.Response(200, text="not-json"), httpx.Response(200, json={"keys": "x"}),
])
def test_unavailable_jwks_fails_closed(response):
    _, attempt = begin()
    with pytest.raises(ServiceError) as caught:
        finish(FakeOpenAI(jwks_response=response), attempt)
    assert caught.value.error_code == "oauth_jwks_unavailable"


# --- local files ---------------------------------------------------------------

def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def test_host_id_is_created_once_and_kept_private(tmp_path):
    path = tmp_path / "config" / "host-id"
    host_id = signin.load_or_create_host_id(path)
    assert host_id.startswith("urn:uuid:")
    assert host_id == signin.load_or_create_host_id(path)
    assert mode(path) == 0o600 and mode(path.parent) == 0o700


def test_invalid_host_id_file_is_rejected(tmp_path):
    path = tmp_path / "host-id"
    path.write_text("user@example.com")
    with pytest.raises(ServiceError, match="oauth_host_id_invalid"):
        signin.load_or_create_host_id(path)


def test_credentials_are_written_atomically_with_owner_only_permissions(tmp_path):
    path = tmp_path / "config" / "chatgpt-credentials.json"
    assert signin.read_record(path) is None
    signin.write_private_json(path, {"client_id": ISSUED_CLIENT_ID, "subject": "s"})
    signin.write_private_json(path, {"client_id": ISSUED_CLIENT_ID, "subject": "s2"})
    assert signin.read_record(path) == {"client_id": ISSUED_CLIENT_ID, "subject": "s2"}
    assert mode(path) == 0o600
    assert [item.name for item in path.parent.iterdir()] == [path.name]


def test_unreadable_credentials_file_is_rejected(tmp_path):
    path = tmp_path / "chatgpt-credentials.json"
    path.write_text("[]")
    with pytest.raises(ServiceError, match="oauth_credentials_invalid"):
        signin.read_record(path)


# --- CLI: login --------------------------------------------------------------

class Browser:
    """Simulates the user approving the request in the system browser."""

    def __init__(self, callback=None):
        self.params = None
        self.callback = callback or (lambda params: {
            "code": "code-secret", "state": params["state"][0], "client_id": ISSUED_CLIENT_ID})

    def __call__(self, url):
        self.params = parse_qs(urlparse(url).query)
        target = self.params["redirect_uri"][0] + "?" + urlencode(self.callback(self.params))
        threading.Thread(target=lambda: urllib.request.urlopen(target, timeout=5).read(),
                         daemon=True).start()


def run_login(tmp_path, fake, browser, *, consent=False):
    out, err = io.StringIO(), io.StringIO()

    def token_response(fake_self):
        fake_self.nonce = browser.params["nonce"][0]
        return httpx.Response(200, json=token_payload(fake_self.nonce))
    if fake.token_response is None:
        fake.token_response = token_response
    with fake.client() as client:
        code = cli.login(config_dir=tmp_path, port=0, consent=consent, timeout=10,
                         http_client=client, open_browser=browser, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def test_login_registers_saves_private_credentials_and_hides_tokens(tmp_path):
    browser = Browser()
    code, out, err = run_login(tmp_path, FakeOpenAI(), browser)
    assert code == 0, err
    assert browser.params["client_id"] == ["dynamic_agent_client"]
    redirect = urlparse(browser.params["redirect_uri"][0])
    assert redirect.scheme == "http" and redirect.hostname == "127.0.0.1"
    assert redirect.path == "/auth/callback"
    path = tmp_path / "chatgpt-credentials.json"
    record = json.loads(path.read_text())
    assert record["client_id"] == ISSUED_CLIENT_ID
    assert record["ext_agent_host_id"] == (tmp_path / "host-id").read_text().strip()
    assert mode(path) == 0o600
    assert "user@example.com" in out and str(path) in out
    for secret in ("access-secret", "refresh-secret", "code-secret", record["id_token"]):
        assert secret not in out + err


def test_login_reauthorizes_saved_registration(tmp_path):
    assert run_login(tmp_path, FakeOpenAI(), Browser())[0] == 0
    browser = Browser(lambda params: {"code": "code", "state": params["state"][0]})
    code, _, err = run_login(tmp_path, FakeOpenAI(), browser)
    assert code == 0, err
    assert browser.params["client_id"] == [ISSUED_CLIENT_ID]
    assert "agent_name_hint" not in browser.params


def test_login_without_plan_permission_saves_nothing_and_suggests_consent(tmp_path):
    def respond(fake):
        return httpx.Response(200, json=token_payload(
            fake.nonce, scope="openid profile email offline_access resource.invoke"))
    browser = Browser()

    def token_response(fake_self):
        fake_self.nonce = browser.params["nonce"][0]
        return respond(fake_self)
    code, _, err = run_login(tmp_path, FakeOpenAI(token_response=token_response), browser)
    assert code == 1
    assert "oauth_scope_missing" in err and "--consent" in err
    assert not (tmp_path / "chatgpt-credentials.json").exists()


def test_login_times_out_without_callback(tmp_path):
    out, err = io.StringIO(), io.StringIO()
    with FakeOpenAI().client() as client:
        code = cli.login(config_dir=tmp_path, port=0, consent=False, timeout=0.2,
                         http_client=client, open_browser=lambda url: None, out=out, err=err)
    assert code == 1 and "oauth_callback_timeout" in err.getvalue()


# --- CLI: import and smoke ---------------------------------------------------

def server_settings(**overrides):
    values = dict(_env_file=None, database_url="sqlite://",
                  token_encryption_key=Fernet.generate_key().decode(),
                  admin_api_token="a" * 32, chatgpt_host_id=HOST_ID,
                  chatgpt_model="gpt-test", responses_url="https://api.openai.com/v1/responses")
    values.update(overrides)
    return Settings(**values)


def server_factory():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def exported_record(**overrides):
    return {"email": "user@example.com", "issuer": CHATGPT_ISSUER, "subject": "user-subject",
            "client_id": ISSUED_CLIENT_ID,
            "ext_agent_host_id": "urn:uuid:00000000-0000-4000-8000-000000000000",
            "id_token": "id-secret", "access_token": "access-secret",
            "refresh_token": "refresh-secret", "token_type": "Bearer", "expires_in": 3600,
            "scopes": sorted(GRANTED_SCOPE.split()),
            "saved_at": datetime.now(timezone.utc).isoformat(), **overrides}


def test_import_command_stores_credentials_without_printing_tokens():
    settings, factory = server_settings(), server_factory()
    auth = OAuthService(settings, factory, None)
    out, err = io.StringIO(), io.StringIO()
    code = cli.import_credentials(io.StringIO(json.dumps(exported_record())), auth, out, err)
    assert code == 0, err.getvalue()
    with factory() as session:
        row = session.get(ChatGPTCredential, 1)
        assert row.client_id == ISSUED_CLIENT_ID and row.host_id == HOST_ID
    assert ISSUED_CLIENT_ID in out.getvalue() and HOST_ID in out.getvalue()
    assert "secret" not in out.getvalue() + err.getvalue()


@pytest.mark.parametrize("payload,error", [
    ("not-json", "oauth_import_invalid"),
    (json.dumps(exported_record(client_id="dynamic_agent_client")), "oauth_import_invalid"),
])
def test_import_command_reports_invalid_input(payload, error):
    auth = OAuthService(server_settings(), server_factory(), None)
    out, err = io.StringIO(), io.StringIO()
    assert cli.import_credentials(io.StringIO(payload), auth, out, err) == 1
    assert error in err.getvalue() and "secret" not in err.getvalue()


def smoke_fake(models_response=None, responses_status=200):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.headers["authorization"] == "Bearer access-secret"
        if str(request.url) == CHATGPT_MODELS_URL:
            return models_response or httpx.Response(200, json={"models": [
                {"slug": "gpt-test", "display_name": "GPT Test", "visibility": "list"},
                {"slug": "gpt-hidden", "display_name": "Hidden", "visibility": "hide"},
            ]})
        if str(request.url) == "https://api.openai.com/v1/responses":
            if responses_status != 200:
                return httpx.Response(responses_status, headers={"x-request-id": "req_1"})
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=(
                'data: {"type":"response.output_text.delta","delta":"OK"}\n\n'
                'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'))
        return httpx.Response(404)
    return handler, calls


def run_smoke(settings, handler):
    factory = server_factory()
    OAuthService(settings, factory, None).import_chatgpt(exported_record())
    out, err = io.StringIO(), io.StringIO()

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await cli.smoke(settings, OAuthService(settings, factory, client), client, out, err)
    return asyncio.run(scenario()), out.getvalue(), err.getvalue()


def test_smoke_checks_model_catalog_and_completes_one_response():
    handler, calls = smoke_fake()
    code, out, err = run_smoke(server_settings(), handler)
    assert code == 0, err
    assert [str(call.url) for call in calls] == [CHATGPT_MODELS_URL,
                                                 "https://api.openai.com/v1/responses"]
    assert "gpt-test" in out and "gpt-hidden" not in out
    assert "response.completed" in out
    assert "access-secret" not in out + err


def test_smoke_fails_when_configured_model_is_not_available():
    handler, calls = smoke_fake()
    code, out, err = run_smoke(server_settings(chatgpt_model="gpt-missing"), handler)
    assert code == 1 and "gpt-missing" in err
    assert [str(call.url) for call in calls] == [CHATGPT_MODELS_URL]


@pytest.mark.parametrize("models_response", [
    httpx.Response(403, json={"error": {"code": "subscription_sharing_user_not_eligible"}}),
    httpx.Response(200, json={"data": []}),
])
def test_smoke_reports_model_catalog_failures(models_response):
    handler, _ = smoke_fake(models_response=models_response)
    code, _, err = run_smoke(server_settings(), handler)
    assert code == 1 and "error_code=" in err


def test_smoke_reports_responses_failure_with_request_id():
    handler, _ = smoke_fake(responses_status=503)
    code, _, err = run_smoke(server_settings(), handler)
    assert code == 1 and "error_code=responses_api_error" in err


def test_cli_entrypoint_has_subcommands():
    parser = cli.build_parser()
    assert parser.parse_args(["login", "--port", "0", "--no-browser"]).command == "login"
    assert parser.parse_args(["import"]).command == "import"
    assert parser.parse_args(["smoke"]).command == "smoke"
