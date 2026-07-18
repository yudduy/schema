"""Anti-cheat guard: run_python audit hook + run_shell path guard + post-run audit."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from schema_harness.guard import (
    audit_events,
    sandbox_exec_argv,
    shell_command_safe,
    wrap_python,
)

REPO = str(Path(__file__).resolve().parents[1])
GAME_SRC = f"{REPO}/environment_files/bp35/0a0ad940/bp35.py"


def _run_guarded(code: str, workdir: Path) -> tuple[int, str]:
    full = wrap_python(code, workdir)
    command, reason = sandbox_exec_argv(
        [sys.executable, "-c", full],
        workdir=workdir,
        read_paths=(sys.prefix, sys.base_prefix),
        allow_read_metadata=True,
    )
    assert command is not None, reason
    proc = subprocess.run(
        command, cwd=str(workdir),
        capture_output=True, text=True, timeout=30,
    )
    return proc.returncode, proc.stdout + proc.stderr


def _run_shell(command: str, workdir: Path) -> tuple[int, str]:
    sandboxed, reason = sandbox_exec_argv(
        ["/bin/sh", "-c", command],
        workdir=workdir,
    )
    assert sandboxed is not None, reason
    proc = subprocess.run(
        sandboxed,
        cwd=workdir,
        capture_output=True,
        text=True,
        timeout=30,
        env={"HOME": str(Path.home()), "PATH": "/usr/bin:/bin"},
    )
    return proc.returncode, proc.stdout + proc.stderr


def test_run_python_allows_workdir_and_stdlib(tmp_path):
    rc, out = _run_guarded("open('a.txt','w').write('x'); import json,numpy; print('ok', numpy.array([1,2]).sum())", tmp_path)
    assert rc == 0 and "ok 3" in out


def test_run_python_blocks_game_source(tmp_path):
    rc, out = _run_guarded(f"print(open({GAME_SRC!r}).read())", tmp_path)
    assert rc != 0 and "outside workdir blocked" in out


def test_run_python_os_sandbox_blocks_ctypes_bypass(tmp_path):
    outside = tmp_path.parent / "ctypes-secret.txt"
    outside.write_text("sandbox-secret", encoding="utf-8")
    code = (
        "import ctypes, os\n"
        "libc=ctypes.CDLL(None)\n"
        "libc.open.argtypes=[ctypes.c_char_p, ctypes.c_int]\n"
        f"fd=libc.open({str(outside).encode()!r}, 0)\n"
        "assert fd >= 0, 'OS sandbox blocked ctypes open'\n"
        "print(os.read(fd, 100))\n"
    )
    rc, out = _run_guarded(code, tmp_path)
    assert rc != 0
    assert "sandbox-secret" not in out


def test_run_python_blocks_network_and_subprocess(tmp_path):
    rc, out = _run_guarded("import socket; socket.socket().connect(('1.1.1.1',80))", tmp_path)
    assert rc != 0 and "network access blocked" in out
    rc, out = _run_guarded(f"import subprocess; subprocess.run(['cat', {GAME_SRC!r}])", tmp_path)
    assert rc != 0 and "subprocess/exec blocked" in out


def test_run_shell_path_guard(tmp_path):
    wd = tmp_path
    assert shell_command_safe("head -5 notes.md", wd, REPO)[0]
    assert shell_command_safe("grep -rn x .", wd, REPO)[0]
    assert not shell_command_safe(f"cat {GAME_SRC}", wd, REPO)[0]
    assert not shell_command_safe("find / -name 'r11l*.py'", wd, REPO)[0]
    assert not shell_command_safe("cat /etc/passwd", wd, REPO)[0]


def test_run_shell_os_sandbox_blocks_indirect_external_reads(tmp_path):
    outside = tmp_path.parent / "shell-secret.txt"
    outside.write_text("sandbox-secret", encoding="utf-8")
    (tmp_path / "escape-link").symlink_to(outside)
    attempts = [
        "cat ../shell-secret.txt",
        "target=../shell-secret.txt; cat \"$target\"",
        "cat escape-link",
        "python3 -c 'print(open(\"../shell-secret.txt\").read())'",
    ]
    for attempt in attempts:
        rc, out = _run_shell(attempt, tmp_path)
        assert rc != 0, attempt
        assert "sandbox-secret" not in out


def test_run_shell_os_sandbox_allows_normal_workdir_tools_and_denies_network(tmp_path):
    (tmp_path / "notes.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    rc, out = _run_shell("head -1 notes.txt; grep beta notes.txt; sed -n '1p' notes.txt", tmp_path)
    assert rc == 0
    assert out.splitlines() == ["alpha", "beta", "alpha"]

    rc, _ = _run_shell("curl --connect-timeout 1 https://example.com", tmp_path)
    assert rc != 0


def test_audit_events_flags_leak(tmp_path):
    events = tmp_path / "events.jsonl"
    events.write_text(
        '{"kind":"tool_started","seq":1,"name":"mcp__locus__read_file","args":{"path":"notes.md"}}\n'
        '{"kind":"tool_started","seq":2,"name":"mcp__locus__run_shell","args":{"command":"cat x/environment_files/y"}}\n'
    )
    result = audit_events(events, REPO)
    assert not result["clean"]
    assert result["violations"][0]["seq"] == 2
