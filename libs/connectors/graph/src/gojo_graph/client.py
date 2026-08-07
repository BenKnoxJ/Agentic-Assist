"""Microsoft Graph mail client - read-only, app-only auth.

Auth is OAuth2 client credentials (GOJO-MASTER.md 8.3): the app authenticates
as itself, so there is no /me - the mailbox is named explicitly. Which
mailboxes the token can actually reach is decided server-side by Exchange
RBAC (infra/graph-mail-rbac.ps1), never in this code.

MSAL is synchronous; token acquisition runs on a thread so the single event
loop (3.1: one physical core, one worker) never blocks. The token cache is
MSAL's in-process default - this is one always-on process and a client-
credentials token re-mints in a single call, so an on-disk cache would only
add a secret at rest (ADR 0010).
"""

import asyncio
from collections.abc import Awaitable, Callable

import httpx
import msal

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
SCOPES = ["https://graph.microsoft.com/.default"]

# The $select list is the whole surface: nothing outside it enters the
# agent's context. bodyPreview (~255 chars server-side) rather than body -
# full bodies are a backlog tool, not a default (6.3 rule 3). `id` is opaque
# and exists so a reply can name its target message (step 5); the gate then
# verifies the target by fetching it with get_message, never by trusting
# what an agent says about it.
SELECT_FIELDS = "id,subject,from,receivedDateTime,bodyPreview,isRead,importance,hasAttachments"

MAX_MESSAGES = 25

# Outlook resources reject $orderby unless each ordered property also leads
# the $filter, in that order. The epoch lower bound is vacuous on purpose -
# it exists to satisfy that constraint, not to filter anything.
_EPOCH = "1970-01-01T00:00:00Z"


class GraphError(Exception):
    """A Graph request failed in a way the caller should hear about."""


def token_from_msal_result(result: dict) -> str:
    """Extract the access token, or raise.

    MSAL reports failure as a result dict, it does not raise - without this
    conversion a bad secret surfaces as a KeyError three frames later.
    """
    if "access_token" in result:
        return result["access_token"]
    raise GraphError(
        "Graph token acquisition failed: "
        f"{result.get('error', 'unknown')} - "
        f"{result.get('error_description', 'no description')}"
    )


class GraphMailClient:
    """Fetches mail. Never reasons about it (8.1)."""

    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        owner_upn: str,
        token_getter: Callable[[], Awaitable[str]] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._tenant_id = tenant_id
        self._client_id = client_id
        self._client_secret = client_secret
        self._owner_upn = owner_upn
        self._token_getter = token_getter or self._msal_token
        self._transport = transport
        self._msal_app: msal.ConfidentialClientApplication | None = None

    async def list_recent_messages(
        self, count: int = 10, unread_only: bool = False
    ) -> list[dict]:
        """Most recent messages in the owner's mailbox, newest first."""
        token = await self._token_getter()
        params = {
            "$select": SELECT_FIELDS,
            "$orderby": "receivedDateTime desc",
            "$top": str(min(max(count, 1), MAX_MESSAGES)),
        }
        if unread_only:
            params["$filter"] = f"receivedDateTime ge {_EPOCH} and isRead eq false"

        async with httpx.AsyncClient(transport=self._transport, timeout=15.0) as http:
            response = await http.get(
                f"{GRAPH_BASE}/users/{self._owner_upn}/messages",
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
        _raise_for_status(response)
        return [_reduce(message) for message in response.json().get("value", [])]

    async def get_message(self, message_id: str) -> dict:
        """One message by id, reduced fields. The step-5 gate's deterministic
        reply-target check: the human is shown what this returns."""
        token = await self._token_getter()
        async with httpx.AsyncClient(transport=self._transport, timeout=15.0) as http:
            response = await http.get(
                f"{GRAPH_BASE}/users/{self._owner_upn}/messages/{message_id}",
                params={"$select": SELECT_FIELDS},
                headers={"Authorization": f"Bearer {token}"},
            )
        _raise_for_status(response)
        return _reduce(response.json())

    async def search_messages(self, query: str, count: int = 10) -> list[dict]:
        """Search the owner's mailbox with KQL (from:, subject:, body:, plain
        terms). Results are relevance-ranked: Graph rejects $orderby alongside
        $search, so recency must be part of the query, not the sort."""
        token = await self._token_getter()
        params = {
            # Embedded double-quotes would terminate the KQL string early.
            "$search": '"' + query.replace('"', "") + '"',
            "$select": SELECT_FIELDS,
            "$top": str(min(max(count, 1), MAX_MESSAGES)),
        }
        async with httpx.AsyncClient(transport=self._transport, timeout=15.0) as http:
            response = await http.get(
                f"{GRAPH_BASE}/users/{self._owner_upn}/messages",
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
        _raise_for_status(response)
        return [_reduce(message) for message in response.json().get("value", [])]

    # Write surface (step 5). These are only ever called by the deterministic
    # execute path AFTER human approval - no agent holds them as tools. The
    # payloads are the approved bytes, replayed verbatim (ADR 0011).

    async def create_draft(self, to: list[str], subject: str, body: str) -> str:
        """Create a draft in the owner's Drafts folder. Returns its id."""
        payload = {
            "subject": subject,
            "body": {"contentType": "text", "content": body},
            "toRecipients": [{"emailAddress": {"address": address}} for address in to],
        }
        response = await self._post(f"/users/{self._owner_upn}/messages", payload)
        _raise_for_status(response, write=True)
        return response.json()["id"]

    async def create_reply_draft(self, message_id: str, body: str) -> str:
        """Create a threaded reply draft to an existing message. Returns its id.

        Two calls: createReply mints the draft with the quoted thread below,
        then a PATCH sets our body. Whether createReply's own `comment` field
        could do this in one call is a live-verify item - the PATCH shape is
        the one pinned by tests.
        """
        response = await self._post(
            f"/users/{self._owner_upn}/messages/{message_id}/createReply", {}
        )
        _raise_for_status(response, write=True)
        draft_id = response.json()["id"]

        token = await self._token_getter()
        async with httpx.AsyncClient(transport=self._transport, timeout=15.0) as http:
            patched = await http.patch(
                f"{GRAPH_BASE}/users/{self._owner_upn}/messages/{draft_id}",
                json={"body": {"contentType": "text", "content": body}},
                headers={"Authorization": f"Bearer {token}"},
            )
        _raise_for_status(patched, write=True)
        return draft_id

    async def send_draft(self, message_id: str) -> None:
        """Send an existing draft. 202 with an empty body on success.

        Sending by id is what makes the step-5 replay story hold: a crash
        replay re-sends the SAME draft, which Graph refuses once sent -
        never a second copy (ADR 0011).
        """
        response = await self._post(
            f"/users/{self._owner_upn}/messages/{message_id}/send", None
        )
        _raise_for_status(response, write=True)

    async def _post(self, path: str, payload: dict | None) -> httpx.Response:
        token = await self._token_getter()
        async with httpx.AsyncClient(transport=self._transport, timeout=15.0) as http:
            return await http.post(
                f"{GRAPH_BASE}{path}",
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
            )

    async def _msal_token(self) -> str:
        def acquire() -> dict:
            if self._msal_app is None:
                self._msal_app = msal.ConfidentialClientApplication(
                    self._client_id,
                    client_credential=self._client_secret,
                    authority=f"https://login.microsoftonline.com/{self._tenant_id}",
                )
            return self._msal_app.acquire_token_for_client(scopes=SCOPES)

        return token_from_msal_result(await asyncio.to_thread(acquire))


def _raise_for_status(response: httpx.Response, write: bool = False) -> None:
    if response.is_success:
        return
    if response.status_code == 403:
        role = (
            "Application Mail.ReadWrite / Mail.Send (write path)"
            if write
            else "Application Mail.Read"
        )
        raise GraphError(
            f"Graph returned 403 for the owner's mailbox. Either the {role} "
            "RBAC grant is missing or its cache (30min-2h) has not caught up "
            "- see infra/graph-mail-rbac.ps1."
        )
    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After", "unknown")
        raise GraphError(
            f"Graph is throttling (429, Retry-After: {retry_after}s). Not retrying."
        )
    raise GraphError(f"Graph request failed with {response.status_code}.")


def _reduce(message: dict) -> dict:
    """Flatten a Graph message to exactly the fields the agent may see."""
    sender = message.get("from", {}).get("emailAddress", {})
    sender_text = ""
    if sender:
        sender_text = f"{sender.get('name', '')} <{sender.get('address', '')}>".strip()
    return {
        "id": message.get("id", ""),
        "subject": message.get("subject", ""),
        "from": sender_text,
        "receivedDateTime": message.get("receivedDateTime", ""),
        "bodyPreview": message.get("bodyPreview", ""),
        "isRead": message.get("isRead", True),
        "importance": message.get("importance", "normal"),
        "hasAttachments": message.get("hasAttachments", False),
    }
