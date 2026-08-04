"""Tests for the HTTP surface.

The Agent SDK is never called here. gojo.agents.megumi.gather is the single
injectable seam (GOJO-MASTER.md 6.3 rule 2) - swap it and the graph runs
end to end in milliseconds with no subprocess and no inference spend.
"""

import pytest
from fastapi.testclient import TestClient

from gojo import api, orchestrator
from gojo.agents.runner import AgentResult
from gojo.config import Settings


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """A client whose gather agent is a stub and whose Teams surface is off.

    _env_file=None matters: without it Settings reads the developer's real
    .env, so these tests would pass or fail depending on whether Teams
    happened to be configured on the machine running them.
    """

    async def fake_gather(
        message: str, resume: str | None = None, summary: str = ""
    ) -> AgentResult:
        # Mirrors the real signature: the node passes resume= and reads
        # .session_id back, so a str stub would hide a wiring break.
        return AgentResult(
            text=f"stub findings for: {message}", session_id="stub-session"
        )

    monkeypatch.setattr(orchestrator, "gather", fake_gather)
    monkeypatch.setattr(api, "get_settings", lambda: Settings(_env_file=None))

    # TestClient as a context manager is what triggers lifespan, and lifespan
    # is where the graph is compiled. Without it app.state.graph is unset.
    with TestClient(api.app) as c:
        yield c


def test_health(client: TestClient) -> None:
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["turns_in_flight"] == 0


def test_health_reports_teams_disabled_without_credentials(client: TestClient) -> None:
    """No client id, tenant id or secret means no Teams surface, and it says so."""
    assert client.get("/health").json()["teams"] == "disabled"


def test_messages_rejects_unauthenticated_requests(client: TestClient) -> None:
    """Fails closed.

    The JWT decorator runs before the handler, so an unconfigured deployment
    answers 500 ("authentication configuration not found") rather than the
    handler's 503. Either way an unsigned Activity is never processed - which
    is the property that matters. A 2xx here would mean anyone on the internet
    can drive the orchestrator.
    """
    r = client.post("/api/messages", json={"type": "message", "text": "hi"})
    assert r.status_code >= 400
    assert r.status_code != 503 or r.json()  # documented either way, never 2xx


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
