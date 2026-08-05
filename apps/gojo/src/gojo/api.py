"""HTTP surface for the orchestrator.

FastAPI defines the routes; Uvicorn runs them (single worker - one physical
core, I/O-bound workload, see GOJO-MASTER.md 4.3). Caddy terminates TLS and
forwards plain HTTP to localhost:3000.

This is the door onto the graph and nothing more. No reasoning, no routing
decisions - those belong to the orchestrator. Step 2 replaces /chat's caller
with the Teams surface; the graph underneath does not change.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from pathlib import Path

import aiosqlite
from fastapi import FastAPI, HTTPException, Request, Response
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from microsoft_agents.hosting.fastapi import (
    jwt_authorization_decorator,
    start_agent_process,
)
from pydantic import BaseModel, Field

from gojo import outbox
from gojo.commands import handle as handle_command
from gojo.commands import is_command
from gojo.config import assert_subscription_auth, get_settings
from gojo.logs import new_turn_id
from gojo.orchestrator import GraphTimeout, build_graph, run_locked
from gojo.recovery import recover_owed_replies
from gojo.state import Intent
from gojo.teams import build_agent_app, in_flight_count

logger = logging.getLogger(__name__)


class ChatRequest(BaseModel):
    """One inbound message."""

    message: str = Field(min_length=1, max_length=4000)
    # Same thread id means the same conversation. Handy for exercising
    # continuity from curl without going through Teams.
    thread_id: str = "chat"


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
    settings = get_settings()

    # from_conn_string is an async context manager and the connection must stay
    # open for the life of the process. Entering it on the exit stack keeps it
    # open until shutdown - closing it here would leave the graph holding a
    # dead connection, which fails on the first message rather than at boot.
    async with AsyncExitStack() as stack:
        checkpoint_file = Path(settings.checkpoint_path)
        checkpoint_file.parent.mkdir(parents=True, exist_ok=True)
        checkpointer = await stack.enter_async_context(
            AsyncSqliteSaver.from_conn_string(str(checkpoint_file))
        )
        app.state.graph = build_graph(checkpointer=checkpointer)
        logger.info("checkpointer at %s", checkpoint_file)

        # A second connection to the same file. LangGraph owns its schema and
        # we own ours; at one user's write volume there is no contention
        # (ADR 0008).
        outbox_conn = await stack.enter_async_context(
            aiosqlite.connect(str(checkpoint_file))
        )
        await outbox.create_table(outbox_conn)
        app.state.outbox = outbox_conn
        app.state.recovery_task = None

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
            # The key must be exactly "SERVICE_CONNECTION" - the manager looks it up
            # by that literal name and raises if it is absent (connection_manager.py:98).
            connections = MsalConnectionManager(
                connections_configurations={"SERVICE_CONNECTION": auth}
            )
            adapter = CloudAdapter(connection_manager=connections)

            app.state.adapter = adapter
            # The JWT decorator reads the config from this exact attribute name.
            # Without it every request is rejected 500 rather than validated -
            # it fails closed, but it fails.
            app.state.agent_configuration = auth
            app.state.agent_app = build_agent_app(
                adapter,
                app.state.graph,
                settings.teams_client_id,
                connections,
                settings.allowed_users,
                settings.teams_tenant_id,
                settings.fast_reply_seconds,
                outbox_conn,
            )
            logger.info(
                "Teams surface enabled for tenant %s, %d authorised user(s)",
                settings.teams_tenant_id,
                len(settings.allowed_users),
            )
            if not settings.allowed_users:
                logger.warning(
                    "ALLOWED_USER_IDS is unset - every message will be refused. "
                    "Send one message and read the refusal log line for your object ID."
                )
        else:
            logger.warning("Teams surface disabled - client id, tenant id or secret unset")

        if app.state.agent_app is not None:
            # Started rather than awaited: recovery must not hold the boot open
            # for graph_timeout_seconds per owed row while systemd is watching
            # (ADR 0008). Safe to run alongside live traffic because each row
            # is processed under its conversation's lock (ADR 0009). Held on
            # app.state because asyncio keeps only weak references to tasks -
            # the trap teams.py documents.
            app.state.recovery_task = asyncio.create_task(
                recover_owed_replies(
                    outbox_conn,
                    app.state.graph,
                    app.state.adapter,
                    settings.teams_client_id,
                )
            )

        try:
            yield
        finally:
            # The exit stack is about to close both SQLite connections. A
            # recovery pass still mid-resume would then run against closed
            # handles, so it is cancelled first rather than left to find out.
            task = app.state.recovery_task
            if task is not None and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            # app is a module singleton, so a stale handle would outlive the
            # connection it points at.
            app.state.outbox = None


app = FastAPI(title="Gojo", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, object]:
    """Liveness probe. Used by systemd and by you, from a phone, at 07:00.

    Reports the Teams surface separately: a process that is up but not
    listening to Teams looks identical from the outside otherwise.

    owed_replies is the ADR 0008 backlog - answers promised and not yet
    delivered. Steady state is 0; a number that stays above 0 across restarts
    means delivery is failing, not that turns are slow.
    """
    conn = getattr(app.state, "outbox", None)
    owed = await outbox.list_owed(conn) if conn is not None else []
    return {
        "status": "ok",
        "teams": "enabled" if app.state.agent_app else "disabled",
        "turns_in_flight": in_flight_count(),
        "owed_replies": len(owed),
    }


@app.post("/api/messages")
@jwt_authorization_decorator
async def messages(request: Request) -> Response:
    """Azure Bot Service posts Activities here.

    ⚠ The decorator is what enforces authentication, and it is not optional.
    Without it this endpoint accepts any well-formed Activity from anyone on
    the internet - the adapter itself does not authenticate. It validates the
    bearer token against issuers built from TENANT_ID, which is what makes
    validation single-tenant (5.2). Anonymous access is off by default; do not
    turn it on.

    Never hand-roll this. Hand-written validators routinely skip iss and aud,
    which is the difference between validating a token and validating the
    right token.
    """
    if app.state.agent_app is None:
        raise HTTPException(status_code=503, detail="Teams surface is not configured")

    return await start_agent_process(request, app.state.agent_app, app.state.adapter)


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    """Run one message through the orchestrator and return the reply."""
    new_turn_id()
    # Same command handling as Teams, so behaviour can be exercised from curl
    # rather than only from a phone. No outbox connection on purpose: /chat
    # never acknowledges, so it never owes (ADR 0008).
    if is_command(request.message):
        reply = await handle_command(app.state.graph, request.message, request.thread_id)
        return ChatResponse(reply=reply, intent="unknown", steps=["command"])

    try:
        # Locked like every other graph entry point (ADR 0009), so a curl
        # session and a Teams turn on the same thread id queue, not fork.
        result = await run_locked(app.state.graph, request.message, request.thread_id)
    except GraphTimeout as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - contained, see 10.4
        # A dead upstream degrades the answer; it does not kill the process.
        raise HTTPException(status_code=502, detail=f"orchestrator failed: {exc}") from exc

    return ChatResponse(
        reply=result["reply"],
        intent=result["intent"],
        steps=result["steps"],
    )
