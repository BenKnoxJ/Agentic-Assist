"""Tests for the Graph mail client.

All HTTP goes through httpx.MockTransport and the token path is an injected
fake - no test here touches the network or MSAL. The MSAL result handling
is tested separately against the documented dict shapes.
"""

import json

import httpx
import pytest

from gojo_graph.client import GraphError, GraphMailClient

OWNER = "owner@example.com"


def make_client(handler, **kwargs) -> GraphMailClient:
    async def fake_token() -> str:
        return "test-token"

    return GraphMailClient(
        tenant_id="tenant-id",
        client_id="client-id",
        client_secret="client-secret",
        owner_upn=OWNER,
        token_getter=kwargs.pop("token_getter", fake_token),
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def graph_page(messages: list[dict]) -> httpx.Response:
    return httpx.Response(200, json={"value": messages})


SAMPLE_MESSAGE = {
    "id": "AAMkAGI2TAAA=",
    "subject": "Quarterly numbers",
    "from": {"emailAddress": {"name": "Dave Example", "address": "dave@example.com"}},
    "receivedDateTime": "2026-08-07T09:30:00Z",
    "bodyPreview": "Here are the figures you asked for...",
    "isRead": False,
    "importance": "high",
    "hasAttachments": True,
}


async def test_returns_reduced_messages() -> None:
    client = make_client(lambda request: graph_page([SAMPLE_MESSAGE]))

    messages = await client.list_recent_messages()

    assert messages == [
        {
            "id": "AAMkAGI2TAAA=",
            "subject": "Quarterly numbers",
            "from": "Dave Example <dave@example.com>",
            "receivedDateTime": "2026-08-07T09:30:00Z",
            "bodyPreview": "Here are the figures you asked for...",
            "isRead": False,
            "importance": "high",
            "hasAttachments": True,
        }
    ]


async def test_get_message_fetches_one_by_id() -> None:
    """The deterministic reply-target check (step 5): fetch by id, reduced
    fields only - the gate shows the human what this returns, never what an
    agent claims about it."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = request.url
        return httpx.Response(200, json=SAMPLE_MESSAGE)

    client = make_client(handler)
    message = await client.get_message("AAMkAGI2TAAA=")

    assert seen["url"].path == f"/v1.0/users/{OWNER}/messages/AAMkAGI2TAAA="
    assert dict(seen["url"].params)["$select"].startswith("id,")
    assert message["from"] == "Dave Example <dave@example.com>"
    assert message["subject"] == "Quarterly numbers"


async def test_requests_owner_mailbox_with_capped_select() -> None:
    """App-only auth: /users/{upn}/messages, never /me (8.3), and the $select
    list is the whole injection/state surface - nothing beyond it comes back."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = request.url
        seen["auth"] = request.headers.get("Authorization")
        return graph_page([])

    client = make_client(handler)
    await client.list_recent_messages()

    assert seen["url"].path == f"/v1.0/users/{OWNER}/messages"
    params = dict(seen["url"].params)
    assert params["$select"] == (
        "id,subject,from,receivedDateTime,bodyPreview,isRead,importance,hasAttachments"
    )
    assert params["$orderby"] == "receivedDateTime desc"
    assert params["$top"] == "10"
    assert seen["auth"] == "Bearer test-token"


async def test_count_is_clamped_to_25() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return graph_page([])

    client = make_client(handler)
    await client.list_recent_messages(count=500)

    assert seen["params"]["$top"] == "25"


async def test_unread_filter_repeats_orderby_property() -> None:
    """Outlook resources 400 unless every $orderby property also leads the
    $filter - the constraint is odd enough that this test pins the URL shape."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return graph_page([])

    client = make_client(handler)
    await client.list_recent_messages(unread_only=True)

    assert seen["params"]["$filter"] == (
        "receivedDateTime ge 1970-01-01T00:00:00Z and isRead eq false"
    )
    assert seen["params"]["$orderby"] == "receivedDateTime desc"


async def test_search_uses_kql_without_orderby() -> None:
    """$search ranks by relevance and Graph rejects $orderby alongside it -
    the URL shape is the contract here."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return graph_page([])

    client = make_client(handler)
    await client.search_messages("from:amy Rowntree")

    params = seen["params"]
    assert params["$search"] == '"from:amy Rowntree"'
    assert "$orderby" not in params
    assert params["$select"] == (
        "id,subject,from,receivedDateTime,bodyPreview,isRead,importance,hasAttachments"
    )
    assert params["$top"] == "10"


async def test_search_count_clamped_and_quotes_stripped() -> None:
    """An embedded double-quote must not break out of the KQL string."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return graph_page([])

    client = make_client(handler)
    await client.search_messages('subject:"weekly" AND x', count=99)

    assert seen["params"]["$top"] == "25"
    assert '"' not in seen["params"]["$search"][1:-1]


async def test_search_returns_reduced_messages() -> None:
    client = make_client(lambda request: graph_page([SAMPLE_MESSAGE]))

    messages = await client.search_messages("Rowntree")

    assert messages[0]["subject"] == "Quarterly numbers"
    assert messages[0]["from"] == "Dave Example <dave@example.com>"


class TestWriteMethods:
    """Step 5: drafts and send. These never run without RBAC grants for
    Application Mail.ReadWrite / Mail.Send - the 403 text names them."""

    async def test_create_draft_posts_message_and_returns_id(self) -> None:
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["method"] = request.method
            seen["url"] = request.url
            seen["json"] = json.loads(request.content)
            seen["auth"] = request.headers.get("Authorization")
            return httpx.Response(201, json={"id": "draft-123"})

        client = make_client(handler)
        draft_id = await client.create_draft(
            to=["amy@example.org"], subject="Setup session", body="Hi Amy,\n..."
        )

        assert draft_id == "draft-123"
        assert seen["method"] == "POST"
        assert seen["url"].path == f"/v1.0/users/{OWNER}/messages"
        assert seen["auth"] == "Bearer test-token"
        assert seen["json"]["subject"] == "Setup session"
        assert seen["json"]["body"] == {"contentType": "text", "content": "Hi Amy,\n..."}
        assert seen["json"]["toRecipients"] == [
            {"emailAddress": {"address": "amy@example.org"}}
        ]

    async def test_create_reply_draft_uses_createreply_then_patches_body(self) -> None:
        """createReply mints the threaded draft; the PATCH sets our body
        without clobbering the quoted thread Graph put below it - the exact
        server-side semantics are a live-verify item, this pins our shape."""
        calls: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(
                {
                    "method": request.method,
                    "path": request.url.path,
                    "json": json.loads(request.content) if request.content else None,
                }
            )
            if request.method == "POST":
                return httpx.Response(201, json={"id": "reply-draft-9"})
            return httpx.Response(200, json={"id": "reply-draft-9"})

        client = make_client(handler)
        draft_id = await client.create_reply_draft("AAMkAGI2TAAA=", body="Thursday works.")

        assert draft_id == "reply-draft-9"
        assert calls[0]["method"] == "POST"
        assert calls[0]["path"] == f"/v1.0/users/{OWNER}/messages/AAMkAGI2TAAA=/createReply"
        assert calls[1]["method"] == "PATCH"
        assert calls[1]["path"] == f"/v1.0/users/{OWNER}/messages/reply-draft-9"
        assert calls[1]["json"]["body"]["content"] == "Thursday works."

    async def test_send_draft_posts_send_and_tolerates_empty_202(self) -> None:
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["method"] = request.method
            seen["path"] = request.url.path
            return httpx.Response(202)

        client = make_client(handler)
        await client.send_draft("draft-123")  # must not raise on empty body

        assert seen["method"] == "POST"
        assert seen["path"] == f"/v1.0/users/{OWNER}/messages/draft-123/send"

    async def test_write_403_names_the_write_roles(self) -> None:
        client = make_client(lambda request: httpx.Response(403))
        with pytest.raises(GraphError, match="Mail.ReadWrite"):
            await client.create_draft(to=["x@example.org"], subject="s", body="b")


async def test_403_raises_graph_error_pointing_at_the_runbook() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"code": "ErrorAccessDenied"}})

    client = make_client(handler)
    with pytest.raises(GraphError, match="graph-mail-rbac"):
        await client.list_recent_messages()


async def test_429_raises_graph_error_with_retry_after() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "17"})

    client = make_client(handler)
    with pytest.raises(GraphError, match="17"):
        await client.list_recent_messages()


async def test_5xx_raises_graph_error_not_a_crash() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream sad")

    client = make_client(handler)
    with pytest.raises(GraphError, match="503"):
        await client.list_recent_messages()


async def test_missing_fields_become_defaults_not_key_errors() -> None:
    client = make_client(lambda request: graph_page([{"subject": "bare"}]))

    messages = await client.list_recent_messages()

    assert messages[0]["subject"] == "bare"
    assert messages[0]["from"] == ""
    assert messages[0]["bodyPreview"] == ""


def test_msal_error_dict_becomes_graph_error() -> None:
    """MSAL reports failure as a result dict, it does not raise - the token
    path must convert that, or a bad secret surfaces as a KeyError."""
    from gojo_graph.client import token_from_msal_result

    result = {
        "error": "invalid_client",
        "error_description": "AADSTS7000215: Invalid client secret provided.",
    }
    with pytest.raises(GraphError, match="invalid_client"):
        token_from_msal_result(result)


def test_msal_success_dict_yields_token() -> None:
    from gojo_graph.client import token_from_msal_result

    assert token_from_msal_result({"access_token": "tok"}) == "tok"


def test_error_messages_never_contain_the_secret() -> None:
    """A misconfigured client must not leak its credential into logs."""
    from gojo_graph.client import token_from_msal_result

    result = {"error": "invalid_client", "error_description": "bad"}
    try:
        token_from_msal_result(result)
    except GraphError as exc:
        assert "client-secret" not in json.dumps(exc.args)
