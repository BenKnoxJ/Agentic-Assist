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
def fresh_tool_clients(monkeypatch):
    """No test may ever build a connector client from the real .env.

    pytest runs on the box with the real credentials present (VPS.md deploy
    sequence). This was not hypothetical: an api-level gate test reached
    write_client(), built a real GraphMailClient and made a live Graph call
    mid-suite (7 Aug 2026). Both factory modules therefore see unconfigured
    settings under test, unconditionally - tests that need a client inject
    a fake through the factory seam. Caches and the ledger connection are
    also cleared both sides, for the same isolation reasons as the locks.
    """
    from gojo import actions
    from gojo.agents import tools
    from gojo.config import Settings

    def unconfigured() -> Settings:
        return Settings(_env_file=None)

    monkeypatch.setattr(tools, "get_settings", unconfigured)
    monkeypatch.setattr(actions, "get_settings", unconfigured)

    tools.reset_clients()
    actions.reset_clients()
    actions.use_connection(None)
    yield
    tools.reset_clients()
    actions.reset_clients()
    actions.use_connection(None)
