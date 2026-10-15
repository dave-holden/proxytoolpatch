from __future__ import annotations

import pytest
import respx
from fastapi.testclient import TestClient


@pytest.fixture()
def mock_upstream():
    with respx.mock(base_url="http://127.0.0.1:8181") as mock:
        yield mock


@pytest.fixture(autouse=True)
def _reset_loop_guard():
    """Isolate the module-global LoopGuard: repeated minimal prompts in one
    test file must not nudge/abort requests in the next file."""
    from proxytool.main import loop_guard

    loop_guard.reset()
    yield


@pytest.fixture()
def client(mock_upstream, monkeypatch):
    monkeypatch.setenv("LMW_UPSTREAM_URL", "http://127.0.0.1:8181")
    from proxytool.main import app

    return TestClient(app)
