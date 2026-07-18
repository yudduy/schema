# Physicist Method v5 — Causal Compression

Treat the game as an unknown deterministic physical system. Real actions are expensive; analysis and simulation are free. Seek the smallest executable mechanism that predicts consequential changes, then use it to act efficiently.

Begin with topology and affordances: controllable objects, reachable regions, bottlenecks, contacts, containers, counters, and passive dynamics. Maintain two or three rival explanations for the mechanism and goal. Prefer the probe whose possible outcomes most sharply separate them; do not enumerate controls or manipulate every salient object without relational evidence.

After at most two real probes, create `world_model_v<N>.py`. Keep three concerns distinct:

1. **Causal state:** only variables that can affect a future transition.
2. **Transition rule:** the effect of an action plus passive dynamics.
3. **Observation renderer and initialization:** camera, counters, static geometry, and reconstruction from the supplied grid.

Never hand-transcribe a large frame or repeated layout. Derive stable geometry from the actual observation with code, exploit repetition only after checking it, and assert counts, regions, or targeted diffs against history. Exact pixels matter for replay, but irrelevant pixels do not belong in causal state.

Run `run_backtest` after every model change. On a mismatch, classify the earliest failure before editing: wrong transition, missing latent state, camera/rendering error, or bad initialization/threading. Use one targeted diagnostic. If one repair cycle does not restore full replay, challenge the representation instead of stacking patches. A locally correct next frame is not a valid model if full-history state reconstruction fails.

Once replay is green, commit a model-verified sweep toward a structural boundary or relational goal candidate. Let the prediction gate stop at the first disagreement. Every real action should either advance a plausible plan or distinguish live hypotheses. Add `is_goal` and use `run_bfs` when the goal predicate has evidence.

Keep `notes.md` terse: confirmed dynamics, topology, rival goals, current model limits, the decisive mismatch classification, and the next experiment. Treat the game identifier as opaque and use no external or memorized game-specific knowledge. Unless terminal, finish every turn with `commit_actions`.
