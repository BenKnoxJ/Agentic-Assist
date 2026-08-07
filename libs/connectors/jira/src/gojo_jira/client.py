"""Jira Cloud client - read-only, delegated auth.

Basic auth with the owner's email + API token: every query runs as the
owner, so currentUser() in JQL is them. Contrast with the Graph connector's
app-only model - two auth models, chosen per surface (GOJO-MASTER.md 8).

Endpoint is /rest/api/3/search/jql; the older /search was removed by
Atlassian. A 400 carries Jira's own JQL diagnosis and is surfaced verbatim
so the agent can correct its query.
"""

import httpx

MAX_RESULTS = 25

# Exactly what the agent may see; nothing outside this list comes back.
FIELDS = "summary,status,assignee,priority,updated,issuetype"


class JiraError(Exception):
    """A Jira request failed in a way the caller should hear about."""


class JiraClient:
    """Fetches issues. Never reasons about them (8.1)."""

    def __init__(
        self,
        base_url: str,
        email: str,
        api_token: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._email = email
        self._api_token = api_token
        self._transport = transport

    async def search_issues(self, jql: str, max_results: int = 10) -> list[dict]:
        """Issues matching a JQL query, reduced to the allowed field set."""
        params = {
            "jql": jql,
            "maxResults": str(min(max(max_results, 1), MAX_RESULTS)),
            "fields": FIELDS,
        }
        async with httpx.AsyncClient(
            transport=self._transport,
            timeout=15.0,
            auth=(self._email, self._api_token),
        ) as http:
            response = await http.get(f"{self._base_url}/rest/api/3/search/jql", params=params)
        _raise_for_status(response)
        return [_reduce(issue) for issue in response.json().get("issues", [])]


def _raise_for_status(response: httpx.Response) -> None:
    if response.is_success:
        return
    if response.status_code == 400:
        try:
            messages = response.json().get("errorMessages", [])
        except ValueError:
            messages = []
        detail = " ".join(messages) or "no detail from Jira"
        raise JiraError(f"Jira rejected the JQL query: {detail}")
    raise JiraError(f"Jira request failed with {response.status_code}.")


def _named(field: dict | None) -> str:
    return (field or {}).get("name", "")


def _reduce(issue: dict) -> dict:
    fields = issue.get("fields", {})
    return {
        "key": issue.get("key", ""),
        "summary": fields.get("summary", ""),
        "status": _named(fields.get("status")),
        "assignee": (fields.get("assignee") or {}).get("displayName", ""),
        "priority": _named(fields.get("priority")),
        "updated": fields.get("updated", ""),
        "issuetype": _named(fields.get("issuetype")),
    }
