"""Read-only Jira Cloud connector.

Thin by design (GOJO-MASTER.md 8.1): fetches and returns, never reasons.
SDK-free - the Agent SDK tool wrappers live in apps/gojo.
"""

from gojo_jira.client import JiraClient, JiraError

__all__ = ["JiraClient", "JiraError"]
