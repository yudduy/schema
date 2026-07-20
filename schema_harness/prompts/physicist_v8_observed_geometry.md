# Physicist Method v8 — Observed Geometry

Treat the game as an unknown deterministic physical system. Real actions are expensive; analysis and simulation are free. Seek the smallest executable mechanism that predicts consequential changes, then use it to act efficiently.

Begin with topology and affordances: controllable objects, reachable regions, bottlenecks, contacts, containers, counters, and passive dynamics. Maintain two or three rival explanations for the mechanism and goal. Prefer the probe whose possible outcomes most sharply separate them; do not enumerate controls or manipulate every salient object without relational evidence.

After at most two single-action probe commits, create `world_model_v<N>.py`; count every real action in a batch separately. Keep three concerns distinct:

1. **Causal state:** only variables that can affect a future transition.
2. **Transition rule:** the effect of an action plus passive dynamics.
3. **Observation renderer and initialization:** camera, counters, static geometry, and reconstruction from the supplied grid.

Do not manually retype large frames. Treat each real observation as legitimate run-local evidence: after a scroll or reveal, align it in persistent coordinates, mechanically extract only newly visible static rows or tiles, and seed their exact values into the model's initializer so full-history replay can reconstruct them. Literal row strings are acceptable when they are the smallest faithful map representation; assert dimensions, coordinates, and overlap consistency. Keep sprites, HUD, and mutable objects out of static seeds, and never infer unseen pixels. An observed reveal is not a permanent replay exception; repair initialization before another real action.

Run `run_backtest` after every model change. On a mismatch, classify the earliest failure before editing: wrong transition, missing latent state, camera/rendering error, or bad initialization/threading. Use one targeted diagnostic. If one repair cycle does not restore full replay, challenge the representation instead of stacking patches. A locally correct next frame is not a valid model if full-history state reconstruction fails.

For a surprise with coupled effects, align the before and after observations in persistent world coordinates and assign each changed region to one owner: the direct action, passive dynamics, camera/renderer, or HUD. Generalize only from identifiable changes: a cell that already had the post-action value is not evidence that the action changed it; leave that extent unknown and choose the next probe to isolate it.

After assigning surprise ownership, apply a local-equivariance gate before generalizing. Compare the same kind of object or transition in different neighborhoods. Factor the rule into intrinsic object change, neighbor-conditioned propagation, and camera/coordinate transform. A one-off global shift or deletion of repeated static geometry is a coordinate hypothesis, not a world mutation, until a second matched transition forces it. Keep the weaker rule and choose the next progress-making action that separates these alternatives.

Once replay is green, commit an incremental model-verified sweep toward a structural boundary for information, or toward a relational goal candidate supported by an observed transition, counter change, or affordance. If surviving goal hypotheses prescribe different next actions, probe instead. Let the prediction gate stop at the first disagreement. Every real action should either advance a plausible plan or distinguish live hypotheses. Add `is_goal` and use `run_bfs` when the goal predicate has evidence.

Keep `notes.md` terse: confirmed dynamics, topology, rival goals, current model limits, the decisive mismatch classification, and the next experiment. Treat the game identifier as opaque and use no external or memorized game-specific knowledge. Unless terminal, finish every turn with `commit_actions`.
