"""The approval protocol - how a pending action is shown and decided.

Pure functions, shared by teams.py and api.py so both surfaces speak the
same protocol. The principles (ADR 0011):

- **The human sees the exact payload.** What renders is the canonical
  proposal the ledger will execute, plus - for replies - the target's
  sender/subject fetched deterministically by id, never agent prose. The
  target leads, because it is the one thing the reader must actually check.
- **Consent is exact.** A bare yes/no (or the card buttons). "yes but
  change the subject" is a new instruction, not consent - anything that
  isn't an exact yes/no cancels the action loudly and is then handled as a
  normal message.
- **Stale approvals are refused, not silent.** A card outlives its action
  (Teams keeps buttons tappable); a mismatched action_id returns None and
  the caller says so.
"""

APPROVE_WORDS = frozenset({"yes", "y", "approve", "approved", "go ahead", "do it"})
REJECT_WORDS = frozenset({"no", "n", "reject", "rejected", "don't", "stop"})

# Adaptive Card schema for Teams. §13 item 5: 1.5 is the spike's starting
# point; if the T1 spike shows only an older schema renders, change this
# constant and nothing else. Set CARD_ENABLED False to fall back to the
# always-wired plain-text prompt.
CARD_SCHEMA_VERSION = "1.5"
CARD_ENABLED = True


def _lines(value: dict) -> list[tuple[str, str]]:
    """(label, text) pairs in display order - target first, body last."""
    payload = value["payload"]
    rows: list[tuple[str, str]] = []
    if value.get("verified_target"):
        target = value["verified_target"]
        rows.append(("Replying to", f"{target['from']} — “{target['subject']}”"))
    if payload["kind"] == "new":
        rows.append(("To", ", ".join(payload["to"])))
        rows.append(("Subject", payload["subject"]))
    rows.append(("Body", payload["body"]))
    return rows


def _headline(value: dict) -> str:
    if value["payload"]["op"] == "send":
        return "⚠ SEND — this mail leaves your mailbox the moment you approve."
    return "Draft — saved to your Drafts folder; nothing is sent."


def render_prompt(value: dict) -> str:
    """The plain-text approval prompt. Always wired, card or no card."""
    body = "\n".join(f"**{label}:** {text}" for label, text in _lines(value))
    return (
        f"{_headline(value)}\n\n{body}\n\n"
        "Reply **yes** to approve or **no** to reject. "
        "Anything else discards this action."
    )


def build_card(value: dict) -> dict | None:
    """The Adaptive Card version of the same prompt. None when disabled."""
    if not CARD_ENABLED:
        return None
    blocks: list[dict] = [
        {"type": "TextBlock", "text": _headline(value), "weight": "Bolder", "wrap": True}
    ]
    for label, text in _lines(value):
        blocks.append(
            {"type": "TextBlock", "text": f"{label}: {text}", "wrap": True}
        )
    data = {"action_id": value["action_id"]}
    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": CARD_SCHEMA_VERSION,
        "body": blocks,
        "actions": [
            {
                "type": "Action.Submit",
                "title": "Approve",
                "data": {"gojo_action": "approve", **data},
            },
            {
                "type": "Action.Submit",
                "title": "Reject",
                "data": {"gojo_action": "reject", **data},
            },
        ],
    }


def parse_decision(
    activity_value: dict | None, text: str, expected_action_id: str
) -> str | None:
    """What the owner decided. approve | reject | cancel, or None for a
    stale card (mismatched action_id - refuse with a notice, never silence).
    """
    if activity_value:
        if activity_value.get("action_id") != expected_action_id:
            return None
        if activity_value.get("gojo_action") == "approve":
            return "approve"
        if activity_value.get("gojo_action") == "reject":
            return "reject"
        return "cancel"
    word = text.strip().lower()
    if word in APPROVE_WORDS:
        return "approve"
    if word in REJECT_WORDS:
        return "reject"
    return "cancel"
