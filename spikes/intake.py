#!/usr/bin/env python3
"""Verify and score a helper-returned game workdir with the vendored scorer.

Usage:
    uv run python spikes/intake.py <workdir>                # verify + score + manifest line
    uv run python spikes/intake.py <workdir> --merge sol    # also record into the ledger

Checks the driver pins recorded in run.json, scores events.jsonl, and prints a
manifest line with the events SHA-256 so the result is auditable end-to-end.
--merge refuses to overwrite an existing equal-or-better ledger entry.
"""
import hashlib
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from schema_harness.game_identity import short_game_id  # noqa: E402
from sweep import score_game, load_ledger, save_ledger  # noqa: E402
from replay_parity import verify_events  # noqa: E402

EXPECTED_CLI = "codex-cli 0.144.1"


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    wd = Path(sys.argv[1]).resolve()
    merge_phase = None
    if len(sys.argv) > 2:
        if sys.argv[2] == "--merge" and len(sys.argv) > 3:
            merge_phase = sys.argv[3]
        else:
            raise SystemExit(__doc__)

    ev, rj = wd / "events.jsonl", wd / "run.json"
    if not ev.exists() or not rj.exists():
        raise SystemExit(f"REJECT: {wd} is missing events.jsonl or run.json")

    run = json.loads(rj.read_text())
    driver = run.get("driver", {})
    # Bind the replayed game to the SAME identifier the scorer baselines RHAE
    # against (run.json's game_id, short form) — never the workdir name. Otherwise a
    # genuine easy-game trace, relabeled to a harder game_id, would replay green while
    # being scored against the harder baseline (a maxed score for a game never played).
    game_id = str(run.get("game_id") or "")
    if not game_id:
        raise SystemExit(f"REJECT: {wd} run.json has no game_id — cannot bind the "
                         f"replay engine to the scorer's baseline")
    try:
        game = short_game_id(game_id)
    except ValueError as exc:
        raise SystemExit(f"REJECT: {wd} {exc}") from None
    warns = []
    if driver.get("cli_version") and driver["cli_version"] != EXPECTED_CLI:
        warns.append(f"cli_version={driver['cli_version']!r} != {EXPECTED_CLI!r}")
    res = score_game(wd)
    if res["state"] in ("NO_RUN", "SCORE_FAIL"):
        raise SystemExit(f"REJECT: scorer failed on {wd}: {res}")

    # The scorer trusts the events file's self-reported levels/actions. Re-execute
    # the trajectory on the ground-truth engine before trusting that score, so a
    # fabricated or edited trace cannot enter the ledger.
    # Replay against the FULL versioned id: arc.make() honours the version, so a
    # trace recorded on a different build of the game cannot verify against the
    # one currently installed.
    verdict = verify_events(ev, game_id)
    if not verdict.green:
        raise SystemExit(f"REJECT: {wd} failed replay verification — {verdict.reason()}")

    sha = hashlib.sha256(ev.read_bytes()).hexdigest()
    print(f"game={game} rhae={res['rhae']} state={res['state']} levels={res['levels']}")
    print(f"replay_verified=GREEN steps={verdict.steps_replayed}/{verdict.total_actions} "
          f"final=({verdict.final_levels},{verdict.final_state})")
    print(f"model={run.get('model')} effort={run.get('effort')} "
          f"cli={driver.get('cli_version')} catalog={str(driver.get('model_catalog_sha256'))[:12]}")
    print(f"events_sha256={sha}")
    for w in warns:
        print(f"WARN: {w}")

    if merge_phase:
        ledger = load_ledger()
        g = ledger.setdefault(merge_phase, {}).setdefault(game, {})
        prev = (g.get("final") or {}).get("rhae", -1)
        if prev >= res["rhae"]:
            raise SystemExit(f"SKIP MERGE: existing {prev} >= incoming {res['rhae']}")
        rec = dict(res, workdir=str(wd), source="intake", events_sha256=sha,
                   replay_verified=True)
        g["primary"] = rec
        g["primary_done"] = True
        if res["rhae"] >= 80:
            g["final"] = rec
        save_ledger(ledger)
        print(f"MERGED into ledger[{merge_phase}][{game}]"
              + ("" if res["rhae"] >= 80 else " (final pending fallback)"))


if __name__ == "__main__":
    main()
