"""Server entrypoint: `python -m gojo`.

Exists so the event-loop choice below lives in code rather than in a command
line that step 4's systemd unit could omit.

⚠ loop="asyncio" is mandatory, not a preference. Under uvloop (which
uvicorn[standard] installs and selects by default) every Claude Agent SDK
call fails deterministically with "Reached maximum number of turns (1)" -
verified 3/3, while the same requests succeed 3/3 under asyncio. The SDK
drives the bundled Claude Code CLI as a subprocess over anyio streams, and
uvloop's subprocess handling breaks that. Nothing is lost: the workload is
I/O-bound on one core (GOJO-MASTER.md 3.1, 4.3), so uvloop's throughput
advantage is not something this system was ever going to spend.
"""

import uvicorn


def main() -> None:
    """Run the HTTP surface. Single worker - see GOJO-MASTER.md 4.3."""
    uvicorn.run(
        "gojo.api:app",
        host="127.0.0.1",  # Caddy terminates TLS and forwards here (3.2).
        port=3000,
        workers=1,
        loop="asyncio",
    )


if __name__ == "__main__":
    main()
