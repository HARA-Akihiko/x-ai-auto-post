import asyncio
import json
import logging

import httpx

from app.services.auth import ServiceError

logger = logging.getLogger(__name__)

# Codes with documented recovery actions, mapped to whether a later retry may succeed:
# https://developers.openai.com/siwc/token-sharing-open-source/errors-and-recovery
STRUCTURED_ERROR_RETRYABLE = {
    "subscription_sharing_user_not_eligible": False,
    "subscription_sharing_usage_limit_exceeded": True,
    "subscription_sharing_usage_unavailable": True,
    "subscription_sharing_unsupported_capability": False,
    "subscription_sharing_route_not_supported": False,
    "subscription_sharing_invalid_user": False,
    "chatpass_v2_scope_not_authorized": False,
    "chatpass_v2_invalid_authorization_context": False,
    "subscription_sharing_user_unavailable": True,
}
MAX_REQUEST_ID_LENGTH = 128


def _known_error_code(error):
    code = error.get("code") if isinstance(error, dict) else None
    return code if isinstance(code, str) and code in STRUCTURED_ERROR_RETRYABLE else None


def _structured_error(code):
    return ServiceError(code, STRUCTURED_ERROR_RETRYABLE[code])


def _request_id(response):
    value = response.headers.get("x-request-id")
    is_safe = (value and len(value) <= MAX_REQUEST_ID_LENGTH
               and value.isascii() and value.isprintable())
    return value if is_safe else None


class ResponsesClient:
    MAX_EVENT_BYTES = 65536
    MAX_STREAM_BYTES = 1048576
    MAX_TEXT_LENGTH = 32768
    TOTAL_TIMEOUT_SECONDS = 90
    TIMEOUT = httpx.Timeout(60.0, connect=10.0)

    def __init__(self, settings, auth_service, http_client):
        self.settings = settings
        self.auth_service = auth_service
        self.http_client = http_client

    async def complete(self, prompt: str, *, web_search: bool = False) -> str:
        if not self.settings.chatgpt_model.strip() or not self.settings.responses_url:
            raise ServiceError("responses_not_configured")
        access = await self.auth_service.access_token("chatgpt")
        # Token-sharing preview requires an input array; system-role items are rejected.
        payload = {"model": self.settings.chatgpt_model,
                   "input": [{"role": "user", "content": prompt}],
                   "stream": True, "store": False}
        if web_search:
            payload["tools"] = [{"type": "web_search"}]
        try:
            async with asyncio.timeout(self.TOTAL_TIMEOUT_SECONDS):
                async with self.http_client.stream(
                    "POST", self.settings.responses_url,
                    headers={"Authorization": "Bearer " + access, "Accept": "text/event-stream"},
                    json=payload,
                    timeout=self.TIMEOUT, follow_redirects=False,
                ) as response:
                    try:
                        return await self._handle(response)
                    except ServiceError as exc:
                        exc.request_id = _request_id(response)
                        logger.warning(json.dumps({
                            "event": "responses_failed", "http_status": response.status_code,
                            "request_id": exc.request_id, "error_code": exc.error_code,
                        }))
                        raise
        except (httpx.RequestError, TimeoutError):
            raise ServiceError("responses_transport_error", retryable=True) from None

    async def _handle(self, response):
        if response.status_code != 200:
            code = await self._error_body_code(response)
            if code:
                raise _structured_error(code)
            if response.status_code == 401:
                raise ServiceError("responses_unauthorized")
            if response.status_code == 403:
                raise ServiceError("responses_forbidden")
            if response.status_code == 429:
                raise ServiceError("chatgpt_usage_limit", retryable=True)
            raise ServiceError("responses_api_error", response.status_code >= 500)
        if "text/event-stream" not in response.headers.get("content-type", "").lower():
            raise ServiceError("responses_invalid")
        return await self._read(response)

    async def _error_body_code(self, response):
        body = bytearray()
        async for chunk in response.aiter_bytes(chunk_size=4096):
            body.extend(chunk)
            if len(body) > self.MAX_EVENT_BYTES:
                return None
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeError):
            return None
        return _known_error_code(payload.get("error")) if isinstance(payload, dict) else None

    async def _read(self, response):
        pending = bytearray()
        data = []
        event_name = ""
        event_bytes = total = text_length = 0
        parts = []
        async for chunk in response.aiter_bytes(chunk_size=4096):
            total += len(chunk)
            if total > self.MAX_STREAM_BYTES:
                raise ServiceError("responses_too_large")
            pending.extend(chunk)
            while b"\n" in pending:
                raw, _, rest = pending.partition(b"\n")
                pending = bytearray(rest)
                event_bytes += len(raw) + 1
                if event_bytes > self.MAX_EVENT_BYTES:
                    raise ServiceError("responses_too_large")
                try:
                    line = raw.rstrip(b"\r").decode("utf-8")
                except UnicodeError:
                    raise ServiceError("responses_invalid") from None
                if line.startswith("data:"):
                    data.append(line[5:].removeprefix(" "))
                elif line.startswith("event:"):
                    event_name = line[6:].strip()
                elif not line:
                    if data:
                        try:
                            payload = json.loads("\n".join(data))
                        except ValueError:
                            raise ServiceError("responses_invalid") from None
                        if not isinstance(payload, dict):
                            raise ServiceError("responses_invalid")
                        kind = payload.get("type", event_name)
                        if kind in ("error", "response.failed", "response.incomplete"):
                            raise self._stream_error(kind, payload)
                        if kind == "response.output_text.delta":
                            delta = payload.get("delta")
                            if not isinstance(delta, str):
                                raise ServiceError("responses_invalid")
                            text_length += len(delta)
                            if text_length > self.MAX_TEXT_LENGTH:
                                raise ServiceError("responses_too_large")
                            parts.append(delta)
                        elif kind == "response.completed":
                            result = payload.get("response")
                            if not isinstance(result, dict) or result.get("status") != "completed":
                                raise ServiceError("responses_failed")
                            if not parts:
                                parts = self._output(result)
                            text = "".join(parts)
                            if not text or len(text) > self.MAX_TEXT_LENGTH:
                                raise ServiceError("responses_too_large" if text else "responses_empty")
                            return text
                    data, event_name, event_bytes = [], "", 0
            if len(pending) + event_bytes > self.MAX_EVENT_BYTES:
                raise ServiceError("responses_too_large")
        raise ServiceError("responses_incomplete")

    def _stream_error(self, kind, payload):
        # Usage-limit errors can arrive as response.failed after streaming has begun.
        if kind == "response.failed":
            result = payload.get("response")
            error = result.get("error") if isinstance(result, dict) else None
        elif kind == "error":
            error = payload.get("error", payload)
        else:
            error = None
        code = _known_error_code(error)
        return _structured_error(code) if code else ServiceError("responses_failed")

    def _output(self, response):
        output = response.get("output", [])
        if not isinstance(output, list):
            raise ServiceError("responses_invalid")
        parts = []
        for item in output:
            if not isinstance(item, dict):
                raise ServiceError("responses_invalid")
            if item.get("type") != "message":
                continue
            content = item.get("content", [])
            if not isinstance(content, list):
                raise ServiceError("responses_invalid")
            for piece in content:
                if not isinstance(piece, dict):
                    raise ServiceError("responses_invalid")
                if piece.get("type") == "output_text":
                    if not isinstance(piece.get("text"), str):
                        raise ServiceError("responses_invalid")
                    parts.append(piece["text"])
        return parts
