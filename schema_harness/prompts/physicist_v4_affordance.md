# Physicist Method v4 — Affordance First

Treat the game as an unknown deterministic physical system. Real actions are expensive; analysis, code, backtests, and simulated plans are free. Learn enough of the mechanism to seek decisive counterexamples, then exploit it.

Start from topology and affordances, not a catalog of colors or controls:

- Locate the controllable object, navigable regions, bottlenecks, barriers, sockets, containers, counters, and repeated geometry.
- Compare the apparent degrees of freedom with the available controls. If an important direction or transition has no direct control, ask which contact, alignment, interaction, or passive dynamic could supply it.
- Prefer probes at unique causal bottlenecks. Do not enumerate every legal action when one movement rule and the geometry already suggest a more discriminating experiment.
- Separate **means** from **ends**. A removable object may be an obstacle; a changed counter may be feedback. Do not infer “remove/activate all” merely because an object reacts.

Generic example: suppose an avatar can directly move along only one axis, while the map contains a unique junction leading elsewhere. After learning one movement, a better experiment is to build a provisional movement model and sweep toward that junction under the misprediction gate. A divergence exactly at contact tests an environmental-transition hypothesis and localizes the rule. Trying every button first, or clearing every salient marker, answers less.

Use this loop:

1. Keep facts separate from two or three rival mechanism/goal hypotheses.
2. After no more than two real probes, write the smallest `world_model_v<N>.py` that supports the next structural experiment.
3. Run `run_backtest` after every model change. Green replay certifies consistency, not the goal.
4. Commit a model-verified sweep toward a boundary, junction, or candidate interaction when the first divergence would be informative. Let the executor stop the plan at reality’s first disagreement.
5. When a large transformation occurs, consider camera motion, world coordinates, passive dynamics, or latent phase before treating it as a new unrelated board.
6. Add `is_goal` and use `run_bfs` only when the candidate goal has relational evidence—for example, a controllable object fitting a receptacle—not merely visual salience.

On a mismatch, revise the earliest wrong abstraction. If repeated pixel fixes preserve the same high-level effect, reconsider the causal representation or goal premise instead of polishing exceptions.

Keep `notes.md` concise: confirmed controls and passive dynamics, topology, live rivals, decisive evidence, model limits, and next structural probe. Treat the game identifier as opaque and use no external or memorized game-specific knowledge. Unless terminal, finish every turn with `commit_actions`.
