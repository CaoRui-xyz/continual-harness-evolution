"""Provider-free checks for the original fixed-baseline Meta-Harness method."""

from __future__ import annotations

import json
from pathlib import Path

import meta_harness
from meta_harness import CandidateOutcome, EvolutionController


def test_two_candidates_are_independent_h0_variants_and_update_frontier(tmp_path: Path, monkeypatch) -> None:
    """The previous Mini loop hill-climbed; source TB2 does not.

    Run two simulated iterations through the real H0 setup and AHE validator,
    asserting the second candidate is forked from H0 even after iteration one
    becomes the overall frontier winner.
    """
    monkeypatch.setattr(meta_harness, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(meta_harness, "WORKSPACES", tmp_path / "workspaces")
    config = meta_harness.load_config(Path(meta_harness.ROOT) / "configs" / "evolution" / "swebench-smoke.yaml")
    controller = EvolutionController(config, "two-independent-iterations")
    observed_candidate_bases: list[str] = []

    def fake_score(candidate_id: str, commit: str, snapshot: Path, tree: str, suites, **_kwargs):
        score = 0.0 if candidate_id == "H0" else (0.75 if candidate_id == "legal_comment_1" else 0.5)
        task = f"{suites[0].suite_id}/astropy__astropy-14539"
        return CandidateOutcome(candidate_id, commit, tree, True, score, {"fake_harbor": [suite.suite_id for suite in suites]}, {task: score})

    def fake_propose(workspace: Path, iteration: int, _timeout: int):
        observed_candidate_bases.append(
            meta_harness.subprocess_output(workspace, "rev-parse", "HEAD")
        )
        target = workspace / "src" / "minisweagent" / "agents" / "default.py"
        target.write_text(target.read_text(encoding="utf-8") + f"\n# independent candidate {iteration}\n", encoding="utf-8")
        return {"name": f"legal_comment_{iteration}", "hypothesis": "a legal H0 variant reaches the evaluator", "changes": "append a harmless agent-module comment", "expected_efficiency": "no token or call change"}

    monkeypatch.setattr(controller, "_score_candidate_for_suites", fake_score)
    monkeypatch.setattr(controller, "_propose", fake_propose)
    controller.run(iterations=2, propose_timeout=1, skip_baseline=False)

    state = json.loads(controller.state_path.read_text(encoding="utf-8"))
    frontier = json.loads(controller.frontier_path.read_text(encoding="utf-8"))
    assert observed_candidate_bases == [state["h0_commit"], state["h0_commit"]]
    assert state["candidate_base_commit"] == state["h0_commit"]
    assert state["candidate_base_id"] == "H0"
    assert state["next_iteration"] == 3
    assert frontier["_best"]["agent"] == "legal_comment_1"
    second_snapshot = controller.candidates_dir / "legal_comment_2" / "workspace" / "src" / "minisweagent" / "agents" / "default.py"
    second_text = second_snapshot.read_text(encoding="utf-8")
    assert "independent candidate 2" in second_text
    assert "independent candidate 1" not in second_text


def test_full_eval_skips_h0_winner(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(meta_harness, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(meta_harness, "WORKSPACES", tmp_path / "workspaces")
    config = meta_harness.load_config(Path(meta_harness.ROOT) / "configs" / "evolution" / "swebench-smoke.yaml")
    controller = EvolutionController(config, "full-eval")
    controller.run_dir.mkdir(parents=True)
    controller.frontier_path.write_text(json.dumps({"_best": {"agent": "H0", "avg_pass_rate": 1.0}}), encoding="utf-8")
    assert controller.full_evaluate() is None


def test_pending_metadata_is_descriptive_not_a_candidate_validity_gate(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(meta_harness, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(meta_harness, "WORKSPACES", tmp_path / "workspaces")
    config = meta_harness.load_config(Path(meta_harness.ROOT) / "configs" / "evolution" / "swebench-smoke.yaml")
    controller = EvolutionController(config, "metadata")
    controller.run_dir.mkdir(parents=True)

    class Result:
        exit_code = 0
        def show(self) -> None:
            return None

    def fake_run(**_kwargs):
        controller.pending_path.write_text(json.dumps({"candidates": [{"name": "minimal_candidate"}]}), encoding="utf-8")
        return Result()

    monkeypatch.setattr(meta_harness.claude_wrapper, "run", fake_run)
    assert controller._propose(tmp_path, 1, 1)["name"] == "minimal_candidate"


def test_proposer_exception_restores_anthropic_key(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(meta_harness, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(meta_harness, "WORKSPACES", tmp_path / "workspaces")
    config = meta_harness.load_config(Path(meta_harness.ROOT) / "configs" / "evolution" / "swebench-smoke.yaml")
    controller = EvolutionController(config, "restore-key")
    controller.run_dir.mkdir(parents=True)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-be-restored")

    def fake_run(**_kwargs):
        raise RuntimeError("proposer transport error")

    monkeypatch.setattr(meta_harness.claude_wrapper, "run", fake_run)

    import pytest

    with pytest.raises(RuntimeError, match="transport"):
        controller._propose(tmp_path, 1, 1)
    assert meta_harness.os.environ["ANTHROPIC_API_KEY"] == "must-be-restored"


def test_unchanged_h0_workspace_can_be_declared_as_a_candidate(tmp_path: Path) -> None:
    from mini_runtime.workspace import commit_candidate
    import subprocess

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init", "--initial-branch=main"], cwd=workspace, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=workspace, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=workspace, check=True)
    (workspace / "x.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "x.py"], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "-m", "H0"], cwd=workspace, check=True, capture_output=True)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=workspace, text=True).strip()
    assert commit_candidate(workspace, "candidate: unchanged") == head


def test_resume_refuses_changed_search_trial_count(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(meta_harness, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(meta_harness, "WORKSPACES", tmp_path / "workspaces")
    config_path = Path(meta_harness.ROOT) / "configs" / "evolution" / "swebench-smoke.yaml"
    first = EvolutionController(meta_harness.load_config(config_path), "resume", search_attempts=1, search_concurrency=1)
    first.initialize()
    changed = EvolutionController(meta_harness.load_config(config_path), "resume", search_attempts=2, search_concurrency=1)
    import pytest

    with pytest.raises(meta_harness.ContractError, match="task set or trial count"):
        changed.initialize()


def test_resume_accepts_concurrency_change_and_records_it(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(meta_harness, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(meta_harness, "WORKSPACES", tmp_path / "workspaces")
    config_path = Path(meta_harness.ROOT) / "configs" / "evolution" / "swebench-smoke.yaml"
    first = EvolutionController(meta_harness.load_config(config_path), "concurrency", search_attempts=1, search_concurrency=1)
    first.initialize()
    resumed = EvolutionController(meta_harness.load_config(config_path), "concurrency", search_attempts=1, search_concurrency=2)
    assert resumed.initialize()["run_name"] == "concurrency"
    rows = (resumed.run_dir / "resume_execution_overrides.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(rows) == 1
    row = json.loads(rows[0])
    assert row["previous_search_contract"]["suites"][0]["n_concurrent"] == 1
    assert row["requested_search_contract"]["suites"][0]["n_concurrent"] == 2


def test_resume_accepts_the_identical_json_normalized_contract(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(meta_harness, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(meta_harness, "WORKSPACES", tmp_path / "workspaces")
    config_path = Path(meta_harness.ROOT) / "configs" / "evolution" / "swebench-smoke.yaml"
    first = EvolutionController(meta_harness.load_config(config_path), "same", search_attempts=1, search_concurrency=1)
    state = first.initialize()
    resumed = EvolutionController(meta_harness.load_config(config_path), "same", search_attempts=1, search_concurrency=1)
    assert resumed.initialize()["h0_commit"] == state["h0_commit"]


def test_fresh_preserves_harbor_jobs_but_clears_generated_candidate_source(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(meta_harness, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(meta_harness, "WORKSPACES", tmp_path / "workspaces")
    config_path = Path(meta_harness.ROOT) / "configs" / "evolution" / "swebench-smoke.yaml"
    controller = EvolutionController(meta_harness.load_config(config_path), "fresh")
    state = controller.initialize()
    baseline = controller.candidates_dir / "H0" / "workspace"
    baseline.mkdir(parents=True)
    candidate = controller.candidates_dir / "generated" / "workspace"
    candidate.mkdir(parents=True)
    controller.evaluations_dir.joinpath("H0").mkdir(parents=True)
    controller.evaluations_dir.joinpath("generated").mkdir(parents=True)
    controller.summary_path.write_text("{}\n", encoding="utf-8")
    controller.frontier_path.write_text("{}\n", encoding="utf-8")
    controller.fresh_start()
    assert baseline.is_dir()
    assert not candidate.exists()
    # Like source TB2 --fresh, job artifacts remain available for diagnosis.
    assert controller.evaluations_dir.joinpath("H0").is_dir()
    assert controller.evaluations_dir.joinpath("generated").is_dir()
    assert not controller.summary_path.exists()
    assert not controller.frontier_path.exists()
    assert json.loads(controller.state_path.read_text(encoding="utf-8"))["next_iteration"] == 1
    assert state["h0_commit"] == json.loads(controller.state_path.read_text(encoding="utf-8"))["h0_commit"]


def test_cached_baseline_rejects_mutated_snapshot(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(meta_harness, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(meta_harness, "WORKSPACES", tmp_path / "workspaces")
    config_path = Path(meta_harness.ROOT) / "configs" / "evolution" / "swebench-smoke.yaml"
    controller = EvolutionController(meta_harness.load_config(config_path), "baseline-snapshot")
    state = controller.initialize()
    prepared = controller._prepare_candidate(controller.repository, controller.baseline_id)
    controller.baselines_dir.mkdir(parents=True, exist_ok=True)
    controller.baselines_dir.joinpath("H0.json").write_text(
        json.dumps(CandidateOutcome("H0", state["h0_commit"], prepared.candidate_tree_sha256, True, 1.0, {}).payload()),
        encoding="utf-8",
    )
    snapshot_file = prepared.snapshot / "src" / "minisweagent" / "agents" / "default.py"
    snapshot_file.write_text(snapshot_file.read_text(encoding="utf-8") + "\n# mutated after baseline cache\n", encoding="utf-8")

    import pytest

    with pytest.raises(meta_harness.ContractError, match="cached H0 baseline"):
        controller._baseline_outcome(state)


def test_scoring_rejects_snapshot_mutation_during_harbor(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(meta_harness, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(meta_harness, "WORKSPACES", tmp_path / "workspaces")
    config_path = Path(meta_harness.ROOT) / "configs" / "evolution" / "swebench-smoke.yaml"
    controller = EvolutionController(meta_harness.load_config(config_path), "mutation")
    controller.initialize()
    prepared = controller._prepare_candidate(controller.repository, controller.baseline_id)

    def fake_run_harbor(*, jobs_dir: Path, **_kwargs) -> Path:
        target = prepared.snapshot / "src" / "minisweagent" / "agents" / "default.py"
        target.write_text(target.read_text(encoding="utf-8") + "\n# mutated while evaluating\n", encoding="utf-8")
        job = jobs_dir / "job"; job.mkdir(parents=True)
        return job

    monkeypatch.setattr(meta_harness, "run_harbor", fake_run_harbor)

    import pytest

    with pytest.raises(meta_harness.ContractError, match="changed during Harbor evaluation"):
        controller._score_candidate(prepared)
