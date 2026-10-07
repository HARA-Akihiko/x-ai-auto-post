import asyncio
import base64
import hashlib
import json
import math
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.models import ChatGPTCredential, OAuthState, XCredential, utcnow


class ServiceError(Exception):
    def __init__(self, error_code: str, retryable: bool = False):
        self.error_code = error_code
        self.retryable = retryable
        super().__init__(error_code)


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise ServiceError("oauth_credentials_invalid")
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class OAuthService:
    STATE_TTL = timedelta(minutes=10)
    TIMEOUT = httpx.Timeout(30.0, connect=10.0)
    X_SCOPES = "tweet.read tweet.write users.read offline.access"
    DIRECT_SCOPE = "chatgpt.tokens.use.direct"

    def __init__(self, settings, session_factory, http_client):
        self.settings = settings
        self.session_factory = session_factory
        self.http_client = http_client
        self.cipher = Fernet(settings.token_encryption_key.get_secret_value().encode())

    def _model(self, provider):
        if provider == "chatgpt":
            return ChatGPTCredential
        if provider == "x":
            return XCredential
        raise ServiceError("oauth_provider_invalid")

    def _config(self, provider):
        self._model(provider)
        client = getattr(self.settings, f"{provider}_client_id")
        host = self.settings.chatgpt_host_id if provider == "chatgpt" else ""
        scope = self.settings.chatgpt_scope if provider == "chatgpt" else self.X_SCOPES
        if (not client.strip() or len(client) > 255
                or (provider == "chatgpt" and (not host.strip() or len(host) > 255))):
            raise ServiceError("oauth_not_configured")
        if provider == "chatgpt" and self.DIRECT_SCOPE not in scope.split():
            raise ServiceError("oauth_scope_missing")
        if provider == "chatgpt" and not self.settings.chatgpt_resource.strip():
            raise ServiceError("oauth_not_configured")
        return client, host, scope

    def _lock(self, session, provider):
        model = self._model(provider)
        dialect = session.get_bind().dialect.name
        insert = pg_insert if dialect == "postgresql" else sqlite_insert
        # Keep a singleton tombstone when disconnected so every mutation locks
        # the same row, including the first authorization and concurrent refresh.
        session.execute(insert(model).values(
            id=1, access_token="", expires_at=utcnow(), scope="", client_id="", host_id=""
        ).on_conflict_do_nothing(index_elements=["id"]))
        return session.scalars(select(model).where(model.id == 1).with_for_update()).one()

    async def _async_lock(self, session, provider):
        if session.get_bind().dialect.name == "postgresql":
            # A contended synchronous DB lock must not block the event loop
            # while its current owner awaits the OAuth HTTP response.
            task = asyncio.create_task(asyncio.to_thread(self._lock, session, provider))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise
        return self._lock(session, provider)

    def _encrypt(self, value):
        return self.cipher.encrypt(value.encode()).decode() if value is not None else None

    def _decrypt(self, value):
        try:
            return self.cipher.decrypt(value.encode()).decode()
        except (InvalidToken, AttributeError, UnicodeError, ValueError):
            raise ServiceError("oauth_credentials_invalid") from None

    def login(self, provider: str) -> str:
        client, host, scope = self._config(provider)
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        context = {"verifier": verifier, "client_id": client, "host_id": host,
                   "redirect_uri": getattr(self.settings, f"{provider}_redirect_uri"),
                   "resource": self.settings.chatgpt_resource if provider == "chatgpt" else ""}
        with self.session_factory.begin() as session:
            self._lock(session, provider)
            session.execute(delete(OAuthState).where(OAuthState.provider == provider))
            session.add(OAuthState(
                state_hash=hashlib.sha256(state.encode()).hexdigest(),
                provider=provider, verifier=self._encrypt(json.dumps(context)),
                expires_at=utcnow() + self.STATE_TTL,
            ))
        params = dict(response_type="code", client_id=client,
                      redirect_uri=getattr(self.settings, f"{provider}_redirect_uri"),
                      scope=scope, state=state, code_challenge=challenge,
                      code_challenge_method="S256")
        if host:
            params["ext_agent_host_id"] = host
        if provider == "chatgpt":
            params["resource"] = context["resource"]
        url = getattr(self.settings, f"{provider}_authorize_url")
        return url + ("&" if "?" in url else "?") + urlencode(params)

    async def _request_token(self, provider, data):
        secret = getattr(self.settings, f"{provider}_client_secret").get_secret_value()
        auth = None
        if secret:
            if provider == "x":
                auth = httpx.BasicAuth(data["client_id"], secret)
            else:
                data["client_secret"] = secret
        try:
            response = await self.http_client.post(
                getattr(self.settings, f"{provider}_token_url"),
                data=data, auth=auth, timeout=self.TIMEOUT, follow_redirects=False,
            )
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

    def _save(self, row, provider, payload, client, host, refreshing=False):
        access = payload.get("access_token")
        expires = payload.get("expires_in")
        if (not isinstance(access, str) or not access or isinstance(expires, bool)
                or not isinstance(expires, (int, float)) or expires <= 0
                or expires > 31536000 or not math.isfinite(expires)):
            raise ServiceError("oauth_token_invalid")
        if "token_type" in payload and str(payload["token_type"]).lower() != "bearer":
            raise ServiceError("oauth_token_invalid")
        scope = payload.get("scope", row.scope if refreshing else "")
        if not isinstance(scope, str):
            raise ServiceError("oauth_scope_missing")
        if provider == "chatgpt" and self.DIRECT_SCOPE not in scope.split():
            raise ServiceError("oauth_scope_missing")
        if payload.get("client_id", client) != client:
            raise ServiceError("oauth_client_mismatch")
        host = payload.get("ext_agent_host_id", host)
        if (not isinstance(host, str) or len(host) > 255
                or any(ord(character) < 32 or ord(character) == 127 for character in host)
                or (provider == "chatgpt" and not host)):
            raise ServiceError("oauth_token_invalid")
        for key in ("refresh_token", "id_token"):
            if key in payload and (not isinstance(payload[key], str) or not payload[key]):
                raise ServiceError("oauth_token_invalid")
        row.access_token = self._encrypt(access)
        if not refreshing or "refresh_token" in payload:
            row.refresh_token = self._encrypt(payload.get("refresh_token"))
        if not refreshing or "id_token" in payload:
            row.id_token = self._encrypt(payload.get("id_token"))
        row.expires_at = utcnow() + timedelta(seconds=expires)
        row.scope, row.client_id, row.host_id = scope, client, host

    async def callback(self, provider: str, state: str, code: str) -> None:
        self._model(provider)
        if not isinstance(state, str) or not state or len(state) > 512:
            raise ServiceError("oauth_state_invalid")
        failure = None
        with self.session_factory.begin() as session:
            row = await self._async_lock(session, provider)
            stored = session.scalars(select(OAuthState).where(
                OAuthState.state_hash == hashlib.sha256(state.encode()).hexdigest(),
                OAuthState.provider == provider,
            ).with_for_update()).one_or_none()
            if stored is None:
                raise ServiceError("oauth_state_invalid")
            session.delete(stored)
            # Commit consumed state even when the authorization endpoint rejects it.
            try:
                if _utc(stored.expires_at) <= utcnow():
                    raise ServiceError("oauth_state_invalid")
                if not isinstance(code, str) or not code or len(code) > 8192:
                    raise ServiceError("oauth_authorization_failed")
                context = json.loads(self._decrypt(stored.verifier))
                if (not isinstance(context, dict)
                        or any(not isinstance(context.get(key), str)
                               for key in ("client_id", "verifier", "host_id", "redirect_uri", "resource"))):
                    raise ServiceError("oauth_state_invalid")
                data = dict(grant_type="authorization_code", code=code,
                            client_id=context["client_id"], code_verifier=context["verifier"],
                            redirect_uri=context["redirect_uri"])
                if context["host_id"]:
                    data["ext_agent_host_id"] = context["host_id"]
                if provider == "chatgpt":
                    data["resource"] = context["resource"]
                payload = await self._request_token(provider, data)
                self._save(row, provider, payload, context["client_id"], context["host_id"])
            except (ServiceError, asyncio.CancelledError) as exc:
                failure = exc
            except (ValueError, KeyError, TypeError):
                failure = ServiceError("oauth_state_invalid")
        if failure:
            raise failure

    def status(self, provider: str) -> dict:
        with self.session_factory() as session:
            row = session.get(self._model(provider), 1)
            if row is None or not row.access_token:
                return {"provider": provider, "connected": False}
            valid_scope = provider != "chatgpt" or self.DIRECT_SCOPE in row.scope.split()
            return dict(provider=provider, connected=valid_scope,
                        expires_at=_utc(row.expires_at).isoformat(),
                        expired=_utc(row.expires_at) <= utcnow(), scope=row.scope,
                        client_id=row.client_id, host_id=row.host_id)

    def disconnect(self, provider: str) -> None:
        with self.session_factory.begin() as session:
            row = self._lock(session, provider)
            row.access_token, row.refresh_token, row.id_token = "", None, None
            row.scope, row.client_id, row.host_id = "", "", ""
            row.expires_at = utcnow()
            session.execute(delete(OAuthState).where(OAuthState.provider == provider))

    async def access_token(self, provider: str) -> str:
        with self.session_factory.begin() as session:
            row = await self._async_lock(session, provider)
            if not row.access_token:
                raise ServiceError("oauth_not_connected")
            if provider == "chatgpt" and self.DIRECT_SCOPE not in row.scope.split():
                raise ServiceError("oauth_scope_missing")
            if _utc(row.expires_at) > utcnow():
                return self._decrypt(row.access_token)
            if not row.refresh_token:
                raise ServiceError("oauth_reauthorization_required")
            data = dict(grant_type="refresh_token", refresh_token=self._decrypt(row.refresh_token),
                        client_id=row.client_id)
            if row.host_id:
                data["ext_agent_host_id"] = row.host_id
            if provider == "chatgpt":
                if not self.settings.chatgpt_resource.strip():
                    raise ServiceError("oauth_not_configured")
                data["resource"] = self.settings.chatgpt_resource
            payload = await self._request_token(provider, data)
            self._save(row, provider, payload, row.client_id, row.host_id, refreshing=True)
            return self._decrypt(row.access_token)
