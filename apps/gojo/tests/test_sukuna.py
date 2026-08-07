"""Tests for sukuna's wiring - the compose agent that may only propose.

The seam is run_agent (6.3 rule 2). The load-bearing assertions: sukuna
holds exactly the READ tools (the seal is structural - no write tool exists
for any agent to hold), and classify's new compose phrases don't misroute
the product's core read questions.
"""

import pytest

from gojo.agents import sukuna
from gojo.agents.runner import AgentResult
from gojo.agents.tools import GATHER_TOOL_NAMES
from gojo.orchestrator import classify


@pytest.fixture
def captured(monkeypatch) -> dict:
    seen: dict = {}

    async def fake_run_agent(
        prompt,
        system_prompt="",
        allowed_tools=None,
        max_turns=None,
        resume=None,
        mcp_servers=None,
    ):
        seen.update(
            prompt=prompt,
            system_prompt=system_prompt,
            allowed_tools=allowed_tools,
            mcp_servers=mcp_servers,
            resume=resume,
        )
        return AgentResult(text='{"op": "draft"}', session_id="sukuna-session")

    monkeypatch.setattr(sukuna, "run_agent", fake_run_agent)
    return seen


async def test_compose_holds_exactly_the_read_tools(captured) -> None:
    """No write tool exists anywhere; sukuna gets the same read-only server
    megumi does, nothing more."""
    await sukuna.compose("draft a reply to Amy")

    assert captured["allowed_tools"] == GATHER_TOOL_NAMES
    assert "gather" in captured["mcp_servers"]


async def test_compose_resumes_the_shared_session(captured) -> None:
    """'Reply to the mail we just discussed' needs the conversation's
    context - compose resumes the shared session (ADR 0011 records the
    trade; the sukuna NODE must not write the returned id back to state)."""
    await sukuna.compose("draft a reply", resume="thread-session-1")

    assert captured["resume"] == "thread-session-1"


async def test_compose_prompt_demands_one_json_object(captured) -> None:
    await sukuna.compose("draft a reply")

    prompt = captured["system_prompt"]
    assert "JSON" in prompt
    assert "untrusted" in prompt.lower()
    assert "reply_to_message_id" in prompt


class TestClassifyPhrases:
    """M5: compose phrases must not misroute core read questions."""

    def _intent(self, message: str) -> str:
        return classify({"message": message})["intent"]

    def test_compose_asks_route_to_act(self) -> None:
        assert self._intent("draft an email to Chloe about the invoice") == "act"
        assert self._intent("compose an update for the team") == "act"
        assert self._intent("write an email to Dave") == "act"

    def test_reply_asks_still_route_to_act(self) -> None:
        assert self._intent("draft a reply to Amy") == "act"  # via existing "reply"

    def test_read_questions_stay_on_the_gather_path(self) -> None:
        assert self._intent("any email from Amy about Rowntree?") == "gather"
        assert self._intent("what needs my attention today") == "gather"

    def test_draft_as_a_noun_does_not_misroute(self) -> None:
        """'the draft agreement' must not become an action - bare 'draft'
        and bare 'email' are deliberately NOT action words."""
        assert self._intent("summarise the draft agreement from Waterstons") == "gather"
