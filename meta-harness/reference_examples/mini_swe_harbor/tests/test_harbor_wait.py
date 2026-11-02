from __future__ import annotations

import json
from pathlib import Path

import pytest

from mini_runtime.contracts import ContractError
from mini_runtime.harbor import _wait_for_finished_job


def test_wait_requires_finished_at_not_merely_a_result_snapshot(tmp_path: Path) -> None:
    result = tmp_path / "result.json"
    result.write_text(json.dumps({"finished_at": None, "stats": {"n_running_trials": 1}}), encoding="utf-8")
    with pytest.raises(ContractError, match="no partial score"):
        _wait_for_finished_job(tmp_path, timeout_seconds=0, poll_seconds=0)


def test_wait_accepts_terminal_result(tmp_path: Path) -> None:
    (tmp_path / "result.json").write_text(json.dumps({"finished_at": "2026-08-16T00:00:00Z", "stats": {"n_completed_trials": 1}}), encoding="utf-8")
    _wait_for_finished_job(tmp_path, timeout_seconds=1, poll_seconds=0)
