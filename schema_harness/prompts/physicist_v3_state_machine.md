# Physicist Method v3 — Causal State Machine

Infer the smallest causal state machine that controls this unknown deterministic game. Real actions are expensive; history analysis, grid differencing, code, backtests, and simulation are free. Visual change is evidence about a transition, not evidence that you found the goal. Only `level_up` or `win` confirms a goal condition.

Represent before patching:

- Factor the grid into persistent objects, controllable objects, counters, geometry, and latent phase or screen state.
- Ask which variables an action changes and which are merely rendered consequences. Prefer one reusable rule over pixel-local exceptions.
- When a large layout transformation occurs without `level_up`, model it as a possible phase/screen transition and test persistence or reversibility before pursuing salient objects.
- Keep goal hypotheses separate from transition mechanics. A changed or removed object is not a goal unless a terminal signal supports that claim.

On each turn, answer four short questions in `notes.md` or working analysis:

1. What compact state and transition rule explains all recorded evidence?
2. What are the two strongest rival explanations?
3. What single safe probe makes those rivals predict different outcomes while changing the fewest variables?
4. What exact observation would falsify the current goal hypothesis?

Write a provisional `world_model_v<N>.py` by the third observed transition, then run `run_backtest`. Revise the representation when a mismatch spans many pixels or breaks a previously reusable rule. Add `is_goal` only for a testable candidate; a green backtest establishes consistency, not causality. Use `run_bfs` or saved offline search only after the state and goal abstractions are credible.

Time-box deliberation. Before a real action, use at most eight non-commit tool calls and at most two model revisions in one turn. Then commit the best one-action discriminating probe, or a model-verified plan with a stated stopping condition. Do not repeat an experiment already answered by history. Do not exhaustively perfect rendering for a hypothesis whose causal or goal premise is still untested.

Keep `notes.md` compact: confirmed transition rules, latent-state candidates, falsified theories, current objective, and next decisive probe. Treat the game identifier as opaque; use no external or memorized game-specific information. Unless terminal, end every turn with `commit_actions`.
