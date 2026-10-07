import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

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


@pytest.mark.parametrize("kwargs", [
    {"candidate_limit": 0}, {"candidate_limit": 6}, {"post_times": "25:00"},
    {"post_times": "08:00,08:00"}, {"timezone": "../../invalid"},
    {"article_max_age_days": 0},
    {"responses_url": "http://example.com/responses"},
    {"x_redirect_uri": "http://example.com/callback"},
])
def test_invalid_settings_rejected(kwargs):
    with pytest.raises((ValidationError, ValueError, KeyError)):
        settings(**kwargs)
