import os
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.core.config import get_settings
from app.core.db import get_engine, get_session_factory


@pytest.mark.integration
def test_migration_roundtrip_preserves_drafts_and_initial_feeds(monkeypatch):
    database = os.environ.get("TEST_DATABASE_URL")
    if not database:
        pytest.skip("TEST_DATABASE_URL is required")
    schema = "migration_test_" + uuid.uuid4().hex
    admin = create_engine(database, hide_parameters=True)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    url = make_url(database).update_query_dict({"options": f"-csearch_path={schema}"})
    monkeypatch.setenv("DATABASE_URL", url.render_as_string(hide_password=False))
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("ADMIN_API_TOKEN", uuid.uuid4().hex)
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    try:
        command.upgrade(config, "0e7b81141fb1")
        engine = get_engine()
        with engine.begin() as connection:
            assert connection.scalar(text("SELECT count(*) FROM sources")) == 6
            connection.execute(text(
                "INSERT INTO post_drafts (slot,text,status,created_at) "
                "VALUES ('existing-slot','draft','generated',CURRENT_TIMESTAMP)"
            ))
            connection.execute(text(
                "INSERT INTO chatgpt_credentials "
                "(id,access_token,expires_at,scope,client_id,host_id) "
                "VALUES (1,'encrypted-legacy',CURRENT_TIMESTAMP,'openid','pre-registered','host')"
            ))
        command.upgrade(config, "head")
        with engine.connect() as connection:
            assert connection.scalar(text(
                "SELECT reconcile_attempts FROM post_drafts WHERE slot='existing-slot'"
            )) == 0
            # Credentials from the old browser flow stay, marked legacy by a NULL subject.
            assert tuple(connection.execute(text(
                "SELECT access_token, subject, email, issuer FROM chatgpt_credentials"
            )).one()) == ("encrypted-legacy", None, None, None)
        command.downgrade(config, "0e7b81141fb1")
        command.upgrade(config, "head")
        command.check(config)
        command.downgrade(config, "base")
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT to_regclass('post_drafts')")) is None
    finally:
        get_engine().dispose()
        get_settings.cache_clear()
        get_engine.cache_clear()
        get_session_factory.cache_clear()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
