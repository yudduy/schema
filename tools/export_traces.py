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

Usage:  uv run python tools/export_traces.py [phase]     (default: sol)
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
from sweep import CLEAN, CONTAMINATED  # noqa: E402
from verify import verify_events  # noqa: E402


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    h.update(p.read_bytes())
    return h.hexdigest()


def harness_sha() -> str:
    r = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"],
                       capture_output=True, text=True)
    return r.stdout.strip() or "unknown"


def retained_run_tag(rec: dict, final: dict) -> str:
    """Return the durable sweep tag for a ledger result loaded from JSON."""
    fallback = rec.get("fallback")
    return "fallback" if fallback and final == fallback else "primary"


def resolve_trace_workdir(
    phase: str, game: str, rec: dict, final: dict,
) -> tuple[Path | None, list[Path]]:
    """Find a complete retained trace, preferring the ledger-recorded location."""
    candidates = []
    recorded = final.get("workdir")
    if recorded:
        candidates.append(Path(recorded))
    canonical = ROOT / f"{phase}-{retained_run_tag(rec, final)}-{game}"
    if canonical not in candidates:
        candidates.append(canonical)
    for workdir in candidates:
        if (workdir / "events.jsonl").is_file() and (workdir / "run.json").is_file():
            return workdir, candidates
    return None, candidates


def rhae_group(scores: dict[str, float], game_ids: list[str]) -> dict:
    included = [game for game in game_ids if game in scores]
    mean = (
        round(sum(scores[game] for game in included) / len(included), 2)
        if included else None
    )
    return {
        "mean_rhae_partial": mean,
        "n_games_included": len(included),
        "n_games_total": len(game_ids),
        "game_ids_included": included,
    }


def print_rhae_group(label: str, summary: dict, note: str = "") -> None:
    mean = summary["mean_rhae_partial"]
    mean_text = "N/A" if mean is None else f"{mean:.2f}%"
    ids = ", ".join(summary["game_ids_included"]) or "(none)"
    print(
        f"{label} mean RHAE "
        f"({summary['n_games_included']}/{summary['n_games_total']} verified "
        f"games included{note}; ids: {ids}): {mean_text}"
    )


def main() -> None:
    phase = sys.argv[1] if len(sys.argv) > 1 else "sol"
    ledger = json.loads((ROOT / "ledger.json").read_text())
    games = ledger.get(phase, {})

    resolved_workdirs = {}
    missing_traces = []
    for game, rec in sorted(games.items()):
        final = rec.get("final")
        if not final:
            continue
        workdir, candidates = resolve_trace_workdir(phase, game, rec, final)
        if workdir is None:
            missing_traces.append((game, candidates))
        else:
            resolved_workdirs[game] = workdir
    if missing_traces:
        for game, candidates in missing_traces:
            checked = ", ".join(str(path) for path in candidates)
            print(
                f"ERROR: scored game {game} has no complete trace; checked: "
                f"{checked}",
                file=sys.stderr,
            )
        print(
            "ERROR: refusing to build a bundle that omits scored games",
            file=sys.stderr,
        )
        raise SystemExit(1)

    out_traces = RELEASE / "traces" / phase
    out_traces.mkdir(parents=True, exist_ok=True)

    manifest = {"phase": phase, "harness_git_sha": harness_sha(), "games": {}}
    scores = {}
    unverified = []
    for game, rec in sorted(games.items()):
        final = rec.get("final")
        if not final:
            continue
        wd = resolved_workdirs[game]
        ev, rj = wd / "events.jsonl", wd / "run.json"
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
                "state": final.get("state"),
                "levels": final.get("levels"),
                "model": run.get("model"),
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
            "used_fallback": retained_run_tag(rec, final) == "fallback",
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
        scores[game] = final["rhae"]

    manifest["n_games_scored"] = len(scores)
    manifest["n_unverified"] = len(unverified)
    manifest["unverified_games"] = unverified
    clean_summary = rhae_group(scores, CLEAN)
    contaminated_summary = rhae_group(scores, CONTAMINATED)
    mixed_summary = rhae_group(scores, CLEAN + CONTAMINATED)
    mixed_summary["mixes_clean_and_contaminated"] = True
    manifest["rhae_summary"] = {
        "clean": clean_summary,
        "contaminated": contaminated_summary,
        "all_games_mixed": mixed_summary,
    }
    (RELEASE / "MANIFEST.json").write_text(json.dumps(manifest, indent=2))

    print(f"exported {len(scores)} verified games -> {RELEASE}")
    print(f"{'game':6} {'RHAE':>7} {'state':10} {'levels':8} {'replay':7} {'model':14}")
    for g, m in sorted(manifest["games"].items()):
        if m.get("replay_verified") is True:
            rhae, mark = m["rhae"], "GREEN"
        elif m.get("replay_verified") is False:
            rhae, mark = m["claimed_rhae"], "RED"
        else:
            continue
        state = str(m.get("state") or "-")
        levels = str(m.get("levels") or "-")
        model = str(m.get("model") or "-")
        print(
            f"{g:6} {rhae:>6.2f}% {state:10} {levels:8} "
            f"{mark:7} {model:14}"
        )
    if unverified:
        print(
            "RED rows show claimed RHAE and are excluded from the bundle "
            "and all means."
        )
    print_rhae_group("clean-set", clean_summary)
    print_rhae_group("contaminated-set", contaminated_summary)
    print_rhae_group(
        "all-games mixed",
        mixed_summary,
        note="; mixes clean + contaminated",
    )
    if unverified:
        print(f"UNVERIFIED (excluded): {', '.join(unverified)}")


if __name__ == "__main__":
    main()
