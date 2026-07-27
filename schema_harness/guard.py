"""Anti-cheat guards for ``locus`` subprocesses and post-run auditing."""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Sequence
from pathlib import Path

from .events import iter_json_objects

# System roots the interpreter genuinely needs to read to run + import the stdlib.
_SYSTEM_PREFIXES = ("/usr/", "/System/", "/Library/", "/opt/homebrew/",
                    "/private/var/folders/", "/private/var/db/", "/dev/null", "/dev/urandom")
_SANDBOX_EXEC = Path("/usr/bin/sandbox-exec")


def _sandbox_literal(path: str | Path) -> str:
    """Quote a canonical path for a Sandbox Profile Language string."""

    value = os.path.realpath(os.fspath(path))
    return value.replace("\\", "\\\\").replace('"', '\\"')


def sandbox_exec_argv(
    command: Sequence[str],
    *,
    workdir: str | Path,
    read_paths: Sequence[str | Path] = (),
    read_literals: Sequence[str | Path] = (),
    deny_read_paths: Sequence[str | Path] = (),
    deny_read_literals: Sequence[str | Path] = (),
    write_paths: Sequence[str | Path] | None = None,
    deny_write_paths: Sequence[str | Path] = (),
    deny_write_literals: Sequence[str | Path] = (),
    allow_subprocesses: bool = True,
    allow_read_metadata: bool = False,
) -> tuple[list[str] | None, str]:
    """Wrap a command in the host's deny-by-default macOS sandbox.

    The child may read its workdir and explicit runtime paths, write only configured
    paths, optionally spawn children that inherit the sandbox, and cannot use the
    network. Unsupported hosts fail closed instead of silently running unconfined.
    """

    if sys.platform != "darwin":
        return None, "OS sandbox unavailable: Schema subprocesses require macOS"
    if not _SANDBOX_EXEC.is_file() or not os.access(_SANDBOX_EXEC, os.X_OK):
        return None, f"OS sandbox unavailable: {_SANDBOX_EXEC} is not executable"

    metadata_rule = "(allow file-read-metadata)" if allow_read_metadata else ""
    extra_reads = "\n".join(
        f'  (subpath "{_sandbox_literal(path)}")' for path in read_paths
    )
    literal_reads = "\n".join(
        f'  (literal "{_sandbox_literal(path)}")' for path in read_literals
    )
    writable = (workdir,) if write_paths is None else write_paths
    write_rules = "\n".join(
        f'(allow file-write* (subpath "{_sandbox_literal(path)}"))'
        for path in writable
    )
    denied_writes = "\n".join(
        [
            *(f'(deny file-write* (subpath "{_sandbox_literal(path)}"))'
              for path in deny_write_paths),
            *(f'(deny file-write* (literal "{_sandbox_literal(path)}"))'
              for path in deny_write_literals),
        ]
    )
    denied_reads = "\n".join(
        [
            *(f'(deny file-read* (subpath "{_sandbox_literal(path)}"))'
              for path in deny_read_paths),
            *(f'(deny file-read* (literal "{_sandbox_literal(path)}"))'
              for path in deny_read_literals),
        ]
    )
    process_rule = (
        "(allow process*)"
        if allow_subprocesses
        else f'(allow process-exec (literal "{_sandbox_literal(command[0])}"))'
    )
    profile = f"""(version 1)
(deny default)
(import \"system.sb\")
{process_rule}
{metadata_rule}
(allow file-read*
  (subpath \"/bin\")
  (subpath \"/usr/bin\")
  (subpath \"/System\")
  (subpath \"/Library\")
  (subpath \"/opt/homebrew\")
  (subpath \"/usr/local\")
  (literal \"/private/var/select/sh\")
  (subpath (param \"WORKDIR\"))
{extra_reads}
{literal_reads})
{denied_reads}
{write_rules}
{denied_writes}
(deny file-read* (subpath \"/private/etc\"))
(deny network*)
"""
    workdir_real = os.path.realpath(os.fspath(workdir))
    return [
        str(_SANDBOX_EXEC),
        "-D",
        f"WORKDIR={workdir_real}",
        "-p",
        profile,
        *command,
    ], ""


def python_guard_preamble(workdir: str | Path) -> str:
    """A preamble that installs a permanent audit hook (cannot be removed once set).

    Blocks: file reads/writes whose realpath is outside the workdir + interpreter/system
    roots (so `environment_files/…` under the repo is unreachable), network, and
    subprocess/exec-of-external (which would run un-hooked)."""
    wd = os.path.realpath(str(workdir))
    allow = (wd, os.path.realpath(sys.prefix), os.path.realpath(sys.base_prefix)) + _SYSTEM_PREFIXES
    return (
        "import sys as _s, os as _o\n"
        f"_ALLOW = {allow!r}\n"
        "def _guard(event, args):\n"
        "    if event == 'open' and args:\n"
        "        p = args[0]\n"
        "        try: rp = _o.path.realpath(_o.fspath(p))\n"
        "        except Exception: return\n"
        "        if not any(rp == a or rp.startswith(a if a.endswith('/') else a + '/') for a in _ALLOW):\n"
        "            raise PermissionError('run_python: filesystem access outside workdir blocked: ' + str(p))\n"
        "    elif event.startswith('socket.') or event.startswith('urllib.') or event == 'http.client.connect':\n"
        "        raise PermissionError('run_python: network access blocked')\n"
        "    elif event in ('os.system', 'os.exec', 'os.posix_spawn', 'subprocess.Popen'):\n"
        "        raise PermissionError('run_python: subprocess/exec blocked')\n"
        "_s.addaudithook(_guard)\n"
        "del _guard\n"
    )


def wrap_python(code: str, workdir: str | Path) -> str:
    return python_guard_preamble(workdir) + "\n" + code


# ---- run_shell path guard -------------------------------------------------

# Match absolute path arguments, including a bare "/" (root traversal like `find /`).
# Lookbehind excludes \w . / so relative paths (./notes.md, a/b) are not treated as absolute.
_ABS_PATH = re.compile(r"(?<![\w./])/[^\s'\"|&;><)]*")
_SHELL_BIN_PREFIXES = ("/bin/", "/usr/bin/", "/opt/homebrew/bin/", "/usr/local/bin/",
                       "/dev/null", "/dev/stdin", "/dev/stdout", "/dev/stderr")


def shell_command_safe(command: str, workdir: str | Path, repo: str | Path) -> tuple[bool, str]:
    """Reject shell commands that reference paths outside the workdir or name game source.

    Conservative: any absolute path in the command must resolve under the workdir; and the
    command must not mention the repo path or environment_files."""
    wd = os.path.realpath(str(workdir))
    repo_real = os.path.realpath(str(repo))
    if "environment_files" in command:
        return False, "run_shell: reference to environment_files blocked"
    if repo_real in command:
        return False, "run_shell: reference to the harness repo blocked"
    for match in _ABS_PATH.findall(command):
        rp = os.path.realpath(match)
        if rp == wd or rp.startswith(wd + "/"):
            continue
        # allow the executables themselves (e.g. /bin/ls), not absolute DATA paths
        if match.startswith(_SHELL_BIN_PREFIXES):
            continue
        return False, f"run_shell: absolute path outside workdir blocked: {match or '/'}"
    return True, ""


# ---- post-run audit (disclosure backstop) ---------------------------------

_LEAK_TOKENS = ("environment_files",)


def audit_events(events_path: str | Path, repo: str | Path) -> dict:
    """Scan a run's events.jsonl for any tool call that touched game source / repo internals.

    Returns {'clean': bool, 'violations': [...]}. Inspects run_python code, run_shell
    commands, and file-tool (read_file/find/grep/cp/mv) path arguments."""
    repo_real = os.path.realpath(str(repo))
    violations: list[dict] = []
    tokens = _LEAK_TOKENS + (repo_real,)
    for _, e in iter_json_objects(events_path):
        if e.get("kind") != "tool_started":
            continue
        name = str(e.get("name", "")).split("__")[-1]
        blob = json.dumps(e.get("args", {}))
        hit = next((t for t in tokens if t in blob), None)
        if hit:
            violations.append({"seq": e.get("seq"), "tool": name, "token": hit, "args": e.get("args")})
    return {"clean": not violations, "violations": violations}
