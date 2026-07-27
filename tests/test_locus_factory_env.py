from __future__ import annotations

from types import SimpleNamespace

import schema_harness.locus as locus


def test_locus_factory_invalid_timeout_values_fall_back_to_defaults(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("LOCUS_WORKDIR", str(tmp_path))
    monkeypatch.setenv("LOCUS_GAME", "factory-test")
    monkeypatch.setenv("LOCUS_TURN_ID", "turn-1")
    monkeypatch.setenv("LOCUS_PROCESS_TIMEOUT", "2m")
    monkeypatch.setenv("LOCUS_BFS_TIMEOUT", "2m")
    monkeypatch.setenv("LOCUS_BACKTEST_TIMEOUT", "2m")

    def fake_service(*_args, **kwargs):
        return SimpleNamespace(**kwargs)

    monkeypatch.setattr(locus, "LocusService", fake_service)
    monkeypatch.setattr(locus, "_SERVICE", None)

    service = locus._service()

    assert service.process_timeout == 30
    assert service.bfs_timeout == 60
    assert service.backtest_timeout == 120
