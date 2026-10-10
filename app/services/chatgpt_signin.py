"""Sign in with ChatGPT for open-source apps, run on the computer with the browser.

The flow follows https://developers.openai.com/siwc/token-sharing-open-source/sign-in
(checked 2026-10-11). It needs neither the database nor the server's encryption key.
"""
import base64
import contextlib
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlencode

import httpx
import jwt

from app.core.config import (CHATGPT_AUTHORIZE_URL, CHATGPT_ISSUER, CHATGPT_JWKS_URL,
                             CHATGPT_RESOURCE, CHATGPT_SCOPE, CHATGPT_TOKEN_URL, is_valid_host_id)
from app.services.auth import (DYNAMIC_CLIENT_ID, MAX_TOKEN_LENGTH, OAuthService, ServiceError,
                               has_control_characters, is_valid_lifetime)

AGENT_NAME = "x-ai-auto-post"
CALLBACK_PATH = "/auth/callback"
ID_TOKEN_ALGORITHM = "RS256"
ID_TOKEN_LEEWAY_SECONDS = 5
MAX_CODE_LENGTH = 8192
TIMEOUT = httpx.Timeout(30.0, connect=10.0)


@dataclass(frozen=True)
class Attempt:
    state: str
    nonce: str
    verifier: str
    redirect_uri: str
    client_id: str | None  # None while registering a new client


def callback_uri(port: int) -> str:
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    # Only the port may change between sign-ins, and localhost must not replace 127.0.0.1.
    return f"http://127.0.0.1:{port}{CALLBACK_PATH}"


def start(redirect_uri: str, host_id: str, client_id: str | None = None, *,
          consent: bool = False) -> tuple[str, Attempt]:
    """Build the authorization URL; reuse the issued client_id for a saved registration."""
    attempt = Attempt(state=secrets.token_urlsafe(32), nonce=secrets.token_urlsafe(32),
                      verifier=secrets.token_urlsafe(64), redirect_uri=redirect_uri,
                      client_id=client_id)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(attempt.verifier.encode()).digest()).rstrip(b"=").decode()
    params = {"response_type": "code", "client_id": client_id or DYNAMIC_CLIENT_ID}
    if client_id is None:
        params["agent_name_hint"] = AGENT_NAME
    params.update(ext_agent_host_id=host_id, redirect_uri=redirect_uri, scope=CHATGPT_SCOPE,
                  resource=CHATGPT_RESOURCE, state=attempt.state, nonce=attempt.nonce,
                  code_challenge=challenge, code_challenge_method="S256")
    if consent:
        params["prompt"] = "consent"
    return CHATGPT_AUTHORIZE_URL + "?" + urlencode(params, quote_via=quote), attempt


def finish(http_client: httpx.Client, attempt: Attempt, query, host_id: str, *,
           expected_subject: str | None = None) -> dict:
    """Validate the callback, exchange the code and return the credential record to save."""
    client_id = _callback_client_id(attempt, query)
    payload = _exchange_code(http_client, attempt, query["code"], client_id)
    saved_at = datetime.now(timezone.utc)
    _check_token_response(payload, client_id)
    claims = _verified_id_token(http_client, payload["id_token"], client_id, attempt.nonce)
    if expected_subject is not None and claims["sub"] != expected_subject:
        raise ServiceError("oauth_account_mismatch")
    email = claims.get("email")
    is_usable_email = isinstance(email, str) and len(email) <= 320 and not has_control_characters(email)
    return {
        "email": email if is_usable_email else None,
        "issuer": CHATGPT_ISSUER,
        "subject": claims["sub"],
        "client_id": client_id,
        "ext_agent_host_id": host_id,
        "id_token": payload["id_token"],
        "access_token": payload["access_token"],
        "refresh_token": payload["refresh_token"],
        "token_type": "Bearer",
        "expires_in": payload["expires_in"],
        "scopes": sorted(payload["scope"].split()),
        "saved_at": saved_at.isoformat(),
    }


def _callback_client_id(attempt, query):
    state = query.get("state")
    if not isinstance(state, str) or not hmac.compare_digest(state.encode(), attempt.state.encode()):
        raise ServiceError("oauth_state_invalid")
    if "error" in query:
        is_denied = query["error"] == "access_denied"
        raise ServiceError("oauth_access_denied" if is_denied else "oauth_authorization_failed")
    code = query.get("code")
    if not isinstance(code, str) or not code or len(code) > MAX_CODE_LENGTH:
        raise ServiceError("oauth_authorization_failed")
    returned = query.get("client_id")
    if attempt.client_id is None:
        is_issued = (isinstance(returned, str) and returned and returned != DYNAMIC_CLIENT_ID
                     and len(returned) <= 255 and not has_control_characters(returned))
        if not is_issued:
            raise ServiceError("oauth_registration_incomplete")
        return returned
    # A reauthorization callback may omit client_id but must never switch registrations.
    if returned is not None and returned != attempt.client_id:
        raise ServiceError("oauth_client_mismatch")
    return attempt.client_id


def _exchange_code(http_client, attempt, code, client_id):
    data = {"grant_type": "authorization_code", "code": code, "client_id": client_id,
            "code_verifier": attempt.verifier, "redirect_uri": attempt.redirect_uri,
            "resource": CHATGPT_RESOURCE}
    try:
        response = http_client.post(CHATGPT_TOKEN_URL, data=data, timeout=TIMEOUT,
                                    follow_redirects=False)
    except httpx.RequestError:
        raise ServiceError("oauth_transport_error", retryable=True) from None
    if response.status_code in (400, 401, 403):
        raise ServiceError("oauth_authorization_failed")
    if response.status_code != 200:
        raise ServiceError("oauth_endpoint_error", response.status_code == 429 or response.status_code >= 500)
    try:
        payload = response.json()
    except (ValueError, UnicodeError):
        raise ServiceError("oauth_token_invalid") from None
    if not isinstance(payload, dict):
        raise ServiceError("oauth_token_invalid")
    return payload


def _check_token_response(payload, client_id):
    for key in ("access_token", "refresh_token", "id_token"):
        value = payload.get(key)
        if not isinstance(value, str) or not value or len(value) > MAX_TOKEN_LENGTH:
            raise ServiceError("oauth_token_invalid")
    token_type = payload.get("token_type", "Bearer")
    if not is_valid_lifetime(payload.get("expires_in")) or not isinstance(token_type, str) \
            or token_type.lower() != "bearer":
        raise ServiceError("oauth_token_invalid")
    if payload.get("client_id", client_id) != client_id:
        raise ServiceError("oauth_client_mismatch")
    scope = payload.get("scope")
    # A valid ID token alone does not authorize ChatGPT plan usage.
    if not isinstance(scope, str) or OAuthService.DIRECT_SCOPE not in scope.split():
        raise ServiceError("oauth_scope_missing")


def _verified_id_token(http_client, token, client_id, nonce):
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        raise ServiceError("oauth_id_token_invalid") from None
    key_id = header.get("kid")
    if header.get("alg") != ID_TOKEN_ALGORITHM or not isinstance(key_id, str):
        raise ServiceError("oauth_id_token_invalid")
    key = _signing_key(http_client, key_id)
    try:
        claims = jwt.decode(token, key, algorithms=[ID_TOKEN_ALGORITHM], audience=client_id,
                            issuer=CHATGPT_ISSUER, leeway=ID_TOKEN_LEEWAY_SECONDS,
                            options={"require": ["iss", "aud", "sub", "exp", "iat"]})
    except jwt.PyJWTError:
        raise ServiceError("oauth_id_token_invalid") from None
    claimed_nonce = claims.get("nonce")
    if not isinstance(claimed_nonce, str) or not hmac.compare_digest(claimed_nonce.encode(), nonce.encode()):
        raise ServiceError("oauth_id_token_invalid")
    subject = claims.get("sub")
    if (not isinstance(subject, str) or not subject or len(subject) > 255
            or has_control_characters(subject)):
        raise ServiceError("oauth_id_token_invalid")
    return claims


def _signing_key(http_client, key_id):
    # Fetched fresh for every sign-in, so a rotated key never needs a cache refresh.
    try:
        response = http_client.get(CHATGPT_JWKS_URL, timeout=TIMEOUT, follow_redirects=False)
        if response.status_code != 200:
            raise ValueError("unexpected JWKS status")
        key_set = jwt.PyJWKSet.from_dict(response.json())
    except (httpx.RequestError, ValueError, TypeError, AttributeError, jwt.PyJWTError):
        raise ServiceError("oauth_jwks_unavailable", retryable=True) from None
    for key in key_set.keys:
        if key.key_id == key_id:
            return key.key
    raise ServiceError("oauth_id_token_invalid")


def load_or_create_host_id(path: Path) -> str:
    """Return this computer's stable ext_agent_host_id, creating it before the first sign-in."""
    try:
        value = path.read_text().strip()
    except FileNotFoundError:
        value = "urn:uuid:" + str(uuid.uuid4())
        _write_private(path, value + "\n")
        return value
    if not is_valid_host_id(value):
        raise ServiceError("oauth_host_id_invalid")
    return value


def read_record(path: Path) -> dict | None:
    try:
        record = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError, UnicodeError):
        raise ServiceError("oauth_credentials_invalid") from None
    if (not isinstance(record, dict) or not isinstance(record.get("client_id"), str)
            or not isinstance(record.get("subject"), str)):
        raise ServiceError("oauth_credentials_invalid")
    return record


def write_private_json(path: Path, data: dict) -> None:
    _write_private(path, json.dumps(data, indent=2) + "\n")


def _write_private(path, text):
    """Atomically replace path with owner-only permissions."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "w") as file:
            os.fchmod(file.fileno(), 0o600)
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
        raise
