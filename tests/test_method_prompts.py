from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
V10_PROMPT = (
    REPO_ROOT / "schema_harness" / "prompts" / "physicist_v10_grounded_rollout.md"
)
V11_PROMPT = (
    REPO_ROOT / "schema_harness" / "prompts" / "physicist_v11_inventory_consensus.md"
)
V12_PROMPT = (
    REPO_ROOT / "schema_harness" / "prompts" / "physicist_v12_applicability_certificate.md"
)
V13_PROMPT = (
    REPO_ROOT / "schema_harness" / "prompts" / "physicist_v13_renderer_provenance.md"
)
V14_PROMPT = (
    REPO_ROOT / "schema_harness" / "prompts" / "physicist_v14_renderer_precedence.md"
)


def test_v10_grounds_actual_and_predicted_frames_without_losing_v8_seeding():
    prompt = V10_PROMPT.read_text(encoding="utf-8")

    assert "`runtime/gateway_state.json`" in prompt
    assert '`read_history(indices=[...], detail="full")`' in prompt
    assert "never transcribe frames, rows, or cells by hand" in prompt
    assert "require an executable certificate for the exact suffix" in prompt
    assert "If `init_state(current_grid)` is sufficient" in prompt
    assert "`load_model`, `set_current_level`, `call_init_state`, and `call_predict`" in prompt
    assert "thread every normalized grid and state through the planned actions" in prompt
    assert "inspect `level_up`, `dead`, and `win`" in prompt
    assert "stop at the first terminal flag" in prompt
    assert "batch only an exact plan returned by gateway-aligned `run_bfs`" in prompt
    assert "If the model needs history-dependent latent state" in prompt
    assert "commit only the verified prefix" in prompt
    assert "otherwise commit one action" in prompt

    assert "mechanically extract only newly visible static rows or tiles" in prompt
    assert "seed their exact values into the model's initializer" in prompt
    assert "assert dimensions, coordinates, and overlap consistency" in prompt
    assert "Keep sprites, HUD, and mutable objects out of static seeds" in prompt
    assert "never infer unseen pixels" in prompt

    for inherited_rule in (
        "After at most two single-action probe commits",
        "challenge the representation instead of stacking patches",
        "preserve every unaffected rule and refine only the owning typed rule",
        "Transfer the refined rule to an unqueried object or control of the same type",
        "apply a local-equivariance gate before generalizing",
        "Every real action should either advance a plausible plan or distinguish live hypotheses",
    ):
        assert inherited_rule in prompt


def test_v11_certifies_initial_inventory_and_candidate_consensus():
    prompt = V11_PROMPT.read_text(encoding="utf-8")

    for initialization_gate in (
        "produce an executable initialization certificate",
        "through two independent paths",
        "expected canonical causal-inventory signature",
        "replaying the current level from the actual level-entry observation through its history",
        "write its final current-level signature under `runtime/model_scratch/`",
        "run the full backtest",
        "use `run_python` to read that JSON and the structured gateway state",
        '`load_model`, `set_current_level(model, gateway["level"])`, and `call_init_state(model, gateway["grid"])`',
        "asserting equality",
        "Do not rely on suppressed model-worker stdout or a prose comparison",
        "history-supported, transition-relevant facts",
        "identifiable object counts and types or roles",
        "persistent identities or owner memberships",
        "Ignore collection order and renderer caches",
        "Known persistent objects remain inventory invariants while temporarily occluded",
        "never-established, legitimately spawned, or still-unseen objects",
        "predict the planned suffix differently",
        "current-observation initialization is insufficient",
    ):
        assert initialization_gate in prompt

    for consensus_gate in (
        "every still-live, materially plausible, replay-consistent, decision-relevant candidate executable",
        "evidence may leave only one",
        "Record the replay failure or observed counterexample before retiring a rival",
        "each candidate can independently reconstruct the current observation",
        "Every non-final batched action requires version-space agreement",
        "exact normalized next grid",
        "transition-relevant inventory and identity effects",
        "`level_up`, `dead`, and `win`",
        "only as the final probe",
        "no queued suffix may depend on choosing one unresolved candidate",
        "do not discard a rival merely to manufacture agreement",
        "requires unavailable history-dependent state, commit one action",
    ):
        assert consensus_gate in prompt

    assert "never drop a live candidate merely to meet that count" in prompt
    assert "stateless initialization" not in prompt
    assert "use gateway-aligned state" not in prompt

    for inherited_v10_rule in (
        "`runtime/gateway_state.json`",
        "require an executable certificate for the exact suffix",
        "`load_model`, `set_current_level`, `call_init_state`, and `call_predict`",
        "batch only an exact plan returned by gateway-aligned `run_bfs`",
        "mechanically extract only newly visible static rows or tiles",
        "Keep sprites, HUD, and mutable objects out of static seeds",
    ):
        assert inherited_v10_rule in prompt


def test_v12_certifies_action_applicability_separately_from_effects():
    prompt = V12_PROMPT.read_text(encoding="utf-8")

    for applicability_gate in (
        "Model action applicability or preconditions separately from conditional effects",
        "syntactically legal action or in-bounds target",
        "evidence-backed applicability domain",
        "action or control type, local pre-state or target class, and target footprint or geometry",
        "mechanically classify its target and full footprint from the structured current grid",
        "Each candidate used to certify a multi-action suffix",
        "`check_applicability(state, grid, action, x, y) -> True | False | None`",
        "equivalent executable adapter over explicit transition guards",
        "`run_python` must assert `True` before `call_predict` for every non-final batched action",
        "a default identity fallthrough is not a certified no-effect successor",
        "applicability is true and explicit conditional-effect logic predicts it",
        "same intended progress or goal predicate and equivalent transition-relevant state",
        "total real actions to that horizon across every plausible applicability and effect branch",
        "including the probe, observation and replan, and recovery after a no-op, HUD-only outcome, death, or displacement",
        "Choose the supported plan when its worst-case count is no greater",
        "Information gain or a HUD-only change is not equivalent progress",
        "record the action-cost and information tradeoff",
        "stop the certified prefix before that action",
        "only as the final action after a unanimously verified prefix, with no suffix",
        "alone when the prefix is empty",
        "Call it a discriminator only when executable rivals predict explicit distinct outcomes",
    ):
        assert applicability_gate in prompt

    for noop_gate in (
        "mark every complete rendered-successor prediction that predicted a conflicting spatial world-grid change as mismatched in this context",
        "do not equate no observed effect with inapplicability or globally discard an effect rule supported elsewhere",
        "unmet precondition or applicability constraint",
        "false conditional-effect antecedent",
        "observation aliasing, hidden change, or renderer error",
        "only that no spatial world-grid effect was observed",
        "the counter change remains transition evidence",
        "neither observation by itself identifies which explanation is true",
    ):
        assert noop_gate in prompt

    assert "eliminate candidates that predicted" not in prompt

    for inherited_v11_rule in (
        "produce an executable initialization certificate through two independent paths",
        "write its final current-level signature under `runtime/model_scratch/`",
        "every still-live, materially plausible, replay-consistent, decision-relevant candidate executable",
        "Every non-final batched action requires version-space agreement",
        "requires unavailable history-dependent state, commit one action",
        "require an executable certificate for the exact suffix",
    ):
        assert inherited_v11_rule in prompt


def test_v13_certifies_renderer_provenance_before_future_unocclusion():
    prompt = V13_PROMPT.read_text(encoding="utf-8")

    for provenance_gate in (
        "ordered composite of persistent base or world layers and transient renderer layers",
        "produce an executable renderer-provenance certificate",
        "every cell whose topmost transient layer can change anywhere in the planned rollout",
        "Starting from the structured level-entry observation",
        "mechanically compute the cover, uncover, and overwrite set at every step",
        "Track the persistent base separately from transient layers",
        "exact previously unoccluded observation or an executable history-supported invariant",
        "Never fill an occluded base cell from a default or visual guess; use only a prior unoccluded observation or an executable history-supported invariant",
        "never let drawing or erasing a transient layer mutate the persistent base",
        '`check_renderer_provenance(state, grid, action, x, y) -> {"revealed": ..., "unknown_base": ...}`',
        "equivalent executable adapter over its renderer",
        "on the same threaded pre-state",
        "Before accepting every non-final predicted successor",
        "`run_python` must assert `unknown_base == []`",
        "verify the predicted values at every revealed cell",
        "canonical renderer fact that can change a later suffix prediction",
        "candidates need not use identical internal cache representations",
        "stop the certified prefix before that action",
        "final isolated probe, with no dependent suffix",
        "Full replay does not certify a future unocclusion that history never exercised",
        "repair only the owning renderer or initialization rule",
        "without discarding a still-correct causal transition",
    ):
        assert provenance_gate in prompt

    for inherited_v12_rule in (
        "Model action applicability or preconditions separately from conditional effects",
        "`check_applicability(state, grid, action, x, y) -> True | False | None`",
        "Every non-final batched action requires version-space agreement",
        "produce an executable initialization certificate through two independent paths",
        "require an executable certificate for the exact suffix",
        "Keep sprites, HUD, and mutable objects out of static seeds",
    ):
        assert inherited_v12_rule in prompt


def test_v14_certifies_contextual_renderer_precedence_before_batching():
    prompt = V14_PROMPT.read_text(encoding="utf-8")

    for precedence_gate in (
        "evidence-backed, possibly conditional renderer rule",
        "not as a universal stack",
        "two or more active layers propose different normalized values",
        '`check_renderer_precedence(state, grid, action, x, y) -> {"contenders": ..., "winner": ..., "support": ..., "unknown_precedence": ...}`',
        "matched overlap of the same layer roles or types and relevant local conditions",
        "`support` must identify the matched history transition, cell, roles, and relevant conditions or the executable invariant used",
        "independently re-evaluate every witness from structured history or the invariant",
        "every distinguishable overlap appears in exactly one of the mechanically supported or `unknown_precedence` sets",
        "every supported winner has a verified witness",
        "An unsupported winner prediction must populate `unknown_precedence`",
        "the predicted winner alone is never evidence",
        "does not license a total order, transitivity",
        "transfer across occupancy, masks, action phase, or other untested conditions",
        "partial, contextual relations",
        "`run_python` must assert `unknown_precedence == []`",
        "exact normalized next-grid value",
        "observationally irrelevant for that suffix",
        "final isolated probe",
        "no dependent suffix",
        "alone when the prefix is empty",
        "Isolation relaxes only pre-action consensus",
        "compare the complete normalized successor exactly against every candidate's pre-recorded complete prediction",
        "mark every mismatch",
    ):
        assert precedence_gate in prompt

    for inherited_v13_rule in (
        "produce an executable renderer-provenance certificate",
        '`check_renderer_provenance(state, grid, action, x, y) -> {"revealed": ..., "unknown_base": ...}`',
        "`run_python` must assert `unknown_base == []`",
        "Every non-final batched action requires version-space agreement",
        "exact normalized next grid",
        "require an executable certificate for the exact suffix",
    ):
        assert inherited_v13_rule in prompt

    for overfit_fragment in ("r11l", "(57,55)", "socket arm"):
        assert overfit_fragment not in prompt
