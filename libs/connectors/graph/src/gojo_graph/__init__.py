"""Read-only Microsoft Graph mail connector.

Thin by design (GOJO-MASTER.md 8.1): this package fetches and returns, it
never reasons. It is SDK-free - the Agent SDK tool wrappers live in
apps/gojo, and dependencies point apps -> libs only.
"""

from gojo_graph.client import GraphError, GraphMailClient

__all__ = ["GraphError", "GraphMailClient"]
