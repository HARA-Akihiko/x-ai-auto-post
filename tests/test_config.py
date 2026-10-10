import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from app.core import config
from app.core.config import Settings


def settings(**kwargs):
    return Settings(_env_file=None, database_url="postgresql+psycopg://localhost/test",
                    token_encryption_key=Fernet.generate_key().decode(),
                    admin_api_token="a" * 32, **kwargs)


def test_safe_defaults_and_bounded_ai_usage():
    value = settings()
    assert value.timezone == "Asia/Tokyo"
    assert value.post_times == "08:00,13:00,19:00"
    assert value.candidate_limit == 3
    assert "TOKEN" not in repr(value.admin_api_token)


def test_chatgpt_token_sharing_values_follow_official_flow():
    # https://developers.openai.com/siwc/token-sharing-open-source/sign-in (checked 2026-10-11)
    assert config.CHATGPT_ISSUER == "https://auth.openai.com"
    assert config.CHATGPT_AUTHORIZE_URL == "https://auth.openai.com/api/accounts/authorize"
    assert config.CHATGPT_TOKEN_URL == "https://auth.openai.com/api/accounts/oauth/token"
    assert config.CHATGPT_JWKS_URL == "https://auth.openai.com/.well-known/jwks.json"
    assert config.CHATGPT_RESOURCE == "https://api.openai.com/v1"
    assert config.CHATGPT_SCOPE.split() == [
        "openid", "profile", "email", "offline_access", "resource.invoke",
        "chatgpt.tokens.use.direct"]


def test_obsolete_chatgpt_settings_in_existing_env_are_ignored():
    value = settings(chatgpt_authorize_url="https://auth.openai.com/oauth/authorize",
                     chatgpt_token_url="https://auth.openai.com/oauth/token",
                     chatgpt_client_id="pre-registered", chatgpt_client_secret="secret",
                     chatgpt_redirect_uri="http://localhost:8000/auth/chatgpt/callback")
    for name in ("chatgpt_authorize_url", "chatgpt_token_url", "chatgpt_client_id",
                 "chatgpt_client_secret", "chatgpt_redirect_uri", "chatgpt_scope",
                 "chatgpt_resource"):
        assert not hasattr(value, name)


@pytest.mark.parametrize("host_id", [
    "",
    "urn:uuid:123e4567-e89b-42d3-a456-426614174000",
    "urn:uuid:123E4567-E89B-42D3-A456-426614174000",
    "urn:ietf:params:oauth:jwk-thumbprint:sha-256:NzbLsXh8uDCcd-6MNwXF4W_7noWXFZAfHkxZsRGC9Xs",
    "did:key:z6MkhaXgBZDvotDkL5257faiztiGiC2QtKLGpbnnEGta2doK",
    "urn:ietf:params:oauth:jwk-thumbprint:sha-256:" + "a" * 210,
])
def test_valid_chatgpt_host_ids(host_id):
    assert settings(chatgpt_host_id=host_id).chatgpt_host_id == host_id


@pytest.mark.parametrize("kwargs", [
    {"candidate_limit": 0}, {"candidate_limit": 6}, {"post_times": "25:00"},
    {"post_times": "08:00,08:00"}, {"timezone": "../../invalid"},
    {"article_max_age_days": 0},
    {"responses_url": "http://example.com/responses"},
    {"x_redirect_uri": "http://example.com/callback"},
    {"chatgpt_host_id": "registered-host"},
    {"chatgpt_host_id": "user@example.com"},
    {"chatgpt_host_id": "urn:uuid:not-a-uuid"},
    {"chatgpt_host_id": "urn:uuid:123e4567-e89b-42d3-a456-426614174000\n"},
    {"chatgpt_host_id": "did:web:example.com"},
    {"chatgpt_host_id": "urn:ietf:params:oauth:jwk-thumbprint:sha-256:" + "a" * 211},
])
def test_invalid_settings_rejected(kwargs):
    with pytest.raises((ValidationError, ValueError, KeyError)):
        settings(**kwargs)
