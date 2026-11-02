from __future__ import annotations

import json
from pathlib import Path

import meta_harness
from meta_harness import CandidateOutcome, EvolutionController, PreparedCandidate


def controller_for_state(tmp_path: Path, monkeypatch) -> EvolutionController:
    monkeypatch.setattr(meta_harness, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(meta_harness, "WORKSPACES", tmp_path / "workspaces")
    cfg = meta_harness.load_config(Path(meta_harness.ROOT) / "configs" / "evolution" / "swebench-smoke.yaml")
    controller = EvolutionController(cfg, "state")
    controller.run_dir.mkdir(parents=True)
    controller.state_path.write_text(
        json.dumps(
            {
                "h0_commit": "h0",
                "candidate_base_commit": "h0",
                "candidate_base_id": "H0",
                "next_iteration": 1,
            }
        ),
        encoding="utf-8",
    )
    return controller


def test_incomplete_candidate_never_replaces_task_or_overall_frontier(tmp_path: Path, monkeypatch) -> None:
    controller = controller_for_state(tmp_path, monkeypatch)
    controller.frontier_path.write_text(
        json.dumps(
            {
                "suite/one": {"best_agent": "good", "pass_rate": 0.5},
                "_best": {"agent": "good", "avg_pass_rate": 0.5},
            }
        ),
        encoding="utf-8",
    )
    outcome = CandidateOutcome("bad", "badcommit", "new", False, None, {"reason": "missing trial"}, {"suite/one": 1.0})
    assert not controller.record(1, outcome, "hypothesis", "changes")
    frontier = json.loads(controller.frontier_path.read_text(encoding="utf-8"))
    assert frontier["suite/one"]["best_agent"] == "good"
    assert frontier["_best"]["agent"] == "good"


def test_complete_candidate_updates_per_task_and_overall_frontier(tmp_path: Path, monkeypatch) -> None:
    controller = controller_for_state(tmp_path, monkeypatch)
    controller.frontier_path.write_text(
        json.dumps(
            {
                "suite/one": {"best_agent": "good", "pass_rate": 0.5},
                "suite/two": {"best_agent": "good", "pass_rate": 0.75},
                "_best": {"agent": "good", "avg_pass_rate": 0.5},
            }
        ),
        encoding="utf-8",
    )
    outcome = CandidateOutcome("better", "bettercommit", "new", True, 0.75, {"suites": []}, {"suite/one": 1.0, "suite/two": 0.5})
    assert controller.record(1, outcome, "hypothesis", "changes")
    frontier = json.loads(controller.frontier_path.read_text(encoding="utf-8"))
    assert frontier["suite/one"]["best_agent"] == "better"
    assert frontier["suite/two"]["best_agent"] == "good"
    assert frontier["_best"]["agent"] == "better"
    # Like the source: ties don't replace an earlier task/frontier record.
    tied = CandidateOutcome("tie", "tiecommit", "tie", True, 0.75, {"suites": []}, {"suite/one": 1.0})
    assert not controller.record(2, tied, "tie", "tie")
    frontier = json.loads(controller.frontier_path.read_text(encoding="utf-8"))
    assert frontier["suite/one"]["best_agent"] == "better"
    assert frontier["_best"]["agent"] == "better"


def test_invalid_candidate_is_logged_as_source_style_display_zero(tmp_path: Path, monkeypatch) -> None:
    controller = controller_for_state(tmp_path, monkeypatch)
    controller.frontier_path.write_text(json.dumps({"_best": {"agent": "H0", "avg_pass_rate": 0.5}}), encoding="utf-8")
    outcome = CandidateOutcome("invalid", "", "", False, None, {"error": "validator"})
    controller.record(1, outcome, "hypothesis", "changes")
    row = json.loads(controller.summary_path.read_text(encoding="utf-8"))
    assert row["avg_pass_rate"] == 0.0
    assert row["outcome"] == "failed"
    assert row["complete"] is False


def test_search_configs_have_a_one_attempt_non_scoring_smoke_suite() -> None:
    root = Path(meta_harness.ROOT)
    for name in ("swebench.yaml", "swtbench.yaml", "featurebench.yaml"):
        controller = EvolutionController(meta_harness.load_config(root / "configs" / "evolution" / name), f"smoke-{name}")
        assert controller.smoke_suite is not None
        assert controller.smoke_suite.attempts == 1
        assert len(controller.smoke_suite.task_names) == 1


def test_smoke_accepts_a_healthy_terminal_trial_without_a_verifier_reward(tmp_path: Path, monkeypatch) -> None:
    """Match TB2 smoke semantics: runtime health is independent of reward."""
    controller = controller_for_state(tmp_path, monkeypatch)
    controller.smoke_suite = controller.suites[0]
    job_dir = tmp_path / "smoke-job"
    job_dir.mkdir()
    (job_dir / "result.json").write_text(
        json.dumps(
            {
                "finished_at": "2026-08-19T00:00:00",
                "stats": {"n_errored_trials": 0},
            }
        ),
        encoding="utf-8",
    )
    # The strict search scorer correctly marks the missing reward incomplete.
    # The smoke gate must nevertheless accept a terminal, zero-error job.
    outcome = CandidateOutcome(
        "candidate",
        "commit",
        "tree",
        False,
        None,
        {"suites": [{"job_dir": str(job_dir)}]},
    )
    monkeypatch.setattr(controller, "_score_candidate", lambda *_args, **_kwargs: outcome)

    passed, detail = controller._smoke(
        PreparedCandidate("candidate", "commit", "tree", tmp_path / "snapshot")
    )

    assert passed
    assert detail["passed"] is True
    assert detail["outcome"]["complete"] is False
