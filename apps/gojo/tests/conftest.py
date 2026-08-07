"""Shared test fixtures."""

import pytest

from gojo import orchestrator


@pytest.fixture(autouse=True)
def fresh_thread_locks():
    """Empty the per-conversation lock registry before every test.

    The registry is module-level and asyncio.Lock binds to the event loop
    that first acquires it. In production there is one loop for the life of
    the process (ADR 0009 records the assumption); under pytest every test
    gets a fresh loop, so a lock leaking across tests raises "bound to a
    different event loop" in whichever test reuses the thread id.
    """
    orchestrator._thread_locks.clear()
    yield
    orchestrator._thread_locks.clear()


@pytest.fixture(autouse=True)
def fresh_tool_clients():
    """Drop cached connector clients before and after every test.

    Same class of hazard as the locks above, plus one more: pytest runs on
    the box with the real .env present (VPS.md deploy sequence), so a client
    cached from one test's settings must never leak into another - and no
    test may inherit a client built from real credentials.
    """
    from gojo import actions
    from gojo.agents import tools

    tools.reset_clients()
    actions.reset_clients()
    actions.use_connection(None)
    yield
    tools.reset_clients()
    actions.reset_clients()
    actions.use_connection(None)
