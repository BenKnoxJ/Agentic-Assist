"""Logging setup — one turn, one id, every line.

Written after spending several minutes correlating a single Teams turn by
eyeballing timestamps across three loggers. With a turn id that is a grep.

Format is key=value rather than JSON: the journal is read by a human on one
box, and `journalctl -u gojo | grep turn=abc123` beats piping through jq. If
this ever ships logs somewhere that parses them, swap the formatter and
nothing else changes.
"""

import contextvars
import logging
import uuid

# Set once per turn and read by every log record emitted while handling it,
# including from inside the graph, without threading a parameter through
# every function signature.
turn_id: contextvars.ContextVar[str] = contextvars.ContextVar("turn_id", default="-")


def new_turn_id() -> str:
    """Start a new turn and return its id."""
    value = uuid.uuid4().hex[:8]
    turn_id.set(value)
    return value


class TurnIdFilter(logging.Filter):
    """Attaches the current turn id to every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.turn = turn_id.get()
        return True


def configure(level: int = logging.INFO) -> None:
    """Install the handler.

    Uvicorn configures its own loggers and leaves the root at WARNING, so
    without this every application log line is silently dropped - which is
    how the per-turn cost figures went nowhere when they were first added.
    """
    handler = logging.StreamHandler()
    handler.addFilter(TurnIdFilter())
    handler.setFormatter(
        logging.Formatter("%(levelname)s turn=%(turn)s %(name)s %(message)s")
    )

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

    # Uvicorn's access log is one line per request and duplicates what the
    # turn logs already say, with none of the context.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
