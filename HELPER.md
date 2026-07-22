# Help run the Schema ARC-AGI-3 sweep

This repo is an open reconstruction of the Schema harness
([schema-harness.github.io](https://schema-harness.github.io/)) that has frontier models
play ARC-AGI-3 like physicists. We are reproducing the blog's benchmark numbers on all 25
public games, and the bottleneck is **subscription token quota, not machines** — one
ChatGPT account finishes roughly 5–7 games per week. Every game you run is a real
datapoint in the release. The science lives in [REPRODUCE.md](REPRODUCE.md); this file is
just how to run games.

## You need

- **macOS** (required to *run* games: the deny-by-default agent sandbox is built on
  macOS `sandbox-exec` and fails closed elsewhere — Linux can verify/score traces but
  not play), [uv](https://docs.astral.sh/uv/), Node 22+
- A ChatGPT subscription (your own — higher tiers have much larger Codex quotas; one hard
  game can consume 50–150M tokens)
- A free ARC Prize API key (sign up at the ARC-AGI-3 site, three.arcprize.org)

## Setup (once, ~5 minutes)

```bash
git clone https://github.com/yudduy/schema && cd schema
uv sync
npm install -g @openai/codex@0.144.1      # EXACTLY this version — the runner refuses others
codex login                                # sign in with YOUR ChatGPT account
printf 'ARC_API_KEY=<your key>\n' > .env && chmod 600 .env
```

## Claim a game, run it, send it back

1. **Claim**: open a GitHub issue titled `claim: <game>` (so two people don't burn quota
   on the same game). Ask which games are open if unsure.
2. **Run** (one command — it does the whole blog protocol for that game: xhigh primary,
   automatic budget growth, Sol-max fallback if the primary scores <80, official scoring):

   ```bash
   ONLY_RESET_LEVELS=true caffeinate -is uv run python spikes/sweep.py sol <game>
   ```

   (`caffeinate -is` is macOS; on Linux just keep the machine awake.) Expect 2–8+ hours.
   Interrupting is safe: re-run the same command and it resumes exactly where it stopped.
   Flaky wifi is handled (backoff + a hang watchdog); quota exhaustion idles and
   auto-resumes when your window resets.
3. **Send back**: progress prints to `~/schema-sweep/progress.log`; workdirs live in
   `~/schema-sweep/`. Package and attach to your claim issue:

   ```bash
   tar -czf <game>.tgz -C ~/schema-sweep $(cd ~/schema-sweep && ls -d sol-*-<game>)
   ```

We verify centrally: `spikes/intake.py` re-scores your events.jsonl with the vendored
official scorer and records its SHA-256 in the release manifest, and your `run.json` pins
model/effort/CLI/catalog — so every returned result is auditable end-to-end.

## Rules (all load-bearing)

1. Never upgrade or change the codex CLI while a run exists (`0.144.1`, pinned).
2. One live game per machine (a lock enforces this — don't fight it).
3. Don't edit anything inside `~/schema-sweep/` workdirs.
4. Don't look up game solutions or hint the agent in any way — the eval's integrity is
   the whole point. The agent must discover mechanics itself.
5. `ONLY_RESET_LEVELS=true` always (the runner hard-fails without it; it preserves replay
   parity with the released traces).
