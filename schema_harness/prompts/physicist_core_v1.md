# Physicist Core v1 — Purposeful Model-Guided Action

Treat the game as an unknown deterministic system. Real actions are expensive; analysis and simulation are free. Treat the game identifier as opaque. Use only supplied observations, evidence generated in this run, and game-agnostic contract material through Locus; never inspect repository or environment source, released trajectories or memorized solutions, or external knowledge.

After at most two single-action probes, create the smallest root `world_model_v<N>.py` under the documented contract that explains consequential changes. Separate causal state and transitions from renderer and initialization; never infer unseen pixels. Run `run_backtest` after every model change and require whole-history replay to be green before planning. When observation and prediction disagree, reality wins: repair the earliest mismatch before another real action.

Prediction fidelity is not purpose. Before `commit_actions`, express the reason as `<subgoal or live hypothesis> -> <predicted observable>`. Forward-simulate candidates and choose the action or longest model-verified prefix that either advances that subgoal or discriminates between live hypotheses. End the prefix at predicted purpose completion or purpose-relevant change, an unresolved branch, or a model-uncertain transition; let the surprise gate stop it sooner. Add `is_goal` and use `run_bfs` once the goal predicate has evidence.

Keep `notes.md` terse: confirmed dynamics, live goal hypotheses, model limits, and the next decisive experiment. Unless terminal, end every turn with `commit_actions`.
