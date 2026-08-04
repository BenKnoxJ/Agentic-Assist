"""HTTP surface for the orchestrator.

FastAPI defines the routes; Uvicorn runs them (single worker - one physical
core, I/O-bound workload, see GOJO-MASTER.md 4.3). Caddy terminates TLS and
forwards plain HTTP to localhost:3000.

This is the door onto the graph and nothing more. No reasoning, no routing
decisions - those belong to the orchestrator. Step 2 replaces /chat's caller
with the Teams surface; the graph underneath does not change.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, Field

from gojo.config import assert_subscription_auth, get_settings
from gojo.orchestrator import build_graph
from gojo.state import Intent
from gojo.teams import build_agent_app, in_flight_count

logger = logging.getLogger(__name__)


class ChatRequest(BaseModel):
    """One inbound message."""

    message: str = Field(min_length=1, max_length=4000)


class ChatResponse(BaseModel):
    """The orchestrator's answer, plus the path it took to get there."""

    reply: str
    intent: Intent
    steps: list[str]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Boot-time setup.

    Auth is asserted here, not on first request: a process that would bill at
    API rates must fail at boot where systemd and the logs will show it
    (GOJO-MASTER.md 6.2 rule 3). The graph is compiled once for the same
    reason a worker is - there is one core to spend.
    """
    assert_subscription_auth()
    app.state.graph = build_graph()

    settings = get_settings()
    app.state.adapter = None
    app.state.agent_app = None

    if settings.teams_configured:
        # Imported here, not at module scope: the Teams stack pulls in aiohttp
        # and MSAL, and an unconfigured deployment should not pay for them.
        from microsoft_agents.authentication.msal import MsalConnectionManager
        from microsoft_agents.hosting.core import AgentAuthConfiguration
        from microsoft_agents.hosting.fastapi import CloudAdapter

        auth = AgentAuthConfiguration(
            client_id=settings.teams_client_id,
            tenant_id=settings.teams_tenant_id,
            client_secret=settings.teams_client_secret.get_secret_value(),
        )
        connections = MsalConnectionManager(connections_configurations={"SERVICE": auth})
        adapter = CloudAdapter(connection_manager=connections)

        app.state.adapter = adapter
        app.state.agent_app = build_agent_app(
            adapter, app.state.graph, settings.teams_client_id
        )
        logger.info("Teams surface enabled for tenant %s", settings.teams_tenant_id)
    else:
        logger.warning("Teams surface disabled - client id, tenant id or secret unset")

    yield


app = FastAPI(title="Gojo", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, object]:
    """Liveness probe. Used by systemd and by you, from a phone, at 07:00.

    Reports the Teams surface separately: a process that is up but not
    listening to Teams looks identical from the outside otherwise.
    """
    return {
        "status": "ok",
        "teams": "enabled" if app.state.agent_app else "disabled",
        "turns_in_flight": in_flight_count(),
    }


@app.post("/api/messages")
async def messages(request: Request) -> Response:
    """Azure Bot Service posts Activities here.

    The SDK validates the JWT - issuer and audience included - before this
    handler sees anything. Do not add a hand-rolled check in front of it (5.2).
    """
    if app.state.agent_app is None:
        raise HTTPException(status_code=503, detail="Teams surface is not configured")

    from microsoft_agents.hosting.fastapi import start_agent_process

    return await start_agent_process(request, app.state.agent_app, app.state.adapter)


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    """Run one message through the orchestrator and return the reply."""
    initial = {"message": request.message, "steps": [], "findings": []}

    try:
        result = await app.state.graph.ainvoke(initial)
    except Exception as exc:  # noqa: BLE001 - contained, see 10.4
        # A dead upstream degrades the answer; it does not kill the process.
        raise HTTPException(status_code=502, detail=f"orchestrator failed: {exc}") from exc

    return ChatResponse(
        reply=result["reply"],
        intent=result["intent"],
        steps=result["steps"],
    )
