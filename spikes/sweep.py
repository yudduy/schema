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

REPO = Path(__file__).resolve().parents[1]
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
# in tuning; CONTAMINATED = blog-discussed mechanics/trace mentions, or a dev game.
# 2026-07-20 re-audit against the FULL blog text (earlier fetches missed the expandable
# case studies): re86/ka59/dc22/lf52/sb26 have detailed mechanism case studies in the
# post (Evidence 1A/1B/2A/2B/2C), so they move to CONTAMINATED. The true held-out set
# is 11 games. Both lists run cheapest-first (ascending human baseline total) to
# maximize completed games per subscription-quota window; per-game protocol (budgets,
# <80 fallback) is unchanged, so ordering does not affect any score.
CLEAN = [
    "cd82", "tn36", "sc25", "su15", "lp85", "tr87", "vc33", "sp80", "s5i5",
    "cn04", "tu93",
]
CONTAMINATED = [
    "ft09", "sb26", "bp35", "ka59", "ls20", "g50t", "sk48", "m0r0", "dc22",
    "re86", "lf52", "wa30", "ar25", "r11l",
]
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
MAX_INVOCATIONS = 100_000        # patient: don't cap out on ride-out retries
MAX_DRIVER_RETRIES = 100_000     # network/quota walls are ridden out, never failed
NO_PROGRESS_GIVEUP = 3           # genuine agent stalls still move on (don't block the sweep)
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


WATCHDOG_GRACE = 900  # events.jsonl quiet for TURN_TIMEOUT+this => hung driver, kill group


def _wait_with_watchdog(proc, ev_path: Path, start_ts: float, max_quiet: float,
                        poll: float = 60) -> bool:
    """Wait for proc; if ev_path sees no writes for max_quiet seconds, SIGKILL its
    process group (a codex stream stuck on a dead network read never returns and
    the runner's turn timeout cannot see it). Returns True iff the watchdog fired."""
    while True:
        try:
            proc.wait(timeout=poll)
            return False
        except subprocess.TimeoutExpired:
            pass
        try:
            last = ev_path.stat().st_mtime
        except OSError:
            last = start_ts
        if time.time() - last > max_quiet:
            import signal
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                pass
            return True


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
    log_path = ROOT / f"runner-{wd.name}.log"
    with open(log_path, "w") as fh:
        proc = subprocess.Popen(cmd, cwd=str(REPO), env=env, stdout=fh,
                                stderr=subprocess.STDOUT, text=True,
                                start_new_session=True)
        hung = _wait_with_watchdog(proc, wd / "events.jsonl", time.time(),
                                   TURN_TIMEOUT + WATCHDOG_GRACE)
    out = log_path.read_text()
    if hung:
        out += "\nWATCHDOG-HANG-KILLED\n"
    return out


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
        if "another live Schema harness run is active" in out:
            raise RuntimeError(
                f"{game}: harness live-lock contention (another runner active) — "
                f"aborting to avoid a false-0.0 cascade; kill stray runners first")
        if state == "WIN" or hist >= MAX_ACTIONS:
            continue
        if "WATCHDOG-HANG-KILLED" in out:
            log(f"{game} [{tag}] hang watchdog fired (events quiet "
                f">{TURN_TIMEOUT + WATCHDOG_GRACE}s — dead network stream?); re-invoking")
            time.sleep(30)
            continue
        if "driver returned an error" in out:
            driver_retries += 1
            if driver_retries == 3:
                log(f"{game} [{tag}] WALL: sustained driver errors — likely quota "
                    f"exhausted or network down. NOT stopping: retrying every ~10min; "
                    f"auto-resumes when a fresh account is logged in or quota resets.")
            wait = min(600, 120 * driver_retries)
            log(f"{game} [{tag}] driver error, backoff {wait}s (retry {driver_retries})")
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
        if turns_done >= max_turns:
            # Turn budget exhausted mid-game. The runner prints no reliable stop
            # string for this (stdout just ends with "=== EXIT ==="), so infer it
            # from the events snapshot — never score a game that still has budget
            # headroom and momentum.
            if max_turns >= MAX_TURNS_CAP:
                log(f"{game} [{tag}] hit MAX_TURNS_CAP; giving up")
                break
            max_turns = min(MAX_TURNS_CAP, max_turns + TURN_GROW)
            log(f"{game} [{tag}] turn budget exhausted at {turns_done}; "
                f"growing to {max_turns}")
            continue
        log(f"{game} [{tag}] unknown stop; tail: {out[-250:]}")
        break
    res = score_game(wd)
    if res["state"] in ("NO_RUN", "SCORE_FAIL"):
        raise RuntimeError(
            f"{game}: no scorable run produced (state={res['state']}) — aborting "
            f"instead of recording a false 0.0 (env problem, not a game result)")
    log(f"{game} [{tag}] SCORED rhae={res['rhae']} state={res['state']} "
        f"levels={res['levels']}")
    return res


def _assert_lock_free() -> None:
    """Refuse to start if the harness live-lock is already held (orphaned runner)."""
    import fcntl
    lock_path = Path(tempfile.gettempdir()) / f"schema-harness-live-{os.getuid()}.lock"
    if not lock_path.exists():
        return
    fd = os.open(str(lock_path), os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        raise SystemExit(
            "ABORT: harness live-lock is held — an orphaned runner is active. "
            "Kill it (pkill -9 -f schema_harness.runner) before starting.")
    finally:
        os.close(fd)


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
    _assert_lock_free()
    # One game at a time, primary then its own <80 fallback. Two sequential passes
    # (all primaries, then all fallbacks) let a single non-terminating primary block
    # every other game's fallback — which is exactly how sp80 stopped tn36 (71.93) and
    # sc25 (61.44) from ever getting their Sol-max rerun.
    for game in games:
        g = ledger[phase].setdefault(game, {})
        if not g.get("primary_done"):
            log(f"--- {phase} PRIMARY {game} ---")
            try:
                g["primary"] = run_game(phase, game, cfg["model"],
                                        cfg["primary_effort"], cfg["provider"],
                                        "primary")
            except RuntimeError as exc:
                log(f"ABORT PHASE (primary {game}): {exc}")
                save_ledger(ledger)
                return
            g["primary_done"] = True
            save_ledger(ledger)
        else:
            log(f"{game} primary already done: {g['primary']['rhae']}%")

        if g["primary"]["rhae"] >= 80:
            g["final"] = g["primary"]
            save_ledger(ledger)
            continue

        if not g.get("fallback_done"):
            log(f"--- {phase} FALLBACK {game} (primary {g['primary']['rhae']}%) ---")
            try:
                g["fallback"] = run_game(phase, game, cfg["fallback_model"],
                                         cfg["fallback_effort"], cfg["provider"],
                                         "fallback")
            except RuntimeError as exc:
                log(f"ABORT PHASE (fallback {game}): {exc}")
                save_ledger(ledger)
                return
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
        if games is None and subset in GAMES:
            games = [subset]   # single-game invocation, e.g. `sweep.py sol wa30`
        if games is None:
            raise SystemExit(f"unknown subset {subset!r}; choose from "
                             f"{list(SUBSETS)} or a single game id")
        run_phase(phase, games)
