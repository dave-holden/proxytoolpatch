from __future__ import annotations

import pytest
import respx
from fastapi.testclient import TestClient


@pytest.fixture()
def mock_upstream():
    with respx.mock(base_url="http://127.0.0.1:8181") as mock:
        yield mock


@pytest.fixture()
def client(mock_upstream, monkeypatch):
    monkeypatch.setenv("LMW_UPSTREAM_URL", "http://127.0.0.1:8181")
    from proxytool.main import app

    return TestClient(app)
