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
            "subject": "Quarterly numbers",
            "from": "Dave Example <dave@example.com>",
            "receivedDateTime": "2026-08-07T09:30:00Z",
            "bodyPreview": "Here are the figures you asked for...",
            "isRead": False,
            "importance": "high",
            "hasAttachments": True,
        }
    ]


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
        "subject,from,receivedDateTime,bodyPreview,isRead,importance,hasAttachments"
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
