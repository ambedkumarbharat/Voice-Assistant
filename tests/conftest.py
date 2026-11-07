from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from app.config import load_settings  # noqa: E402
from app.db import Store  # noqa: E402
from app.providers import build_providers  # noqa: E402


@pytest.fixture
def config(tmp_path):
    return load_settings(
        env="test",
        is_production=False,
        data_dir=tmp_path,
        db_path=str(tmp_path / "voxdesk.db"),
        log_level="silent",
        api_keys=["test-key"],
        allow_anonymous_dashboard=False,
    )


@pytest.fixture
def store():
    database = Store(":memory:")
    yield database
    database.close()


@pytest.fixture
def providers(config):
    return build_providers(config)


@pytest.fixture
def app(config, store, providers):
    from app.api import create_app

    return create_app(settings=config, store=store, providers=providers)


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def auth():
    return {"authorization": "Bearer test-key"}
