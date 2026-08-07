"""Tests for the approval protocol - pure functions, table-tested.

The card submit fixture mirrors the payload shape our own Action.Submit
data declares; the T1 spike recording confirms Teams echoes it (build-log
Session 6). Until then these pin our side of the contract.
"""

from gojo.approval import build_card, parse_decision, render_prompt

NEW_DRAFT_VALUE = {
    "action_id": "act-123",
    "payload": {
        "op": "draft",
        "kind": "new",
        "to": ["amy@example.org"],
        "subject": "Setup session",
        "body": "Hi Amy — Thursday works.",
        "reply_to_message_id": None,
    },
    "verified_target": None,
}

REPLY_SEND_VALUE = {
    "action_id": "act-456",
    "payload": {
        "op": "send",
        "kind": "reply",
        "to": [],
        "subject": "",
        "body": "Thursday works — invite to follow.",
        "reply_to_message_id": "AAMkAGI2TAAA=",
    },
    "verified_target": {
        "from": "Amy Whalen <amy@jrht.example>",
        "subject": "Onboarding go-ahead",
    },
}


class TestRenderPrompt:
    def test_new_draft_shows_payload_verbatim_with_recipient_first(self) -> None:
        prompt = render_prompt(NEW_DRAFT_VALUE)

        assert "amy@example.org" in prompt
        assert "Setup session" in prompt
        assert "Hi Amy — Thursday works." in prompt
        assert prompt.index("amy@example.org") < prompt.index("Hi Amy")
        assert "yes" in prompt.lower() and "no" in prompt.lower()

    def test_reply_leads_with_the_verified_target(self) -> None:
        """M3: the deterministically fetched sender/subject come first -
        the one thing the human must actually read."""
        prompt = render_prompt(REPLY_SEND_VALUE)

        assert prompt.index("Amy Whalen") < prompt.index("Thursday works")
        assert "Onboarding go-ahead" in prompt

    def test_send_is_flagged_loudly(self) -> None:
        prompt = render_prompt(REPLY_SEND_VALUE)
        assert "SEND" in prompt  # leaves the mailbox on approval - say so

    def test_draft_says_it_stays_in_drafts(self) -> None:
        prompt = render_prompt(NEW_DRAFT_VALUE)
        assert "Draft" in prompt


class TestBuildCard:
    def test_card_carries_approve_and_reject_submits(self) -> None:
        card = build_card(NEW_DRAFT_VALUE)

        assert card is not None
        actions = card["actions"]
        assert [a["data"]["gojo_action"] for a in actions] == ["approve", "reject"]
        assert all(a["data"]["action_id"] == "act-123" for a in actions)
        assert all(a["type"] == "Action.Submit" for a in actions)

    def test_card_body_wraps_and_leads_with_the_target(self) -> None:
        card = build_card(REPLY_SEND_VALUE)

        blocks = card["body"]
        assert all(b.get("wrap", True) for b in blocks if b["type"] == "TextBlock")
        first_texts = " ".join(b["text"] for b in blocks[:3] if b["type"] == "TextBlock")
        assert "Amy Whalen" in first_texts


class TestParseDecision:
    def test_card_approve_with_matching_id(self) -> None:
        value = {"gojo_action": "approve", "action_id": "act-123"}
        assert parse_decision(value, "", "act-123") == "approve"

    def test_card_reject_with_matching_id(self) -> None:
        value = {"gojo_action": "reject", "action_id": "act-123"}
        assert parse_decision(value, "", "act-123") == "reject"

    def test_stale_card_id_is_refused_not_silent(self) -> None:
        value = {"gojo_action": "approve", "action_id": "old-action"}
        assert parse_decision(value, "", "act-123") is None

    def test_text_yes_and_synonyms_approve(self) -> None:
        for text in ("yes", "Yes", " YES ", "approve", "y"):
            assert parse_decision(None, text, "act-123") == "approve"

    def test_text_no_and_synonyms_reject(self) -> None:
        for text in ("no", "No", "reject", "n"):
            assert parse_decision(None, text, "act-123") == "reject"

    def test_anything_else_cancels_loudly(self) -> None:
        assert parse_decision(None, "actually what time is it", "act-123") == "cancel"

    def test_yes_inside_a_sentence_is_not_an_approval(self) -> None:
        """'yes but change the subject' is a new instruction, not consent."""
        assert parse_decision(None, "yes but change the subject", "act-123") == "cancel"
