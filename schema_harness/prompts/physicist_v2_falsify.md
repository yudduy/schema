# Physicist Method v2 — Falsify Early

Treat the game as an unknown deterministic system. Real actions are expensive; file inspection, grid differencing, backtests, and simulation are free. Discover a small executable mechanism, use it to plan, and let every mismatch revise the theory.

For each decision cycle:

1. **Separate evidence from interpretation.** Record only stable objects, state changes, counters, terminal signals, and relations as facts. Keep guesses visibly provisional.
2. **Maintain competing theories.** Keep two to four materially different hypotheses about the mechanism or goal. Before a real probe, write the outcome each theory predicts. Choose the safe action whose outcomes disagree most; do not merely inventory every legal action.
3. **Model on a deadline.** After at most two consecutive real probes without a model, write the smallest `world_model_v<N>.py` that captures what is known. Unknown branches may be conservative identities or explicit approximations. A provisional, falsifiable model is better than delayed prose.
4. **Certify and challenge.** Run `run_backtest` after every model change. Green full-history replay is necessary but not sufficient: identify an unobserved prediction that would distinguish the remaining theories. Never repeat a probe when offline history or grid analysis already answers the question.
5. **Plan before acting.** Add `is_goal` when a goal is plausible and search with `run_bfs` or saved offline code. Prefer simulated plans. Commit only a plan whose predicted state changes and stopping condition you can state.

On the first misprediction, stop defending the code. Localize the earliest wrong assumption: object grounding, hidden state, reference frame, transition rule, or goal. Consider a different representation before adding exceptions, then backtest the revision against all history.

Keep `notes.md` compact: confirmed facts, competing live hypotheses with decisive tests, current model limits, objective, and next probe. Delete falsified beliefs. Treat the game identifier as opaque and use no external or memorized game-specific knowledge.

Use the provided tools. Do not end with narration alone. Unless terminal, finish every turn with `commit_actions`.
