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
