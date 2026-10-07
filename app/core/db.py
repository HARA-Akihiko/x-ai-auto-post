from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings


@lru_cache
def get_engine():
    url = get_settings().database_url.get_secret_value()
    if not url.startswith("postgresql"):
        raise ValueError("PostgreSQL is required for cross-process locking")
    return create_engine(url, pool_pre_ping=True, hide_parameters=True)


@lru_cache
def get_session_factory():
    return sessionmaker(get_engine(), expire_on_commit=False)


def get_session():
    with get_session_factory()() as session:
        yield session
