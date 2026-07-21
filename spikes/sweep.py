#!/usr/bin/env python3
"""Resumable 25-game ARC-AGI-3 sweep orchestrator.

Serial (respects the single-live-run flock), self-healing on rate-limit driver
errors (wait + resume), auto-resumes token/turn-capped games (budget reconcile),
scores each game with the vendored scorer, runs the blog's <80 fallback pass,
and aggregates the benchmark mean. Checkpoints to ~/schema-sweep/ledger.json so
it resumes cleanly after any interruption (including a reboot + relaunch).

Usage:  ONLY_RESET_LEVELS=true uv run python spikes/sweep.py <phase>
        phase in {sol, opus, selftest}
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("ONLY_RESET_LEVELS", "true")

REPO = Path("/Users/c-dnguyen/Documents/project/schema")
ROOT = Path.home() / "schema-sweep"
ROOT.mkdir(exist_ok=True)
LEDGER = ROOT / "ledger.json"
PROGRESS = ROOT / "progress.log"

GAMES = [
    "ar25", "bp35", "cd82", "cn04", "dc22", "ft09", "g50t", "ka59", "lf52",
    "lp85", "ls20", "m0r0", "r11l", "re86", "s5i5", "sb26", "sc25", "sk48",
    "sp80", "su15", "tn36", "tr87", "tu93", "vc33", "wa30",
]

# Contamination split (see REPRODUCE.md): CLEAN = never blog-spoiled and never used
# in tuning; CONTAMINATED = blog-discussed mechanics/trace-card mentions or a dev game.
# CLEAN is ordered hardest-first (by human baseline total) so the long-game stress
# tests run while quota is freshest. Subsets accumulate into one ledger, so
# `sweep.py sol clean` then `sweep.py sol rest` yields the full 25.
CLEAN = [
    "lf52", "re86", "dc22", "cn04", "ka59", "s5i5", "sp80", "vc33", "tr87",
    "lp85", "su15", "sc25", "tn36", "sb26", "cd82", "tu93",
]
CONTAMINATED = ["bp35", "ls20", "ft09", "wa30", "m0r0", "sk48", "g50t", "ar25", "r11l"]
SUBSETS = {"clean": CLEAN, "rest": CONTAMINATED, "contaminated": CONTAMINATED, "full": GAMES}

PHASES = {
    "sol": {
        "provider": "codex", "model": "gpt-5.6-sol",
        "primary_effort": "xhigh",
        "fallback_model": "gpt-5.6-sol", "fallback_effort": "max",
    },
    "opus": {
        "provider": "claude", "model": "claude-opus-4-8",
        "primary_effort": "max",
        "fallback_model": "claude-fable-5", "fallback_effort": "max",
    },
}

MAX_ACTIONS = 3000
START_MAX_TURNS = 80
TURN_GROW = 40
MAX_TURNS_CAP = 400
MAX_INVOCATIONS = 60
MAX_DRIVER_RETRIES = 24
NO_PROGRESS_GIVEUP = 3
TURN_TIMEOUT = 3600
TURN_TOKEN_CAP = 20_000_000


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    with open(PROGRESS, "a") as f:
        f.write(line + "\n")
    print(line, flush=True)


def load_ledger() -> dict:
    if LEDGER.exists():
        return json.loads(LEDGER.read_text())
    return {}


def save_ledger(ledger: dict) -> None:
    tmp = LEDGER.with_suffix(".tmp")
    tmp.write_text(json.dumps(ledger, indent=2))
    tmp.replace(LEDGER)


def snapshot_state(wd: Path):
    """(state, level, history_len) parsed straight from events.jsonl — no imports."""
    ev = wd / "events.jsonl"
    state, level, hist, turns = None, 0, 0, 0
    if not ev.exists():
        return state, level, hist, turns
    for line in ev.read_text().splitlines():
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        k = e.get("kind")
        if k == "action_taken":
            hist += 1
            state = e.get("state", state)
            level = e.get("level", level)
        elif k == "turn_started":
            turns += 1
        elif k == "run_finished":
            state = e.get("state", state)
    return state, level, hist, turns


def reconcile_budget(wd: Path, max_turns: int) -> None:
    rj = wd / "run.json"
    if not rj.exists():
        return
    d = json.loads(rj.read_text())
    drv = d.get("driver")
    if drv:
        drv["max_turns"] = max_turns
        drv["turn_token_cap"] = TURN_TOKEN_CAP
        rj.write_text(json.dumps(d, indent=2))


def run_once(game, wd, model, effort, provider, max_turns) -> str:
    env = dict(os.environ, ONLY_RESET_LEVELS="true")
    reconcile_budget(wd, max_turns)
    cmd = [
        sys.executable, "-m", "schema_harness.runner",
        "--provider", provider, "--model", model, "--game", game,
        "--effort", effort,
        "--max-turns", str(max_turns), "--max-actions", str(MAX_ACTIONS),
        "--turn-timeout", str(TURN_TIMEOUT), "--no-progress-turns", "8",
        "--turn-token-cap", str(TURN_TOKEN_CAP), "--run-token-cap", "0",
        "--workdir", str(wd),
    ]
    if provider == "claude":
        cmd += ["--run-cost-cap", "100000", "--turn-cost-cap", "10000"]
    r = subprocess.run(cmd, cwd=str(REPO), env=env, capture_output=True, text=True)
    return (r.stdout or "") + "\n" + (r.stderr or "")


def score_game(wd: Path) -> dict:
    """Run the vendored scorer; return {rhae, state, levels, workdir}."""
    scorer = REPO / "vendor" / "score_trajectories.py"
    baseline = REPO / "vendor" / "baseline_actions.csv"
    if not (wd / "events.jsonl").exists() or not (wd / "run.json").exists():
        return {"rhae": 0.0, "state": "NO_RUN", "levels": "0/0", "workdir": str(wd)}
    with tempfile.TemporaryDirectory(prefix="sweep-score-") as tmp:
        root = Path(tmp)
        shutil.copy2(baseline, root / "baseline_actions.csv")
        for ds in ("gpt_5_6_sol", "claude_fable_opus"):
            traj = root / ds / wd.name
            traj.mkdir(parents=True)
            shutil.copy2(wd / "events.jsonl", traj / "events.jsonl")
            shutil.copy2(wd / "run.json", traj / "run.json")
        r = subprocess.run(
            [sys.executable, str(scorer), "--root", str(root),
             "--expected", "0", "--no-manifest-check", "--compact"],
            capture_output=True, text=True,
        )
    out = r.stdout + r.stderr
    m = re.search(
        r"\|\s*\d+\s*\|\s*\w+\s*\|\s*\w+\s*\|[^|]*\|[^|]*\|\s*(\w+)\s*\|"
        r"\s*(\d+/\d+)\s*\|\s*\d+\s*\|\s*([\d.]+)%\s*\|",
        out,
    )
    if not m:
        return {"rhae": 0.0, "state": "SCORE_FAIL", "levels": "0/0",
                "workdir": str(wd), "raw": out[-400:]}
    return {"rhae": float(m.group(3)), "state": m.group(1),
            "levels": m.group(2), "workdir": str(wd)}


def run_game(phase, game, model, effort, provider, tag) -> dict:
    wd = ROOT / f"{phase}-{tag}-{game}"
    _, _, _, turns_done = snapshot_state(wd)
    max_turns = max(START_MAX_TURNS, turns_done + TURN_GROW)
    invocations = driver_retries = no_progress = 0
    while True:
        state, level, hist, turns_done = snapshot_state(wd)
        if state == "WIN":
            log(f"{game} [{tag}] already WIN")
            break
        if hist >= MAX_ACTIONS:
            log(f"{game} [{tag}] exhausted {MAX_ACTIONS} actions")
            break
        if invocations >= MAX_INVOCATIONS:
            log(f"{game} [{tag}] giving up (invocations={invocations})")
            break
        if turns_done + 1 > max_turns:
            max_turns = min(MAX_TURNS_CAP, turns_done + TURN_GROW)
        invocations += 1
        out = run_once(game, wd, model, effort, provider, max_turns)
        state, level, hist, turns_done = snapshot_state(wd)
        log(f"{game} [{tag}] inv={invocations} state={state} lvl={level} "
            f"hist={hist} turns={turns_done} max_turns={max_turns}")
        if state == "WIN" or hist >= MAX_ACTIONS:
            continue
        if "driver returned an error" in out:
            driver_retries += 1
            if driver_retries > MAX_DRIVER_RETRIES:
                log(f"{game} [{tag}] too many driver errors; giving up")
                break
            wait = min(3600, 900 * driver_retries)
            log(f"{game} [{tag}] driver error (rate limit?), backoff {wait}s "
                f"(retry {driver_retries}/{MAX_DRIVER_RETRIES})")
            time.sleep(wait)
            continue
        if "no progress" in out:
            no_progress += 1
            if no_progress >= NO_PROGRESS_GIVEUP:
                log(f"{game} [{tag}] stalled (no-progress x{no_progress}); giving up")
                break
            max_turns = min(MAX_TURNS_CAP, max_turns + TURN_GROW)
            continue
        if ("trajectory turn cap" in out or "per-turn token cap" in out
                or "run token cap" in out):
            if max_turns >= MAX_TURNS_CAP:
                log(f"{game} [{tag}] hit MAX_TURNS_CAP; giving up")
                break
            max_turns = min(MAX_TURNS_CAP, max_turns + TURN_GROW)
            continue
        log(f"{game} [{tag}] unknown stop; tail: {out[-250:]}")
        break
    res = score_game(wd)
    log(f"{game} [{tag}] SCORED rhae={res['rhae']} state={res['state']} "
        f"levels={res['levels']}")
    return res


def benchmark_mean(ledger, phase, games) -> float:
    finals = [ledger[phase][g]["final"]["rhae"] for g in games
              if g in ledger[phase] and "final" in ledger[phase][g]]
    return sum(finals) / len(finals) if finals else 0.0


def run_phase(phase: str, games: list) -> None:
    cfg = PHASES[phase]
    ledger = load_ledger()
    ledger.setdefault(phase, {})
    log(f"===== PHASE {phase} START ({cfg['model']} {cfg['primary_effort']}) "
        f"over {len(games)} games: {','.join(games)} =====")
    for game in games:
        g = ledger[phase].setdefault(game, {})
        if g.get("primary_done"):
            log(f"{game} primary already done: {g['primary']['rhae']}%")
            continue
        log(f"--- {phase} PRIMARY {game} ---")
        g["primary"] = run_game(phase, game, cfg["model"],
                                cfg["primary_effort"], cfg["provider"], "primary")
        g["primary_done"] = True
        if g["primary"]["rhae"] >= 80:
            g["final"] = g["primary"]
        save_ledger(ledger)
    for game in games:
        g = ledger[phase][game]
        if "final" in g:
            continue
        if g.get("fallback_done"):
            g["final"] = (g["fallback"] if g["fallback"]["rhae"] > g["primary"]["rhae"]
                          else g["primary"])
            save_ledger(ledger)
            continue
        log(f"--- {phase} FALLBACK {game} (primary {g['primary']['rhae']}%) ---")
        g["fallback"] = run_game(phase, game, cfg["fallback_model"],
                                 cfg["fallback_effort"], cfg["provider"], "fallback")
        g["fallback_done"] = True
        g["final"] = (g["fallback"] if g["fallback"]["rhae"] > g["primary"]["rhae"]
                      else g["primary"])
        save_ledger(ledger)
    mean = benchmark_mean(ledger, phase, games)
    log(f"===== PHASE {phase} DONE: BENCHMARK MEAN RHAE = {mean:.2f}% "
        f"over {len(games)} games ({','.join(games)}) =====")
    save_ledger(ledger)


def selftest() -> None:
    for wd in [Path("/private/tmp/schema-val-xhigh-r11l-1784536776"),
               Path("/private/tmp/schema-val-xhigh-tu93-1784554148")]:
        if wd.exists():
            print(wd.name, "->", score_game(wd))
        else:
            print(wd.name, "-> (missing)")


if __name__ == "__main__":
    phase = sys.argv[1] if len(sys.argv) > 1 else "selftest"
    if phase == "selftest":
        selftest()
    else:
        subset = sys.argv[2] if len(sys.argv) > 2 else "full"
        games = SUBSETS.get(subset)
        if games is None:
            raise SystemExit(f"unknown subset {subset!r}; choose from {list(SUBSETS)}")
        run_phase(phase, games)
