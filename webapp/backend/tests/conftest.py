import os
from pathlib import Path

os.environ["WEBAPP_DATABASE_URL"] = "sqlite+pysqlite:////tmp/presd-webapp-tests.sqlite"
os.environ["WEBAPP_DATA_ROOT"] = "/tmp/presd-webapp-data"
os.environ["WEBAPP_ALLOWED_ORIGINS"] = "http://testserver"
os.environ["WEBAPP_ALLOWED_CLIENTS_JSON"] = '{"alice":"correct horse","bob":"another correct horse"}'

import pytest
from fastapi.testclient import TestClient

from webapp.backend.app.database import Base, engine
from webapp.backend.app.main import app


class ImmediateQueue:
    def __init__(self):
        self.calls = []

    def enqueue(self, function, *args, **kwargs):
        self.calls.append((function, args, kwargs))


@pytest.fixture(autouse=True)
def clean_database(monkeypatch):
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    queue = ImmediateQueue()
    monkeypatch.setattr("webapp.backend.app.api.queue", lambda: queue)
    yield queue
    Base.metadata.drop_all(engine)


@pytest.fixture
def client():
    with TestClient(app) as value:
        yield value


@pytest.fixture
def registered(client):
    response = client.post("/api/v1/auth/login", json={"username": "alice", "password": "correct horse"}, headers={"Origin": "http://testserver"})
    assert response.status_code == 200
    return client
