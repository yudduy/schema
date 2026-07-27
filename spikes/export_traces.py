#!/usr/bin/env python3
"""Assemble a verifiable, HF-style evidence package from the sweep ledger.

Idempotent — run anytime while the sweep is in flight; it exports every game
that has a scored result so far, with SHA-256 provenance and a manifest. Layout
mirrors the released schema-harness dataset so the vendored scorer accepts it
unchanged.

    ~/schema-sweep/release/
        MANIFEST.json                       # per-game metadata + aggregate mean
        traces/<phase>/<game>/events.jsonl   # the official-schema trajectory
        traces/<phase>/<game>/run.json       # config + pinned driver metadata

Usage:  uv run python spikes/export_traces.py [phase]     (default: sol)
"""
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ROOT = Path.home() / "schema-sweep"
RELEASE = ROOT / "release"

sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from schema_harness.game_identity import short_game_id  # noqa: E402
from replay_parity import verify_events  # noqa: E402


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    h.update(p.read_bytes())
    return h.hexdigest()


def harness_sha() -> str:
    r = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"],
                       capture_output=True, text=True)
    return r.stdout.strip() or "unknown"


def main() -> None:
    phase = sys.argv[1] if len(sys.argv) > 1 else "sol"
    ledger = json.loads((ROOT / "ledger.json").read_text())
    games = ledger.get(phase, {})
    out_traces = RELEASE / "traces" / phase
    out_traces.mkdir(parents=True, exist_ok=True)

    manifest = {"phase": phase, "harness_git_sha": harness_sha(), "games": {}}
    scored = []
    unverified = []
    for game, rec in sorted(games.items()):
        final = rec.get("final")
        if not final:
            continue
        wd = Path(final["workdir"])
        ev, rj = wd / "events.jsonl", wd / "run.json"
        if not ev.exists() or not rj.exists():
            manifest["games"][game] = {"error": f"missing trace files at {wd}"}
            continue
        run = json.loads(rj.read_text())
        driver = run.get("driver", {})
        # Re-execute on the ground-truth engine BEFORE publishing anything, and bind
        # the replay to the SAME identifier the scorer baselines RHAE against
        # (run.json's game_id) — cross-checking the ledger key so a relabeled trace
        # cannot be scored against a different game's baseline. A trace that does not
        # reproduce byte-for-byte is quarantined: kept out of the release bundle and
        # the mean, recorded only as a rejected claim.
        game_id = str(run.get("game_id") or "")
        try:
            verify_game = short_game_id(game_id)
        except ValueError as exc:
            verdict = None
            reason = str(exc)
        else:
            # Cheap identity check first — never spend a full trajectory replay (and
            # never report mismatch counts measured against the wrong engine) on a
            # relabeled trace.
            if verify_game != game:
                verdict = None
                reason = (
                    f"ledger key {game!r} != trace game_id {game_id!r} "
                    f"(short {verify_game!r})"
                )
            else:
                verdict = verify_events(ev, game_id)  # full id binds the build
                reason = None if verdict.green else verdict.reason()
        if reason is not None:
            manifest["games"][game] = {
                "replay_verified": False,
                "replay_grid_mismatches": verdict.grid_mismatches if verdict else -1,
                "replay_error": reason,
                "claimed_rhae": final["rhae"],
            }
            unverified.append(game)
            # Export is re-runnable: a game green in an earlier run may be red now.
            # Publishing "only verified traces" means removing the stale copy too.
            for stale in (out_traces / game, out_traces / f"{game}-primary"):
                if stale.exists():
                    shutil.rmtree(stale, ignore_errors=True)
                    print(f"  removed previously published {stale.name} from the bundle")
            print(f"WARNING: {game} did NOT replay-verify — QUARANTINED (not published): {reason}")
            continue

        dst = out_traces / game
        dst.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ev, dst / "events.jsonl")
        shutil.copy2(rj, dst / "run.json")
        manifest["games"][game] = {
            "rhae": final["rhae"], "state": final["state"], "levels": final["levels"],
            "model": run.get("model"), "effort": run.get("effort"),
            "system_prompt": run.get("system_prompt"),
            "cli_version": driver.get("cli_version"),
            "model_catalog_sha256": driver.get("model_catalog_sha256"),
            "events_sha256": sha256(ev),
            "primary_rhae": rec.get("primary", {}).get("rhae"),
            "used_fallback": "fallback" in rec and rec["final"] is rec.get("fallback"),
            "replay_verified": True,
            "replay_steps": verdict.steps_replayed,
            "replay_grid_mismatches": verdict.grid_mismatches,
        }
        # Transparency the original release lacks: when the retained run is the
        # fallback, also publish the failed primary's trace.
        prim = rec.get("primary") or {}
        pwd = Path(prim.get("workdir", ""))
        if prim and str(pwd) != str(wd) and (pwd / "events.jsonl").exists():
            pdst = out_traces / f"{game}-primary"
            pdst.mkdir(parents=True, exist_ok=True)
            shutil.copy2(pwd / "events.jsonl", pdst / "events.jsonl")
            if (pwd / "run.json").exists():
                shutil.copy2(pwd / "run.json", pdst / "run.json")
            manifest["games"][game]["primary_events_sha256"] = sha256(pwd / "events.jsonl")
        scored.append(final["rhae"])

    manifest["n_games_scored"] = len(scored)
    manifest["n_unverified"] = len(unverified)
    manifest["unverified_games"] = unverified
    manifest["benchmark_mean_rhae_partial"] = round(sum(scored) / len(scored), 2) if scored else 0.0
    (RELEASE / "MANIFEST.json").write_text(json.dumps(manifest, indent=2))

    print(f"exported {len(scored)} verified games -> {RELEASE}")
    print(f"{'game':6} {'RHAE':>7} {'state':10} {'levels':8} {'replay':7} {'model':14}")
    for g, m in sorted(manifest["games"].items()):
        if "rhae" in m:
            mark = "GREEN" if m.get("replay_verified") else "RED"
            print(f"{g:6} {m['rhae']:>6.2f}% {m['state']:10} {m['levels']:8} {mark:7} {m['model']:14}")
    print(f"partial benchmark mean ({len(scored)} verified games): "
          f"{manifest['benchmark_mean_rhae_partial']}%")
    if unverified:
        print(f"UNVERIFIED (excluded): {', '.join(unverified)}")


if __name__ == "__main__":
    main()
