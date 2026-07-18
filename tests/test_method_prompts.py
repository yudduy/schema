from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
V10_PROMPT = (
    REPO_ROOT / "schema_harness" / "prompts" / "physicist_v10_grounded_rollout.md"
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
