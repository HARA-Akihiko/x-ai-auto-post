from enum import StrEnum
from functools import lru_cache
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet
from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class URLPolicy(StrEnum):
    ALWAYS = "ALWAYS"
    IMPORTANT_ONLY = "IMPORTANT_ONLY"
    NONE = "NONE"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore",
                                     hide_input_in_errors=True)

    database_url: SecretStr
    token_encryption_key: SecretStr
    admin_api_token: SecretStr
    timezone: str = "Asia/Tokyo"
    post_times: str = "08:00,13:00,19:00"
    url_policy: URLPolicy = URLPolicy.ALWAYS
    candidate_limit: int = 3
    article_max_age_days: int = 7
    web_search_enabled: bool = False
    chatgpt_client_id: str = ""
    chatgpt_client_secret: SecretStr = SecretStr("")
    chatgpt_authorize_url: str = "https://auth.openai.com/oauth/authorize"
    chatgpt_token_url: str = "https://auth.openai.com/oauth/token"
    chatgpt_redirect_uri: str = "http://localhost:8000/auth/chatgpt/callback"
    chatgpt_scope: str = "openid offline_access resource.invoke chatgpt.tokens.use.direct"
    chatgpt_resource: str = "https://api.openai.com/v1"
    chatgpt_host_id: str = ""
    chatgpt_model: str = ""
    responses_url: str = "https://api.openai.com/v1/responses"
    x_client_id: str = ""
    x_client_secret: SecretStr = SecretStr("")
    x_redirect_uri: str = "http://localhost:8000/auth/x/callback"
    x_authorize_url: str = "https://x.com/i/oauth2/authorize"
    x_token_url: str = "https://api.x.com/2/oauth2/token"
    x_api_url: str = "https://api.x.com/2"
    x_scope: str = "tweet.read tweet.write users.read offline.access"

    @field_validator("token_encryption_key")
    @classmethod
    def encryption_key(cls, value: SecretStr) -> SecretStr:
        Fernet(value.get_secret_value().encode())
        return value

    @field_validator("admin_api_token")
    @classmethod
    def admin_token(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 32:
            raise ValueError("ADMIN_API_TOKEN must contain at least 32 characters")
        return value

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        ZoneInfo(value)
        return value

    @field_validator("chatgpt_authorize_url", "chatgpt_token_url", "chatgpt_resource", "responses_url",
                     "x_authorize_url", "x_token_url", "x_api_url")
    @classmethod
    def secure_endpoint(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.fragment or any(char.isspace() for char in value)):
            raise ValueError("OAuth and API endpoints must use HTTPS without URL credentials")
        return value

    @field_validator("chatgpt_redirect_uri", "x_redirect_uri")
    @classmethod
    def secure_redirect(cls, value: str) -> str:
        parsed = urlsplit(value)
        local_http = parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if (not parsed.hostname or parsed.username or parsed.password or parsed.fragment
                or not (parsed.scheme == "https" or local_http)
                or any(char.isspace() for char in value)):
            raise ValueError("OAuth callback requires HTTPS except on loopback")
        return value

    @field_validator("candidate_limit")
    @classmethod
    def bounded_candidates(cls, value: int) -> int:
        if not 1 <= value <= 5:
            raise ValueError("candidate_limit must be between 1 and 5")
        return value

    @field_validator("article_max_age_days")
    @classmethod
    def bounded_age(cls, value: int) -> int:
        if not 1 <= value <= 30:
            raise ValueError("article_max_age_days must be between 1 and 30")
        return value

    @field_validator("post_times")
    @classmethod
    def valid_times(cls, value: str) -> str:
        times = value.split(",")
        if not times or len(times) != len(set(times)):
            raise ValueError("post times must be unique")
        for item in times:
            hour, minute = item.split(":")
            if len(item) != 5 or not 0 <= int(hour) < 24 or not 0 <= int(minute) < 60:
                raise ValueError("post times must use HH:MM")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
