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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sweep import score_game, load_ledger, save_ledger  # noqa: E402

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
    game = run.get("game") or wd.name.rsplit("-", 1)[-1]
    warns = []
    if driver.get("cli_version") and driver["cli_version"] != EXPECTED_CLI:
        warns.append(f"cli_version={driver['cli_version']!r} != {EXPECTED_CLI!r}")
    res = score_game(wd)
    if res["state"] in ("NO_RUN", "SCORE_FAIL"):
        raise SystemExit(f"REJECT: scorer failed on {wd}: {res}")

    sha = hashlib.sha256(ev.read_bytes()).hexdigest()
    print(f"game={game} rhae={res['rhae']} state={res['state']} levels={res['levels']}")
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
        rec = dict(res, workdir=str(wd), source="intake", events_sha256=sha)
        g["primary"] = rec
        g["primary_done"] = True
        if res["rhae"] >= 80:
            g["final"] = rec
        save_ledger(ledger)
        print(f"MERGED into ledger[{merge_phase}][{game}]"
              + ("" if res["rhae"] >= 80 else " (final pending fallback)"))


if __name__ == "__main__":
    main()
