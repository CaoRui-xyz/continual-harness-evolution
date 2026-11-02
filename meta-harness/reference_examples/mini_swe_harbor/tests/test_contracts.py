from __future__ import annotations

import json
from pathlib import Path

import pytest

from mini_runtime.contracts import ContractError, load_suite, make_contract, suite_with_execution, tree_sha256
from mini_runtime.results import score_job


def test_text_task_selection_survives_relocation_and_checks_files(tmp_path: Path) -> None:
    import shutil
    import yaml

    collection = tmp_path / "collection"
    collection.mkdir()
    for name in ("one", "two"):
        task = collection / "tasks" / name
        (task / "environment").mkdir(parents=True)
        (task / "tests").mkdir()
        (task / "task.toml").write_text('version = "1.0"\n')
        (task / "instruction.md").write_text(name)
        (task / "tests/test.sh").write_text("exit 0\n")
    (collection / "task_names.txt").write_text("two\none\n")
    config = {
        "schema_version": 1, "id": "portable",
        "source": {"kind": "local_path", "path": "tasks"},
        "selection": {"task_names_file": "task_names.txt", "expected_task_count": 2},
        "execution": {"attempts": 2},
    }
    (collection / "suite.yaml").write_text(yaml.safe_dump(config))
    suite = load_suite(collection / "suite.yaml")
    assert suite.task_names == ("two", "one")
    assert suite.harbor_task_names == suite.task_names
    relocated = tmp_path / "different location"
    shutil.move(collection, relocated)
    moved = load_suite(relocated / "suite.yaml")
    assert moved.path == str(relocated / "tasks")
    assert moved.task_names == suite.task_names
    assert moved.manifest_sha256 == suite.manifest_sha256
    (relocated / "tasks/two/tests/test.sh").unlink()
    with pytest.raises(ContractError, match="Missing task input"):
        load_suite(relocated / "suite.yaml")


@pytest.mark.parametrize("text", ["one\none\n", "../one\n", "swe-bench/one\n", "one*\n", "\n"])
def test_text_task_names_reject_ambiguous_harbor_filters(tmp_path: Path, text: str) -> None:
    from adapters import load_task_names

    names = tmp_path / "task_names.txt"
    names.write_text(text)
    with pytest.raises(ValueError):
        load_task_names(names)


def write_suite(tmp_path: Path, *, attempts: int = 2, concurrent: int = 4) -> Path:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"tasks": [{"instance_id": "one"}, {"instance_id": "two"}]}), encoding="utf-8")
    suite = tmp_path / "suite.yaml"
    suite.write_text(
        "\n".join([
            "schema_version: 1", "id: test", "source:", "  kind: local_path", f"  path: {tmp_path}", "selection:", "  manifest: manifest.json", "  task_names: []", "  exclude_task_names: []", "  expected_task_count: 2", "execution:", f"  attempts: {attempts}", f"  n_concurrent: {concurrent}", "  task_timeout_seconds: 1", "  sandbox_timeout_seconds: 1", "scoring:", "  metric: fraction_solved", "  solved_reward_threshold: 1.0", "",
        ]),
        encoding="utf-8",
    )
    return suite


def contract(tmp_path: Path):
    suite = load_suite(write_suite(tmp_path))
    candidate = tmp_path / "candidate"; candidate.mkdir(); (candidate / "x.py").write_text("x = 1\n", encoding="utf-8")
    policy = tmp_path / "policy"; policy.write_text("x\n", encoding="utf-8")
    lock = tmp_path / "lock"; lock.write_text("x\n", encoding="utf-8")
    adapter = tmp_path / "adapter"; adapter.write_text("x\n", encoding="utf-8")
    return make_contract(candidate_id="c", candidate_tree=tree_sha256(candidate), candidate_snapshot=candidate, suite=suite, runtime={}, policy_path=policy, evaluation_lock_path=lock, adapter_paths=[adapter])


def result(job: Path, task: str, suffix: str, reward: float = 1) -> None:
    trial = job / f"{task}__{suffix}"; trial.mkdir()
    (trial / "result.json").write_text(json.dumps({"verifier_result": {"rewards": {"reward": reward}}}), encoding="utf-8")


def test_complete_matrix_scores_all_declared_attempts(tmp_path: Path) -> None:
    frozen = contract(tmp_path)
    job = tmp_path / "job"; job.mkdir()
    for task in ("one", "two"):
        result(job, task, "aaaaaa", 1)
        result(job, task, "bbbbbb", 0)
    score = score_job(frozen, job)
    assert score.complete
    assert score.expected_count == score.observed_count == 4
    assert score.score == 0.5


def test_missing_attempt_is_incomplete_not_a_smaller_denominator(tmp_path: Path) -> None:
    frozen = contract(tmp_path)
    job = tmp_path / "job"; job.mkdir()
    result(job, "one", "aaaaaa", 1)
    result(job, "one", "bbbbbb", 1)
    result(job, "two", "aaaaaa", 1)
    score = score_job(frozen, job)
    assert not score.complete
    assert score.score is None
    assert score.expected_count == 4
    assert score.observed_count == 3


def test_duplicate_attempt_is_incomplete(tmp_path: Path) -> None:
    frozen = contract(tmp_path)
    job = tmp_path / "job"; job.mkdir()
    for suffix in ("aaaaaa", "bbbbbb", "cccccc"):
        result(job, "one", suffix, 1)
    result(job, "two", "aaaaaa", 1)
    result(job, "two", "bbbbbb", 1)
    score = score_job(frozen, job)
    assert not score.complete
    assert score.score is None


def test_suite_parser_leaves_capacity_to_runtime_lock(tmp_path: Path) -> None:
    suite = load_suite(write_suite(tmp_path, concurrent=6))
    assert suite.n_concurrent == 6


def test_cli_execution_override_has_its_own_contract_identity(tmp_path: Path) -> None:
    suite = load_suite(write_suite(tmp_path, attempts=2, concurrent=4))
    overridden = suite_with_execution(suite, attempts=5, n_concurrent=1)
    assert overridden.attempts == 5
    assert overridden.n_concurrent == 1
    assert overridden.suite_sha256 != suite.suite_sha256
    widened = suite_with_execution(suite, n_concurrent=6)
    assert widened.n_concurrent == 6


@pytest.mark.parametrize("source_kind", ["local_path", "harbor_dataset"])
def test_suite_prefix_keeps_local_filters_bare(tmp_path: Path, source_kind: str) -> None:
    suite_path = write_suite(tmp_path)
    raw = suite_path.read_text(encoding="utf-8").replace("  task_names: []", "  task_name_prefix: registry/\n  task_names: []")
    if source_kind == "harbor_dataset":
        raw = raw.replace(f"  kind: local_path\n  path: {tmp_path}", "  kind: harbor_dataset\n  dataset: example@1.0")
    suite_path.write_text(raw, encoding="utf-8")
    suite = load_suite(suite_path)
    assert suite.task_names == ("one", "two")
    assert suite.harbor_task_names == (("registry/one", "registry/two") if source_kind == "harbor_dataset" else ("one", "two"))


def test_scoring_normalizes_only_the_contract_declared_registry_prefix(tmp_path: Path) -> None:
    suite_path = write_suite(tmp_path)
    raw = suite_path.read_text(encoding="utf-8").replace("  task_names: []", "  task_name_prefix: registry/\n  task_names: []")
    suite_path.write_text(raw, encoding="utf-8")
    suite = load_suite(suite_path)
    candidate = tmp_path / "candidate"; candidate.mkdir(); (candidate / "x.py").write_text("x = 1\n", encoding="utf-8")
    policy = tmp_path / "policy"; policy.write_text("x\n", encoding="utf-8")
    lock = tmp_path / "lock"; lock.write_text("x\n", encoding="utf-8")
    adapter = tmp_path / "adapter"; adapter.write_text("x\n", encoding="utf-8")
    frozen = make_contract(candidate_id="c", candidate_tree=tree_sha256(candidate), candidate_snapshot=candidate, suite=suite, runtime={}, policy_path=policy, evaluation_lock_path=lock, adapter_paths=[adapter])
    job = tmp_path / "job"; job.mkdir()
    for task in ("one", "two"):
        for suffix in ("aaaaaa", "bbbbbb"):
            trial = job / f"{task}__{suffix}"; trial.mkdir()
            (trial / "result.json").write_text(json.dumps({"task_name": f"registry/{task}", "verifier_result": {"rewards": {"reward": 1}}}), encoding="utf-8")

    score = score_job(frozen, job)

    assert score.complete
    assert score.score == 1.0
    assert score.observed_count == 4

    extra = job / "other__cccccc"; extra.mkdir()
    (extra / "result.json").write_text(json.dumps({"task_name": "registry/not-declared", "verifier_result": {"rewards": {"reward": 1}}}), encoding="utf-8")
    incomplete = score_job(frozen, job)
    assert not incomplete.complete
    assert incomplete.score is None
    assert "unexpected trial directories" in incomplete.reason


def test_exception_is_an_explicit_zero_but_missing_is_not(tmp_path: Path) -> None:
    frozen = contract(tmp_path)
    job = tmp_path / "job"; job.mkdir()
    result(job, "one", "aaaaaa", 1)
    result(job, "one", "bbbbbb", 1)
    result(job, "two", "aaaaaa", 1)
    exception = job / "two__bbbbbb"; exception.mkdir()
    (exception / "exception.txt").write_text("TimeoutError: terminal\n", encoding="utf-8")
    score = score_job(frozen, job)
    assert score.complete
    assert score.score == 0.75


def test_result_without_verifier_reward_is_incomplete_not_a_zero(tmp_path: Path) -> None:
    frozen = contract(tmp_path)
    job = tmp_path / "job"; job.mkdir()
    result(job, "one", "aaaaaa", 1)
    result(job, "one", "bbbbbb", 1)
    result(job, "two", "aaaaaa", 1)
    malformed = job / "two__bbbbbb"; malformed.mkdir()
    (malformed / "result.json").write_text(json.dumps({"verifier_result": {}}), encoding="utf-8")

    score = score_job(frozen, job)

    assert not score.complete
    assert score.score is None
    assert "lacks verifier reward" in score.reason


def test_terminal_result_exception_without_reward_is_an_explicit_zero(tmp_path: Path) -> None:
    frozen = contract(tmp_path)
    job = tmp_path / "job"; job.mkdir()
    result(job, "one", "aaaaaa", 1)
    result(job, "one", "bbbbbb", 1)
    result(job, "two", "aaaaaa", 1)
    exception = job / "two__bbbbbb"; exception.mkdir()
    (exception / "result.json").write_text(
        json.dumps({"exception_info": {"exception_type": "AgentTimeoutError", "exception_message": "timed out"}}),
        encoding="utf-8",
    )

    score = score_job(frozen, job)

    assert score.complete
    assert score.score == 0.75


def test_unexpected_nonterminal_task_directory_is_incomplete(tmp_path: Path) -> None:
    frozen = contract(tmp_path)
    job = tmp_path / "job"; job.mkdir()
    for task in ("one", "two"):
        result(job, task, "aaaaaa", 1)
        result(job, task, "bbbbbb", 1)
    (job / "not_in_contract__cccccc").mkdir()

    score = score_job(frozen, job)

    assert not score.complete
    assert score.score is None
    assert "unexpected trial directories" in score.reason


def test_nonterminal_declared_trial_directory_is_incomplete(tmp_path: Path) -> None:
    frozen = contract(tmp_path)
    job = tmp_path / "job"; job.mkdir()
    result(job, "one", "aaaaaa", 1)
    result(job, "one", "bbbbbb", 1)
    result(job, "two", "aaaaaa", 1)
    (job / "two__bbbbbb").mkdir()

    score = score_job(frozen, job)

    assert not score.complete
    assert score.score is None
    assert "nonterminal Harbor trial directory" in score.reason


def test_contract_identity_changes_when_candidate_or_adapter_changes(tmp_path: Path) -> None:
    first = contract(tmp_path)
    adapter = tmp_path / "adapter"; adapter.write_text("changed\n", encoding="utf-8")
    suite = load_suite(write_suite(tmp_path))
    candidate = tmp_path / "candidate"
    policy = tmp_path / "policy"; lock = tmp_path / "lock"
    second = make_contract(candidate_id="c", candidate_tree=tree_sha256(candidate), candidate_snapshot=candidate, suite=suite, runtime={}, policy_path=policy, evaluation_lock_path=lock, adapter_paths=[adapter])
    assert first.contract_id != second.contract_id
