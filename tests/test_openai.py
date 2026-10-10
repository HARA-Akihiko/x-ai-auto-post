import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from app.services.auth import ServiceError
from app.services.openai import ResponsesClient


class Auth:
    async def access_token(self, provider):
        assert provider == "chatgpt"
        return "oauth-secret"


def complete(handler, model="model", *, web_search=False):
    settings = SimpleNamespace(chatgpt_model=model, responses_url="https://api.example/responses")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return asyncio.run(ResponsesClient(settings, Auth(), client).complete(
        "isolated prompt", web_search=web_search))


def event(kind, **data):
    return "data: " + json.dumps({"type": kind, **data}) + "\n\n"


def test_completed_stream_and_payload():
    def handler(request):
        payload = json.loads(request.content)
        assert payload["stream"] is True and payload["store"] is False
        assert payload["model"] == "model" and "tools" not in payload
        # Token-sharing preview: input must be an array and system items are rejected.
        assert payload["input"] == [{"role": "user", "content": "isolated prompt"}]
        assert set(payload) == {"model", "input", "stream", "store"}
        assert request.headers["authorization"] == "Bearer " + "oauth-secret"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=(
            event("response.output_text.delta", delta="Hello ")
            + event("response.output_text.delta", delta="world")
            + event("response.completed", response={"status": "completed"})))
    assert complete(handler) == "Hello world"


@pytest.mark.parametrize("body,error", [
    (event("error", message="secret"), "responses_failed"),
    (event("response.failed", response={"error": "secret"}), "responses_failed"),
    (event("response.output_text.delta", delta="partial"), "responses_incomplete"),
    ("data: not-json\n\n", "responses_invalid"),
    ("data: " + "x" * 70000 + "\n\n", "responses_too_large"),
    (event("response.output_text.delta", delta="x" * 33000), "responses_too_large"),
])
def test_stream_errors(body, error):
    with pytest.raises(ServiceError, match=error):
        complete(lambda request: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, text=body))


def test_api_error_and_model_required():
    with pytest.raises(ServiceError) as caught:
        complete(lambda request: httpx.Response(429, text="secret-error"))
    assert caught.value.retryable
    assert "secret-error" not in str(caught.value)
    with pytest.raises(ServiceError, match="responses_not_configured"):
        complete(lambda request: pytest.fail("must not send"), model="")


def test_completed_output_without_deltas():
    body = event("response.completed", response={"status": "completed", "output": [{
        "type": "message", "content": [{"type": "output_text", "text": "Result"}]}]})
    assert complete(lambda request: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, text=body)) == "Result"


def test_web_search_is_explicit_opt_in():
    def handler(request):
        payload = json.loads(request.content)
        assert payload["tools"] == [{"type": "web_search"}]
        assert "temperature" not in payload and "max_output_tokens" not in payload
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=(
            event("response.output_text.delta", delta="Search result")
            + event("response.completed", response={"status": "completed"})))
    assert complete(handler, web_search=True) == "Search result"


@pytest.mark.parametrize("status,code,retryable", [
    (401, "responses_unauthorized", False),
    (403, "responses_forbidden", False),
    (429, "chatgpt_usage_limit", True),
    (500, "responses_api_error", True),
    (503, "responses_api_error", True),
    (400, "responses_api_error", False),
])
def test_typed_status_errors(status, code, retryable):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, text="sensitive-error-body")
    with pytest.raises(ServiceError) as caught:
        complete(handler)
    assert caught.value.error_code == code
    assert caught.value.retryable is retryable
    assert "sensitive-error-body" not in str(caught.value)
    assert len(calls) == 1


# Codes with documented recovery actions:
# https://developers.openai.com/siwc/token-sharing-open-source/errors-and-recovery
STRUCTURED_ERRORS = [
    (403, "subscription_sharing_user_not_eligible", False),
    (429, "subscription_sharing_usage_limit_exceeded", True),
    (503, "subscription_sharing_usage_unavailable", True),
    (400, "subscription_sharing_unsupported_capability", False),
    (403, "subscription_sharing_route_not_supported", False),
    (401, "subscription_sharing_invalid_user", False),
    (403, "chatpass_v2_scope_not_authorized", False),
    (403, "chatpass_v2_invalid_authorization_context", False),
    (503, "subscription_sharing_user_unavailable", True),
]


@pytest.mark.parametrize("status,code,retryable", STRUCTURED_ERRORS)
def test_structured_http_error_code_is_preserved(status, code, retryable):
    def handler(request):
        return httpx.Response(status, headers={"x-request-id": "req_123"}, json={
            "error": {"code": code, "param": None, "message": "sensitive-error-body"}})
    with pytest.raises(ServiceError) as caught:
        complete(handler)
    assert caught.value.error_code == code
    assert caught.value.retryable is retryable
    assert caught.value.request_id == "req_123"
    assert "sensitive-error-body" not in str(caught.value)


@pytest.mark.parametrize("status,body,code,retryable", [
    (403, {"error": {"code": "undocumented_code"}}, "responses_forbidden", False),
    (503, {"detail": "Direct routing is unavailable"}, "responses_api_error", True),
    (429, {"error": {"code": ["not", "a", "string"]}}, "chatgpt_usage_limit", True),
    (400, {"error": "subscription_sharing_unsupported_capability"}, "responses_api_error", False),
])
def test_unknown_or_unstructured_http_errors_keep_generic_codes(status, body, code, retryable):
    with pytest.raises(ServiceError) as caught:
        complete(lambda request: httpx.Response(status, json=body))
    assert caught.value.error_code == code
    assert caught.value.retryable is retryable
    assert caught.value.request_id is None


def test_oversized_error_body_is_not_parsed():
    body = json.dumps({"error": {"code": "subscription_sharing_user_not_eligible"},
                       "padding": "x" * 70000})
    with pytest.raises(ServiceError) as caught:
        complete(lambda request: httpx.Response(403, text=body))
    assert caught.value.error_code == "responses_forbidden"


@pytest.mark.parametrize("status,code,retryable", STRUCTURED_ERRORS)
def test_structured_code_in_failed_stream_is_preserved(status, code, retryable):
    body = (event("response.output_text.delta", delta="partial")
            + event("response.failed", response={"status": "failed", "error": {
                "code": code, "message": "sensitive-error-body"}}))
    with pytest.raises(ServiceError) as caught:
        complete(lambda request: httpx.Response(
            200, headers={"content-type": "text/event-stream", "x-request-id": "req_456"},
            text=body))
    assert caught.value.error_code == code
    assert caught.value.retryable is retryable
    assert caught.value.request_id == "req_456"


def test_structured_code_in_error_event_is_preserved():
    body = event("error", code="subscription_sharing_usage_limit_exceeded", message="secret")
    with pytest.raises(ServiceError) as caught:
        complete(lambda request: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, text=body))
    assert caught.value.error_code == "subscription_sharing_usage_limit_exceeded"
    assert caught.value.retryable


def test_failure_log_has_code_and_request_id_but_no_body(caplog):
    def handler(request):
        return httpx.Response(429, headers={"x-request-id": "req_789"}, json={"error": {
            "code": "subscription_sharing_usage_limit_exceeded", "message": "sensitive-error-body"}})
    with caplog.at_level("WARNING", logger="app.services.openai"), pytest.raises(ServiceError):
        complete(handler)
    records = [json.loads(record.message) for record in caplog.records
               if record.name == "app.services.openai"]
    assert records == [{"event": "responses_failed", "http_status": 429, "request_id": "req_789",
                        "error_code": "subscription_sharing_usage_limit_exceeded"}]
    assert "sensitive-error-body" not in caplog.text
    assert "oauth-secret" not in caplog.text


def test_unsafe_request_id_is_dropped():
    def handler(request):
        return httpx.Response(500, headers={"x-request-id": "r" * 129})
    with pytest.raises(ServiceError) as caught:
        complete(handler)
    assert caught.value.request_id is None


def test_end_to_end_timeout_stops_trickled_heartbeats(monkeypatch):
    monkeypatch.setattr(ResponsesClient, "TOTAL_TIMEOUT_SECONDS", 0.02)
    class HeartbeatStream(httpx.AsyncByteStream):
        closed = False
        async def __aiter__(self):
            while True:
                await asyncio.sleep(0.001)
                yield b": heartbeat\n\n"
        async def aclose(self):
            self.closed = True
    stream = HeartbeatStream()
    with pytest.raises(ServiceError) as caught:
        complete(lambda request: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=stream))
    assert caught.value.error_code == "responses_transport_error"
    assert caught.value.retryable
    assert stream.closed
