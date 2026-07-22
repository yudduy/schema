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
    for game, rec in sorted(games.items()):
        final = rec.get("final")
        if not final:
            continue
        wd = Path(final["workdir"])
        ev, rj = wd / "events.jsonl", wd / "run.json"
        if not ev.exists() or not rj.exists():
            manifest["games"][game] = {"error": f"missing trace files at {wd}"}
            continue
        dst = out_traces / game
        dst.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ev, dst / "events.jsonl")
        shutil.copy2(rj, dst / "run.json")
        run = json.loads(rj.read_text())
        driver = run.get("driver", {})
        manifest["games"][game] = {
            "rhae": final["rhae"], "state": final["state"], "levels": final["levels"],
            "model": run.get("model"), "effort": run.get("effort"),
            "system_prompt": run.get("system_prompt"),
            "cli_version": driver.get("cli_version"),
            "model_catalog_sha256": driver.get("model_catalog_sha256"),
            "events_sha256": sha256(ev),
            "primary_rhae": rec.get("primary", {}).get("rhae"),
            "used_fallback": "fallback" in rec and rec["final"] is rec.get("fallback"),
        }
        scored.append(final["rhae"])

    manifest["n_games_scored"] = len(scored)
    manifest["benchmark_mean_rhae_partial"] = round(sum(scored) / len(scored), 2) if scored else 0.0
    (RELEASE / "MANIFEST.json").write_text(json.dumps(manifest, indent=2))

    print(f"exported {len(scored)} scored games -> {RELEASE}")
    print(f"{'game':6} {'RHAE':>7} {'state':10} {'levels':8} {'model':14}")
    for g, m in sorted(manifest["games"].items()):
        if "rhae" in m:
            print(f"{g:6} {m['rhae']:>6.2f}% {m['state']:10} {m['levels']:8} {m['model']:14}")
    print(f"partial benchmark mean ({len(scored)} games): "
          f"{manifest['benchmark_mean_rhae_partial']}%")


if __name__ == "__main__":
    main()
