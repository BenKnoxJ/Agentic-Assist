"""Tests for the Jira client. All HTTP via httpx.MockTransport - no network."""

import base64

import httpx
import pytest

from gojo_jira.client import JiraClient, JiraError

BASE_URL = "https://example.atlassian.net"


def make_client(handler) -> JiraClient:
    return JiraClient(
        base_url=BASE_URL,
        email="owner@example.com",
        api_token="jira-token",
        transport=httpx.MockTransport(handler),
    )


def jira_page(issues: list[dict]) -> httpx.Response:
    return httpx.Response(200, json={"issues": issues})


SAMPLE_ISSUE = {
    "key": "CON-42",
    "fields": {
        "summary": "Renew the TLS certificate",
        "status": {"name": "In Progress"},
        "assignee": {"displayName": "Ben Knox"},
        "priority": {"name": "High"},
        "updated": "2026-08-06T16:20:00.000+0000",
        "issuetype": {"name": "Task"},
    },
}


async def test_returns_reduced_issues() -> None:
    client = make_client(lambda request: jira_page([SAMPLE_ISSUE]))

    issues = await client.search_issues("assignee = currentUser()")

    assert issues == [
        {
            "key": "CON-42",
            "summary": "Renew the TLS certificate",
            "status": "In Progress",
            "assignee": "Ben Knox",
            "priority": "High",
            "updated": "2026-08-06T16:20:00.000+0000",
            "issuetype": "Task",
        }
    ]


async def test_calls_current_search_endpoint_with_basic_auth() -> None:
    """/rest/api/3/search/jql - the old /search endpoint is gone."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = request.url
        seen["auth"] = request.headers.get("Authorization")
        return jira_page([])

    client = make_client(handler)
    await client.search_issues("order by updated desc")

    assert seen["url"].path == "/rest/api/3/search/jql"
    params = dict(seen["url"].params)
    assert params["jql"] == "order by updated desc"
    assert params["maxResults"] == "10"
    assert params["fields"] == "summary,status,assignee,priority,updated,issuetype"
    expected = base64.b64encode(b"owner@example.com:jira-token").decode()
    assert seen["auth"] == f"Basic {expected}"


async def test_max_results_clamped_to_25() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return jira_page([])

    client = make_client(handler)
    await client.search_issues("order by updated desc", max_results=200)

    assert seen["params"]["maxResults"] == "25"


async def test_bad_jql_surfaces_the_api_message() -> None:
    """The agent can only self-correct a JQL mistake if it sees Jira's reason."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"errorMessages": ["Error in the JQL Query: unknown field 'asignee'."]}
        )

    client = make_client(handler)
    with pytest.raises(JiraError, match="asignee"):
        await client.search_issues("asignee = currentUser()")


async def test_401_raises_jira_error() -> None:
    client = make_client(lambda request: httpx.Response(401))
    with pytest.raises(JiraError, match="401"):
        await client.search_issues("order by updated desc")


async def test_missing_fields_become_defaults_not_key_errors() -> None:
    """Unassigned issues have assignee: null - that must not crash the tool."""
    bare = {"key": "CON-7", "fields": {"summary": "orphan", "assignee": None}}
    client = make_client(lambda request: jira_page([bare]))

    issues = await client.search_issues("order by updated desc")

    assert issues[0]["key"] == "CON-7"
    assert issues[0]["assignee"] == ""
    assert issues[0]["status"] == ""
