"""HTTP surface for the orchestrator.

FastAPI defines the routes; Uvicorn runs them (single worker - one physical
core, I/O-bound workload, see GOJO-MASTER.md 4.3). Caddy terminates TLS and
forwards plain HTTP to localhost:3000.

This is the door onto the graph and nothing more. No reasoning, no routing
decisions - those belong to the orchestrator. Step 2 replaces /chat's caller
with the Teams surface; the graph underneath does not change.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from gojo.config import assert_subscription_auth
from gojo.orchestrator import build_graph
from gojo.state import Intent


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
    yield


app = FastAPI(title="Gojo", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe. Used by systemd and by you, from a phone, at 07:00."""
    return {"status": "ok"}


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
