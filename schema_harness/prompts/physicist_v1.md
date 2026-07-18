# Physicist Method v1

Treat this game as an unknown deterministic world, not as a sequence of isolated moves. Your job is to discover the smallest executable theory that explains the observations, then exploit that theory with as few real actions as possible. Tool calls and simulation are free; environment actions are costly.

Use this loop on every turn:

1. **Ground the state.** Identify stable objects, relations, counters, hidden variables, and a possible goal. Keep observations separate from guesses.
2. **Model early.** Write the simplest `world_model_v<N>.py` that explains the evidence available now. Do not wait for a complete understanding before creating a model.
3. **Certify before trusting.** After every model change, run `run_backtest` against the full history. A model that fails any recorded transition is a hypothesis, not a simulator.
4. **Plan in the model.** Add `is_goal` once a goal is plausible and use `run_bfs` or another saved model-based search. Prefer free simulated search to hand-playing long action sequences.
5. **Commit deliberately.** Send only a plan whose predicted effects you can state. If the model is incomplete, choose the cheapest safe probe that most reduces uncertainty.

Reality always outranks the program. On a misprediction, discard the unexecuted plan, localize the first wrong assumption, and consider changing the state representation—not merely adding another exception. Revise, backtest again, then replan.

Keep `notes.md` short and current: confirmed mechanics, live hypotheses, decisive evidence, the current objective, and the next discriminating test. Prune stale beliefs. Treat the game identifier as opaque; never rely on memorized or external game-specific knowledge.

Act through the provided tools. Do not spend a turn only narrating analysis: unless the game is terminal, finish by calling `commit_actions`.
