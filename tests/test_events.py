from __future__ import annotations

import json

import pytest

import schema_harness.events as events_module
from schema_harness.events import (
    ActionTaken,
    EventLog,
    ModelMispredicted,
    RunFinished,
    RunStarted,
    TextDelta,
    ToolFinished,
    ToolStarted,
    TurnCommitted,
    TurnFallback,
    TurnStarted,
)


def test_event_object_reader_parity(tmp_path):
    path = tmp_path / "events.jsonl"
    cases = [
        (
            '\n{"kind":"first"}\n \n{"kind":"second","value":2}\n',
            [{"kind": "first"}, {"kind": "second", "value": 2}],
            None,
        ),
        ('{"kind":"first"}\nnot-json\n{"kind":"third"}\n', None, "2: invalid JSON"),
        ('{"kind":"first"}\n{\n', None, "2: invalid JSON"),
        ('{"kind":"first"}\n42\n', None, "2: event must be an object"),
    ]

    for raw, expected, error_suffix in cases:
        path.write_text(raw, encoding="utf-8")
        try:
            outcome = (
                "value",
                [event for _, event in events_module.iter_json_objects(path)],
            )
        except ValueError as exc:
            outcome = ("error", str(exc))

        if error_suffix is None:
            assert outcome == ("value", expected)
        else:
            assert outcome == ("error", f"{path}:{error_suffix}")


def test_event_log_emits_released_schema_monotonic_seq_and_fsync(tmp_path, monkeypatch):
    timestamps = iter(float(value) for value in range(10, 20))
    fsynced: list[int] = []
    monkeypatch.setattr(events_module.os, "fsync", fsynced.append)
    grid = [[0, 1], [2, 3]]
    records = [
        RunStarted(
            game_id="bp35-0a0ad940",
            provider="test",
            model="none",
            max_actions=3000,
            win_levels=0,
            workdir="/tmp/run",
            resumed=False,
            resumed_transitions=0,
        ),
        TurnStarted(
            turn=1,
            env_step=0,
            state="NOT_FINISHED",
            level=0,
            win_levels=9,
            legal=[3, 4, 6, 7],
            grid=grid,
            has_world_model=False,
            surprise="",
        ),
        TextDelta(turn=1, text="delta"),
        ToolStarted(turn=1, call_id="id-1", name="commit_actions", args={}),
        ToolFinished(
            turn=1,
            call_id="id-1",
            name="commit_actions",
            output="ok",
            is_error=False,
        ),
        TurnCommitted(turn=1, plan=[[3, None, None]], reason="probe"),
        ActionTaken(
            turn=1,
            step_index=0,
            action=3,
            x=None,
            y=None,
            grid=grid,
            level_up=False,
            dead=False,
            win=False,
            state="NOT_FINISHED",
            level=0,
        ),
        ModelMispredicted(
            turn=1,
            step_index=0,
            surprise="surprise",
            predicted=[[0]],
            actual=grid,
        ),
        TurnFallback(turn=2, reason="no commit"),
        RunFinished(
            state="NOT_FINISHED",
            levels=0,
            win_levels=9,
            actions=1,
            transitions=1,
            has_world_model=True,
        ),
    ]

    path = tmp_path / "events.jsonl"
    with EventLog(path, clock=lambda: next(timestamps)) as event_log:
        for record in records:
            event_log.append(record)

    raw_lines = path.read_text(encoding="utf-8").splitlines()
    payloads = [json.loads(line) for line in raw_lines]
    assert [payload["seq"] for payload in payloads] == list(range(1, 11))
    assert [payload["ts"] for payload in payloads] == [float(i) for i in range(10, 20)]
    assert [payload["kind"] for payload in payloads] == [record.kind for record in records]
    assert len(fsynced) == len(records)
    assert all({"kind", "seq", "ts"} <= payload.keys() for payload in payloads)
    assert raw_lines[0].startswith('{"kind":"run_started","seq":1,"ts":10.0,')
    assert '"game_id":"bp35-0a0ad940"' in raw_lines[0]
    assert path.read_bytes().endswith(b"\n")


def test_event_log_reopens_without_truncating_and_continues_sequence(tmp_path, monkeypatch):
    monkeypatch.setattr(events_module.os, "fsync", lambda _: None)
    path = tmp_path / "events.jsonl"
    with EventLog(path, clock=lambda: 1.0) as event_log:
        event_log.emit("text_delta", turn=1, text="first")
    with EventLog(path, clock=lambda: 2.0) as event_log:
        event_log.emit("text_delta", turn=2, text="second")

    payloads = [json.loads(line) for line in path.read_text().splitlines()]
    assert [payload["seq"] for payload in payloads] == [1, 2]
    assert [payload["text"] for payload in payloads] == ["first", "second"]
