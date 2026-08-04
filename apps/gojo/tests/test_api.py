"""Tests for the HTTP surface.

The Agent SDK is never called here. gojo.agents.megumi.gather is the single
injectable seam (GOJO-MASTER.md 6.3 rule 2) - swap it and the graph runs
end to end in milliseconds with no subprocess and no inference spend.
"""

import pytest
from fastapi.testclient import TestClient

from gojo import api, orchestrator


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """A client whose gather agent is a stub."""

    async def fake_gather(message: str) -> str:
        return f"stub findings for: {message}"

    monkeypatch.setattr(orchestrator, "gather", fake_gather)
    # TestClient as a context manager is what triggers lifespan, and lifespan
    # is where the graph is compiled. Without it app.state.graph is unset.
    with TestClient(api.app) as c:
        yield c


def test_health(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok"}


def test_read_path_routes_to_megumi(client: TestClient) -> None:
    r = client.post("/chat", json={"message": "what needs my attention today"})
    assert r.status_code == 200
    body = r.json()
    assert body["intent"] == "gather"
    assert body["steps"] == ["classify", "megumi", "respond"]
    assert "stub findings" in body["reply"]


def test_write_path_routes_to_sukuna(client: TestClient) -> None:
    r = client.post("/chat", json={"message": "send a reply to Dave"})
    assert r.status_code == 200
    body = r.json()
    assert body["intent"] == "act"
    assert body["steps"] == ["classify", "sukuna", "respond"]


def test_empty_message_rejected(client: TestClient) -> None:
    assert client.post("/chat", json={"message": ""}).status_code == 422


def test_upstream_failure_is_contained(client: TestClient) -> None:
    """A dead agent returns 502, it does not take the process down."""

    async def exploding_gather(message: str) -> str:
        raise RuntimeError("upstream is down")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(orchestrator, "gather", exploding_gather)
        assert client.post("/chat", json={"message": "anything"}).status_code == 502

    # Process still serving.
    assert client.get("/health").status_code == 200
