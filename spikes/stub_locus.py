"""Stub `locus` MCP server for the Step 0b headless-driver spike.

Not the real harness. Exposes two stdio MCP tools so we can verify that a headless
`claude -p` turn can: load deferred MCP tools, call one, hit the post-commit lock,
and refuse built-ins. Every call is appended to $LOCUS_LOG (JSONL) so the driver can
inspect what happened out-of-band. State is per-process (one process per turn), which
mirrors the real per-turn spawn.
"""

import json
import os
import time

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("locus")
_committed = False


def _log(event: dict) -> None:
    path = os.environ.get("LOCUS_LOG")
    if not path:
        return
    event = {"ts": time.time(), **event}
    with open(path, "a") as f:
        f.write(json.dumps(event) + "\n")
        f.flush()


@mcp.tool()
def echo_state() -> str:
    """Return the current (canned) game state header."""
    _log({"tool": "echo_state", "rejected": _committed})
    if _committed:
        return "Already committed this turn — end your turn now."
    return "State: NOT_FINISHED | level 0/9\nLegal actions: [1, 2]\nWorld model: NONE yet; history: 0 transitions."


@mcp.tool()
def commit_actions(actions: list[int], reason: str = "") -> str:
    """Commit a list of action ids and end the turn."""
    global _committed
    _log({"tool": "commit_actions", "actions": actions, "reason": reason, "rejected": _committed})
    if _committed:
        return "Already committed this turn — end your turn now."
    _committed = True
    return f"Committed {len(actions)} action(s). Stop now — end your turn, do not call more tools."


if __name__ == "__main__":
    mcp.run()
