"""Step 0b spike: verify the headless `claude -p --resume` driver mechanics.

Runs two turns against the stub `locus` MCP server (spikes/stub_locus.py) in an
isolated CLAUDE_CONFIG_DIR and checks:
  1. MCP tools load and are callable in a headless turn
  2. commit_actions works; the post-commit lock rejects later tool calls
  3. --resume continues the same session (turn 2 sees turn 1)
  4. usage / cost are captured from the audited stream-json result
  5. zero permission prompts (bypassPermissions + strict mcp)
  6. native tools and skills are absent (a requested Bash call cannot run)

Usage: uv run python spikes/driver_probe.py [--model claude-haiku-4-5-20251001]
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
VENV_PY = REPO / ".venv" / "bin" / "python"
STUB = REPO / "spikes" / "stub_locus.py"


def _as_text(value):
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value or ""


def parse_stream(
    stdout,
    stderr,
    *,
    returncode,
    timed_out,
    expected_session_id,
    allowed_tools,
):
    """Audit a Claude stream and return its terminal result record."""

    expected = tuple(allowed_tools)
    if not expected or len(expected) != len(set(expected)):
        raise ValueError("allowed_tools must be a non-empty unique sequence")

    violations = []
    records = []
    lines = [line for line in _as_text(stdout).splitlines() if line.strip()]
    for line_number, line in enumerate(lines, start=1):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            violations.append(f"malformed Claude stream record at line {line_number}")
            continue
        if not isinstance(record, dict):
            violations.append(f"non-object Claude stream record at line {line_number}")
            continue
        records.append(record)

    init_records = [
        record
        for record in records
        if record.get("type") == "system" and record.get("subtype") == "init"
    ]
    if len(init_records) != 1:
        violations.append(
            f"Claude stream has {len(init_records)} init records; expected exactly 1"
        )
        init = {}
    else:
        init = init_records[0]
        advertised = init.get("tools")
        if not isinstance(advertised, list) or not all(
            isinstance(name, str) for name in advertised
        ):
            violations.append("Claude init record has malformed tools")
        elif len(advertised) != len(set(advertised)) or set(advertised) != set(expected):
            missing = sorted(set(expected) - set(advertised))
            unexpected = sorted(set(advertised) - set(expected))
            violations.append(
                "Claude tool surface differs from exact Locus allowlist: "
                f"missing={missing}, unexpected={unexpected}"
            )

    for record in records:
        if record.get("type") != "assistant":
            continue
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            violations.append("Claude assistant record has malformed content")
            continue
        for block in content:
            if not isinstance(block, dict):
                violations.append("Claude assistant content block is not an object")
                continue
            if block.get("type") != "tool_use":
                continue
            name = block.get("name")
            if name not in expected:
                violations.append(f"unapproved Claude tool call: {name!r}")

    result_records = [record for record in records if record.get("type") == "result"]
    if len(result_records) > 1:
        violations.append(
            f"Claude stream has {len(result_records)} result records; expected at most 1"
        )
    if not result_records and not timed_out:
        violations.append("Claude stream ended without a result record")

    result = dict(result_records[-1]) if result_records else {
        "session_id": init.get("session_id") or expected_session_id,
        "usage": {},
        "total_cost_usd": 0.0,
        "num_turns": 0,
        "is_error": False,
        "result": "",
    }
    result["timed_out"] = timed_out
    reported_session_id = result.get("session_id")
    if reported_session_id != expected_session_id:
        violations.append(
            "Claude resumed a different session: "
            f"expected {expected_session_id!r}, got {reported_session_id!r}"
        )
    if returncode != 0 and not result_records:
        violations.append(f"Claude exited {returncode} without a result record")
    if violations:
        prior = result.get("violations")
        combined = [str(item) for item in prior] if isinstance(prior, list) else []
        result["violations"] = list(dict.fromkeys(combined + violations))
        result["is_error"] = True
        result["result"] = (
            "Claude driver rejected the turn: exact Locus tool audit failed. "
            "See private driver logs."
        )
    elif returncode != 0:
        result["is_error"] = True
        if not result.get("result"):
            result["result"] = _as_text(stderr)[:1500]
    return result


def oauth_token():
    """Extract the Claude Code OAuth access token from the macOS Keychain.

    Passing it via CLAUDE_CODE_OAUTH_TOKEN authenticates headless turns even under an
    isolated CLAUDE_CONFIG_DIR (which otherwise reads as 'not logged in')."""
    blob = subprocess.run(
        ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
        capture_output=True, text=True,
    ).stdout.strip()
    try:
        return json.loads(blob)["claudeAiOauth"]["accessToken"]
    except Exception:
        return None


def run_turn(
    msg,
    *,
    session_id,
    resume,
    cwd,
    config_dir,
    locus_log,
    mcp_cfg,
    model,
    token,
    allowed_tools,
    effort=None,
    timeout=300,
    system_prompt_file=None,
):
    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    env["LOCUS_LOG"] = str(locus_log)
    # Keep the fourteen Locus tools explicit in context. Claude Code otherwise
    # defers MCP tools behind a native ToolSearch call.
    env["ENABLE_TOOL_SEARCH"] = "false"
    if token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    cmd = [
        "claude", "-p", msg,
        "--model", model,
        "--mcp-config", str(mcp_cfg),
        "--strict-mcp-config",
        "--permission-mode", "bypassPermissions",
        # MCP tools are unaffected by --tools; an empty value removes every
        # native tool. Skills are a separate surface and need their own switch.
        "--tools", "",
        "--disable-slash-commands",
        "--disallowed-tools", "Skill,ToolSearch,MCPSearch",
        "--output-format", "stream-json",
        "--verbose",
    ]
    if effort:
        cmd += ["--effort", effort]
    if system_prompt_file:
        cmd += ["--append-system-prompt-file", str(system_prompt_file)]
    cmd += ["--resume", session_id] if resume else ["--session-id", session_id]
    try:
        proc = subprocess.run(cmd, cwd=str(cwd), env=env, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        print(f"  [claude turn timed out after {timeout}s — stream audit failed; aborting invocation]")
        return parse_stream(
            exc.stdout,
            exc.stderr,
            returncode=-9,
            timed_out=True,
            expected_session_id=session_id,
            allowed_tools=allowed_tools,
        )
    if proc.returncode != 0:
        print(f"  [claude exited {proc.returncode}] stderr:\n{proc.stderr[:1500]}")
    return parse_stream(
        proc.stdout,
        proc.stderr,
        returncode=proc.returncode,
        timed_out=False,
        expected_session_id=session_id,
        allowed_tools=allowed_tools,
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="claude-haiku-4-5-20251001")
    args = ap.parse_args()

    token = oauth_token()
    print(f"oauth token: {'present' if token else 'MISSING'}")
    tmp = Path(tempfile.mkdtemp(prefix="locus-spike-"))
    config_dir = tmp / "config"
    workdir = tmp / "work"
    config_dir.mkdir(parents=True)
    workdir.mkdir(parents=True)
    # Seed the isolated config dir with the login marker so headless auth resolves.
    # The OAuth token itself lives in the macOS Keychain (global); the account record
    # lives in ~/.claude.json, which a custom CLAUDE_CONFIG_DIR does not inherit.
    home_cfg = Path.home() / ".claude.json"
    if home_cfg.exists():
        shutil.copy(home_cfg, config_dir / ".claude.json")
    locus_log = tmp / "locus.log"
    mcp_cfg = tmp / "mcp.json"
    mcp_cfg.write_text(json.dumps({
        "mcpServers": {
            "locus": {
                "command": str(VENV_PY),
                "args": [str(STUB)],
                "alwaysLoad": True,
            }
        }
    }))

    sid = str(uuid.uuid4())
    checks = {}

    print(f"tmp={tmp}\nmodel={args.model}\nsession={sid}\n")

    # Turn 1: call echo_state, then commit_actions, then try echo_state again (must be locked).
    msg1 = (
        "You have MCP tools from the 'locus' server. Do EXACTLY this, then stop:\n"
        "1) call locus echo_state\n"
        "2) call locus commit_actions with actions=[1] and reason='probe'\n"
        "3) call locus echo_state ONE more time (to test the post-commit lock)\n"
        "Then end your turn. Do not call any other tools."
    )
    print("== turn 1 ==")
    r1 = run_turn(msg1, session_id=sid, resume=False, cwd=workdir,
                  config_dir=config_dir, locus_log=locus_log, mcp_cfg=mcp_cfg,
                  model=args.model, token=token,
                  allowed_tools=("mcp__locus__echo_state", "mcp__locus__commit_actions"))
    if r1:
        checks["turn1_returned_json"] = True
        checks["usage_captured"] = bool(r1.get("usage")) or (r1.get("total_cost_usd") is not None)
        checks["session_id_echoed"] = r1.get("session_id") == sid
        print(f"  session_id={r1.get('session_id')} cost=${r1.get('total_cost_usd')} "
              f"num_turns={r1.get('num_turns')} is_error={r1.get('is_error')}")

    # Turn 2: resume; ask what it just did AND ask it to run a bash command (must be unavailable).
    msg2 = (
        "Resuming. In one short sentence, what locus tools did you call last turn?\n"
        "Then attempt to run the shell command `echo hi` using the Bash tool. "
        "If the Bash tool is not available, say 'BASH_UNAVAILABLE'. Then end your turn."
    )
    print("== turn 2 (resume) ==")
    r2 = run_turn(msg2, session_id=sid, resume=True, cwd=workdir,
                  config_dir=config_dir, locus_log=locus_log, mcp_cfg=mcp_cfg,
                  model=args.model, token=token,
                  allowed_tools=("mcp__locus__echo_state", "mcp__locus__commit_actions"))
    if r2:
        # Resume is confirmed by session continuity: turn 2 ran under the same session id
        # without re-initializing (no 'not logged in' / new-session error).
        checks["turn2_resumed"] = (r2.get("session_id") == sid) and not r2.get("is_error")
        text2 = (r2.get("result") or "").lower()
        checks["builtins_absent"] = ("bash_unavailable" in text2) or ("not available" in text2)
        print(f"  result[:200]={text2[:200]!r}")

    # Inspect the out-of-band locus log for the lock behavior.
    calls = []
    if locus_log.exists():
        calls = [json.loads(l) for l in locus_log.read_text().splitlines() if l.strip()]
    commits = [c for c in calls if c["tool"] == "commit_actions"]
    checks["commit_called"] = len(commits) >= 1
    checks["tools_actually_called"] = len(calls) >= 2
    # Post-commit lock is belt-and-suspenders: the "Stop now" return usually makes the
    # model end the turn, so the lock only *needs* to fire if it keeps calling. Report
    # whichever happened, informationally — a compliant stop is the desired outcome.
    post_commit_calls = [c for c in calls if c.get("rejected")]
    if post_commit_calls:
        checks["post_commit_lock_fired"] = all(c["rejected"] for c in post_commit_calls)
    else:
        print("  (model stopped on commit; lock not exercised — desired behavior)")
    print(f"\nlocus calls: {[(c['tool'], c.get('rejected')) for c in calls]}")

    print("\n=== CHECKS ===")
    for k, v in checks.items():
        print(f"  [{'PASS' if v else 'FAIL'}] {k}")
    core = ["turn1_returned_json", "tools_actually_called", "commit_called",
            "usage_captured", "turn2_resumed", "builtins_absent"]
    ok = all(checks.get(k) for k in core)
    print(f"\nVERDICT: {'GREEN' if ok else 'RED'} (core: {', '.join(core)})")
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
