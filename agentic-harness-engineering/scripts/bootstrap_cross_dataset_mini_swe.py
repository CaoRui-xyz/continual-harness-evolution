#!/usr/bin/env python3
"""Create a safe cross-dataset warm-start seed for a Mini-SWE AHE experiment.

This is deliberately *not* an experiment-resume tool.  A source experiment's
scores, search history, task manifest, and Harbor outputs are specific to its
dataset.  The script exports only the fully evaluated harness snapshot, writes
a new target-config overlay, and leaves AHE to create a fresh H0 and run the
target-dataset baseline.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

# Keep copying and tree identities exactly aligned with the AHE implementation.
# Importing evolve.py does not run an experiment; it only loads the shared
# config/tree helpers used by normal Mini-SWE initialization.
from evolve import _mini_source_ignored, _mini_source_tree_sha256, load_config  # noqa: E402


class TransferError(RuntimeError):
    """A fail-closed validation error for a proposed transfer."""


def _run(command: list[str], *, cwd: Path | None = None, text: str = "") -> subprocess.CompletedProcess[str]:
    """Run a local command without shell interpolation and raise useful errors."""
    completed = subprocess.run(
        command,
        cwd=cwd,
        input=text or None,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()
        raise TransferError(f"Command failed ({' '.join(command)}): {detail[-2000:]}")
    return completed


def _load_yaml(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise TransferError(f"Missing {label}: {path}")
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as error:
        raise TransferError(f"Invalid {label} YAML ({path}): {error}") from error
    if not isinstance(payload, dict):
        raise TransferError(f"{label} must have a mapping at its top level: {path}")
    return payload


def _resolve_project_path(value: Any, label: str) -> Path:
    text = str(value or "").strip()
    if not text:
        raise TransferError(f"Missing {label}")
    path = Path(text).expanduser()
    return path.resolve() if path.is_absolute() else (PROJECT_DIR / path).resolve()


def _ensure_mini_config(config: dict[str, Any], label: str) -> None:
    if str(config.get("adapter", {}).get("kind", "")).strip() != "mini_swe_harbor":
        raise TransferError(f"{label} is not a mini_swe_harbor experiment")
    mini = config.get("mini_swe")
    if not isinstance(mini, dict):
        raise TransferError(f"{label} has no mini_swe runtime contract")
    if not str(config.get("source_config_dir", "")).strip():
        raise TransferError(f"{label} has no source_config_dir")
    if not str(config.get("agent_config_filename", "")).strip():
        raise TransferError(f"{label} has no agent_config_filename")


def _dataset_identity(config: dict[str, Any]) -> dict[str, Any]:
    """Return the complete fixed task manifest used by a Harbor config."""
    dataset = str(config.get("dataset", "")).strip()
    raw_path = str(config.get("path", "")).strip()
    if bool(dataset) == bool(raw_path):
        raise TransferError("Experiment must specify exactly one of dataset or path")
    tasks = config.get("task_names")
    if not isinstance(tasks, list) or not tasks or not all(isinstance(item, str) and item for item in tasks):
        raise TransferError("Cross-dataset transfer requires a non-empty explicit task_names manifest")
    if len(set(tasks)) != len(tasks):
        raise TransferError("Task manifest contains duplicate task names")
    if dataset:
        return {"kind": "dataset", "dataset": dataset, "task_names": tasks}
    return {"kind": "path", "path": str(_resolve_project_path(raw_path, "path")), "task_names": tasks}


def _file_hash(path: Path) -> str:
    if not path.is_file():
        raise TransferError(f"Missing runtime-contract file: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _runtime_contract(config: dict[str, Any]) -> dict[str, Any]:
    """Capture only fields that affect candidate semantics, not dataset choice."""
    mini = config["mini_swe"]
    policy = _resolve_project_path(mini.get("workspace_policy"), "mini_swe.workspace_policy")
    evaluation_lock = _resolve_project_path(mini.get("evaluation_lock"), "mini_swe.evaluation_lock")
    runner = Path(str(mini.get("harbor_runner_python", "")).strip()).expanduser()
    source_dir = _resolve_project_path(mini.get("harbor_source_dir"), "mini_swe.harbor_source_dir")
    if not runner.is_file():
        raise TransferError(f"Harbor runtime Python does not exist: {runner}")
    if not source_dir.is_dir():
        raise TransferError(f"Harbor source directory does not exist: {source_dir}")
    return {
        "workspace_policy_sha256": _file_hash(policy),
        "evaluation_lock_sha256": _file_hash(evaluation_lock),
        "harbor_runner_python": str(runner),
        "harbor_source_dir": str(source_dir),
        "harbor_source_commit": str(mini.get("harbor_source_commit", "")).strip(),
        "sandbox_timeout_seconds": mini.get("sandbox_timeout_seconds"),
        "agent_config_filename": str(config["agent_config_filename"]),
        "harbor_n_attempts": config.get("harbor", {}).get("k"),
        "harbor_force_build": config.get("harbor", {}).get("force_build"),
        "mini_upstream_commit": str(mini.get("upstream_commit", "")).strip(),
    }


def _load_scores(experiment: Path) -> list[dict[str, Any]]:
    payload = _load_yaml(experiment / "iteration_scores.yaml", "iteration scores")
    scores = payload.get("scores")
    if not isinstance(scores, list) or not scores:
        raise TransferError("Experiment has no recorded iteration scores")
    if not all(isinstance(score, dict) and isinstance(score.get("iteration"), int) for score in scores):
        raise TransferError("iteration_scores.yaml contains an invalid score entry")
    if len({score["iteration"] for score in scores}) != len(scores):
        raise TransferError("iteration_scores.yaml contains duplicate iteration numbers")
    return scores


def _select_winner(experiment: Path, requested: str) -> dict[str, Any]:
    scores = _load_scores(experiment)
    by_iteration = {score["iteration"]: score for score in scores}
    if requested == "auto":
        best_path = experiment / "best_ever.json"
        if not best_path.is_file():
            raise TransferError("--source-winner auto requires best_ever.json; select an explicit iteration instead")
        try:
            best = json.loads(best_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise TransferError(f"Invalid best_ever.json: {error}") from error
        iteration = best.get("iteration")
        if not isinstance(iteration, int) or iteration not in by_iteration:
            raise TransferError("best_ever.json does not identify an iteration present in iteration_scores.yaml")
        score = by_iteration[iteration]
        best_rate = best.get("pass_rate", best.get("capability_rate"))
        if not isinstance(best_rate, (int, float)) or not math.isclose(float(best_rate), float(score.get("pass_rate", -1)), abs_tol=1e-9):
            raise TransferError("best_ever.json and iteration_scores.yaml disagree; refuse to guess the winner")
        return score
    match = re.fullmatch(r"iteration:(\d+)", requested)
    if not match:
        raise TransferError("--source-winner must be auto or iteration:N")
    iteration = int(match.group(1))
    if iteration not in by_iteration:
        raise TransferError(f"No score recorded for requested source iteration {iteration}")
    return by_iteration[iteration]


def _score_job_dir(experiment: Path, score: dict[str, Any]) -> Path:
    raw = score.get("job_dir")
    if not isinstance(raw, str) or not raw:
        raise TransferError("Selected score has no job_dir")
    job_dir = (experiment / raw).resolve()
    if experiment.resolve() not in job_dir.parents:
        raise TransferError("Selected score job_dir escapes the source experiment")
    if not job_dir.is_dir():
        raise TransferError(f"Selected Harbor job directory is missing: {job_dir}")
    return job_dir


def _trial_task_name(trial_name: str) -> str:
    return re.sub(r"__[A-Za-z0-9]{6,}$", "", trial_name)


def _recorded_trial_task_name(trial_dir: Path) -> str:
    """Return the untruncated task identity stored by Harbor when available.

    Harbor abbreviates long local-task names when it creates trial directories.
    FeatureBench task IDs exceed that limit, but each terminal artifact still
    records the original ``task_name``.  Dataset tasks retain the directory
    fallback for compatibility with older Harbor artifacts.
    """
    result_path = trial_dir / "result.json"
    try:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _trial_task_name(trial_dir.name)
    task_name = payload.get("task_name")
    if isinstance(task_name, str) and task_name:
        return task_name
    task_id = payload.get("task_id")
    if isinstance(task_id, dict):
        task_path = task_id.get("path")
        if isinstance(task_path, str) and task_path:
            return Path(task_path).name
    return _trial_task_name(trial_dir.name)


def _trial_name_map(task_names: list[str]) -> dict[str, str]:
    """Map Harbor trial-dir names to the explicitly configured task IDs.

    Harbor preserves ``swe-bench/...`` in its dataset manifest but its trial
    directories currently omit that registry namespace.  The mapping is only
    accepted when it is one-to-one, so a future dataset cannot silently merge
    two configured tasks into one trial name.
    """
    mapped: dict[str, str] = {}
    for configured_name in task_names:
        trial_name = configured_name.split("/", 1)[1] if "/" in configured_name else configured_name
        previous = mapped.setdefault(trial_name, configured_name)
        if previous != configured_name:
            raise TransferError(
                "Task manifest has ambiguous Harbor trial-directory names: "
                f"{previous!r} and {configured_name!r} both map to {trial_name!r}"
            )
    return mapped


def _validate_completed_job(
    job_dir: Path,
    score: dict[str, Any],
    source_identity: dict[str, Any],
    *,
    allow_errored_source: bool = False,
) -> dict[str, Any]:
    """Verify the selected score came from a terminal fixed-manifest Harbor job.

    The normal transfer contract accepts only zero-error source jobs.  An
    explicit caller opt-in may instead export a historical source job whose
    every configured trial reached a terminal artifact but where Harbor marked
    some trials as exceptions.  That waiver is intentionally narrow: pending,
    running, cancelled, missing, malformed, duplicate, or off-manifest trials
    remain fatal, and the provenance records the raw terminal statistics.
    """
    try:
        result = json.loads((job_dir / "result.json").read_text(encoding="utf-8"))
        job_config = json.loads((job_dir / "config.json").read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise TransferError(f"Selected job is missing a Harbor artifact: {error.filename}") from error
    except json.JSONDecodeError as error:
        raise TransferError(f"Selected job has invalid JSON: {error}") from error
    if not result.get("finished_at"):
        raise TransferError("Selected Harbor job is not finished")
    stats = result.get("stats")
    if not isinstance(stats, dict):
        raise TransferError("Selected Harbor result has no stats mapping")
    for name in ("n_running_trials", "n_pending_trials", "n_cancelled_trials"):
        if stats.get(name) != 0:
            raise TransferError(f"Selected Harbor job is incomplete: {name}={stats.get(name)!r}")
    n_errored_trials = stats.get("n_errored_trials")
    if not isinstance(n_errored_trials, int) or n_errored_trials < 0:
        raise TransferError("Selected Harbor job has an invalid n_errored_trials value")
    if n_errored_trials and not allow_errored_source:
        raise TransferError(f"Selected Harbor job is incomplete: n_errored_trials={n_errored_trials}")
    attempts = job_config.get("n_attempts")
    if not isinstance(attempts, int) or attempts < 1 or attempts != score.get("k"):
        raise TransferError("Selected Harbor job attempt count does not match the recorded score")
    datasets = job_config.get("datasets")
    if not isinstance(datasets, list) or len(datasets) != 1 or not isinstance(datasets[0], dict):
        raise TransferError("Selected Harbor job does not contain exactly one explicit dataset manifest")
    dataset = datasets[0]
    if source_identity["kind"] == "dataset":
        expected_name, separator, expected_selector = source_identity["dataset"].partition("@")
        if not separator or not expected_selector:
            raise TransferError("Source dataset must use a pinned Harbor name@version-or-ref identity")
        if dataset.get("name") != expected_name:
            raise TransferError("Selected Harbor job dataset does not match the source experiment")
        actual_selector = dataset.get("ref") if dataset.get("ref") is not None else dataset.get("version")
        if str(actual_selector) != expected_selector:
            raise TransferError("Selected Harbor job dataset version/ref does not match the source experiment")
    elif dataset.get("path") is None:
        raise TransferError("Selected Harbor job is not the configured local-path experiment")
    if dataset.get("task_names") != source_identity["task_names"]:
        raise TransferError("Selected Harbor job task manifest does not match the source experiment")
    expected_trials = len(source_identity["task_names"]) * attempts
    if result.get("n_total_trials") != expected_trials or stats.get("n_completed_trials") != expected_trials:
        raise TransferError("Selected Harbor job trial count does not match its fixed task manifest")

    trial_dirs = sorted(path for path in job_dir.iterdir() if path.is_dir() and (path / "result.json").is_file())
    if len(trial_dirs) != expected_trials:
        raise TransferError("Selected Harbor job does not contain one result artifact per expected trial")
    trial_names = _trial_name_map(source_identity["task_names"])
    grouped: dict[str, list[float | None]] = {task: [] for task in source_identity["task_names"]}
    n_pass = 0
    n_exception = 0
    for trial_dir in trial_dirs:
        trial_task = _recorded_trial_task_name(trial_dir)
        task = trial_task if trial_task in grouped else trial_names.get(trial_task)
        if task is None:
            raise TransferError(f"Selected Harbor job has a trial outside its manifest: {trial_dir.name}")
        reward_path = trial_dir / "verifier" / "reward.txt"
        if reward_path.is_file():
            try:
                reward = float(reward_path.read_text(encoding="utf-8").strip())
            except ValueError as error:
                raise TransferError(f"Invalid reward artifact: {reward_path}") from error
            grouped[task].append(reward)
            if reward >= 1.0:
                n_pass += 1
        else:
            grouped[task].append(None)
            n_exception += 1
    if any(len(rewards) != attempts for rewards in grouped.values()):
        raise TransferError("Selected Harbor job does not have exactly k attempts for every task")
    observed_rate = n_pass / expected_trials
    score_matches_raw_trials = math.isclose(observed_rate, float(score.get("pass_rate", -1)), abs_tol=5e-5)
    if not score_matches_raw_trials and not allow_errored_source:
        raise TransferError(
            f"Selected score ({score.get('pass_rate')}) disagrees with verifier rewards ({observed_rate:.4f})"
        )
    trials = score.get("trials")
    score_trials_match_raw = not isinstance(trials, dict) or (
        trials.get("total") == expected_trials and trials.get("pass") == n_pass and trials.get("exception") == n_exception
    )
    if not score_trials_match_raw and not allow_errored_source:
        raise TransferError("Selected score's trial summary disagrees with the raw Harbor artifacts")
    return {
        "job_dir": str(job_dir),
        "finished_at": result["finished_at"],
        "n_trials": expected_trials,
        "n_pass": n_pass,
        "n_exception": n_exception,
        "pass_rate": observed_rate,
        "n_errored_trials": n_errored_trials,
        "reported_score_matches_raw_trials": score_matches_raw_trials,
        "reported_trial_summary_matches_raw_trials": score_trials_match_raw,
        "accepted_with_errored_source_waiver": allow_errored_source and n_errored_trials > 0,
    }


def _snapshot_for_iteration(experiment: Path, iteration: int) -> Path:
    snapshot = experiment / "runs" / f"iteration_{iteration:03d}" / "input" / "workspace"
    if not snapshot.is_dir():
        raise TransferError(f"Selected winner has no immutable pre-evaluation workspace snapshot: {snapshot}")
    return snapshot


def _reference_before_iteration(workspace: Path, iteration: int) -> str:
    if not (workspace / ".git").is_dir():
        raise TransferError(f"Source experiment workspace is not a Git repository: {workspace}")
    if iteration > 1:
        reference = f"refs/tags/iteration_{iteration - 1}"
        _run(["git", "rev-parse", "--verify", f"{reference}^{{commit}}"], cwd=workspace)
        return reference
    roots = [line for line in _run(["git", "rev-list", "--max-parents=0", "HEAD"], cwd=workspace).stdout.splitlines() if line]
    if len(roots) != 1:
        raise TransferError("Source workspace does not have a unique H0 root commit")
    return roots[0]


def _check_snapshot_filesystem(snapshot: Path) -> None:
    """Reject links/special files before copying a historic snapshot into a seed."""
    for root, directories, files in os.walk(snapshot, topdown=True, followlinks=False):
        root_path = Path(root)
        for name in list(directories):
            path = root_path / name
            if path.is_symlink():
                raise TransferError(f"Winner snapshot contains a symbolic-link directory: {path}")
        for name in files:
            path = root_path / name
            if path.is_symlink():
                raise TransferError(f"Winner snapshot contains a symbolic-link file: {path}")
            if not os.path.isfile(path):
                raise TransferError(f"Winner snapshot contains a non-regular file: {path}")
    required = snapshot / "src" / "minisweagent" / "config" / "mini.yaml"
    if not required.is_file():
        raise TransferError(f"Winner snapshot is not a complete Mini-SWE workspace: missing {required.relative_to(snapshot)}")


def _validator_command(config: dict[str, Any], workspace: Path, baseline_source: Path) -> list[str]:
    mini = config["mini_swe"]
    validator = PROJECT_DIR / "agents" / "evolve_agent" / "skills" / "mini-swe-evolution-guide" / "scripts" / "validate_workspace.py"
    policy = _resolve_project_path(mini.get("workspace_policy"), "mini_swe.workspace_policy")
    lock = _resolve_project_path(mini.get("evaluation_lock"), "mini_swe.evaluation_lock")
    runner = Path(str(mini.get("harbor_runner_python", "")).strip()).expanduser()
    harbor_source = _resolve_project_path(mini.get("harbor_source_dir"), "mini_swe.harbor_source_dir")
    if not validator.is_file():
        raise TransferError(f"Mini validator is missing: {validator}")
    return [
        sys.executable, str(validator), "--workspace", str(workspace), "--policy", str(policy),
        "--evaluation-lock", str(lock), "--runtime-python", str(runner),
        "--harbor-source-dir", str(harbor_source), "--baseline-source", str(baseline_source), "--json",
    ]


def _run_validator(config: dict[str, Any], workspace: Path, baseline_source: Path, label: str) -> dict[str, Any]:
    completed = subprocess.run(
        _validator_command(config, workspace, baseline_source),
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        report = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        detail = (completed.stderr or completed.stdout).strip()
        raise TransferError(f"{label} validator did not produce JSON: {detail[-2000:]}") from error
    if completed.returncode != 0 or not report.get("valid") or report.get("infrastructure_error"):
        issues = report.get("issues", [])
        raise TransferError(f"{label} failed current Mini workspace validation: {json.dumps(issues, ensure_ascii=False)[-3000:]}")
    return report


def _archive_reference(workspace: Path, reference: str, destination: Path) -> None:
    """Materialize a historical Git tree without modifying the source experiment."""
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / "reference.tar"
    _run(["git", "archive", "--format=tar", "--output", str(archive), reference], cwd=workspace)
    with tarfile.open(archive, "r") as tar:
        for member in tar.getmembers():
            member_path = (destination / "tree" / member.name).resolve()
            tree_root = (destination / "tree").resolve()
            if tree_root != member_path and tree_root not in member_path.parents:
                raise TransferError("Git archive contains an unsafe path")
        tar.extractall(destination / "tree", filter="data")


def _stage_as_new_h0(seed: Path, stage: Path) -> None:
    shutil.copytree(seed, stage, ignore=_mini_source_ignored)
    _run(["git", "init"], cwd=stage)
    _run(["git", "add", "-A", "-f"], cwd=stage)
    _run(
        ["git", "-c", "user.name=AHE transfer validator", "-c", "user.email=ahe-transfer@local", "commit", "-m", "v0: transferred Mini-SWE H0"],
        cwd=stage,
    )


def _safe_copy_seed(snapshot: Path, output_seed: Path) -> None:
    if output_seed.exists() or output_seed.is_symlink():
        raise TransferError(f"Output seed already exists (refusing to overwrite): {output_seed}")
    output_seed.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(snapshot, output_seed, ignore=_mini_source_ignored)


def _generated_overlay(
    target_config: Path,
    output_seed: Path,
    output_hash: str,
    source_experiment: Path,
    source_iteration: int,
) -> dict[str, Any]:
    target_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", target_config.stem).strip("-")
    source_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", source_experiment.name).strip("-")
    return {
        "_base": str(target_config.resolve()),
        "_name": f"mini-swe-transfer-{source_name}-i{source_iteration}-to-{target_name}",
        "_description": "Cross-dataset Mini-SWE warm start. Generated seed only; target baseline/frontier/history are new.",
        "source_config_dir": str(output_seed.resolve()),
        "mini_swe": {
            # The exported seed intentionally has no copied Git history.  Its
            # original upstream ancestry is recorded in provenance instead.
            "upstream_commit": "",
            "source_commit": "",
        },
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _validate_output_paths(paths: list[Path]) -> None:
    for path in paths:
        if path.exists() or path.is_symlink():
            raise TransferError(f"Output path already exists (refusing to overwrite): {path}")


def _within(path: Path, parent: Path) -> bool:
    """Return whether path is parent itself or a descendant, without following leaf links."""
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _output_path(value: str) -> Path:
    """Make an output path absolute while preserving a possible leaf symlink for rejection."""
    path = Path(os.path.abspath(os.path.expanduser(value)))
    # Resolve only the parent: this normalizes ``..`` and parent symlinks for
    # containment checks, while keeping a possibly-existing leaf symlink
    # visible to _validate_output_paths rather than silently following it.
    return path.parent.resolve() / path.name


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export a verified Mini-SWE winner as a fresh cross-dataset warm-start seed."
    )
    parser.add_argument("--source-experiment", required=True, help="Completed Mini-SWE AHE experiment directory")
    parser.add_argument("--target-config", required=True, help="Target dataset's existing AHE config/overlay")
    parser.add_argument("--output-seed", required=True, help="New, non-existent directory for the clean transferred seed")
    parser.add_argument("--source-winner", default="auto", help="auto (best_ever) or iteration:N")
    parser.add_argument("--output-config", help="Generated target overlay; default is <output-seed>.target.yaml")
    parser.add_argument("--provenance", help="Generated provenance JSON; default is <output-seed>.provenance.json")
    parser.add_argument("--allow-same-dataset", action="store_true", help="Allow a warm-start whose source and target dataset manifests are identical")
    parser.add_argument(
        "--allow-errored-source",
        action="store_true",
        help="Allow a terminal source Harbor job with errored trials; record the waiver and raw trial statistics in provenance",
    )
    parser.add_argument("--dry-run", action="store_true", help="Perform all checks without writing a seed, overlay, or provenance")
    args = parser.parse_args()

    try:
        source_experiment = Path(args.source_experiment).expanduser().resolve()
        target_config_path = Path(args.target_config).expanduser().resolve()
        output_seed = _output_path(args.output_seed)
        output_config = _output_path(args.output_config) if args.output_config else Path(f"{output_seed}.target.yaml")
        provenance_path = _output_path(args.provenance) if args.provenance else Path(f"{output_seed}.provenance.json")
        if not source_experiment.is_dir():
            raise TransferError(f"Source experiment does not exist: {source_experiment}")
        if not target_config_path.is_file():
            raise TransferError(f"Target config does not exist: {target_config_path}")
        if _within(output_seed, source_experiment):
            raise TransferError("Output seed must not be inside the source experiment")
        if _within(output_config, output_seed) or _within(provenance_path, output_seed):
            raise TransferError("Output config and provenance must be outside the seed directory so they cannot change its hash")
        if _within(output_config, source_experiment) or _within(provenance_path, source_experiment):
            raise TransferError("Output config and provenance must not be written into the source experiment")
        if not args.dry_run:
            _validate_output_paths([output_seed, output_config, provenance_path])

        source_config = _load_yaml(source_experiment / "config_snapshot.yaml", "source config snapshot")
        target_config = load_config(str(target_config_path))
        _ensure_mini_config(source_config, "Source experiment")
        _ensure_mini_config(target_config, "Target config")
        source_identity = _dataset_identity(source_config)
        target_identity = _dataset_identity(target_config)
        if source_identity == target_identity and not args.allow_same_dataset:
            raise TransferError("Source and target use the same dataset/task manifest; pass --allow-same-dataset to override")
        source_contract = _runtime_contract(source_config)
        target_contract = _runtime_contract(target_config)
        if source_contract != target_contract:
            differing = sorted(key for key in source_contract if source_contract[key] != target_contract.get(key))
            raise TransferError(
                "Source and target Mini runtime contracts differ; transfer is blocked to avoid changing both harness and evaluator: "
                + ", ".join(differing)
            )

        winner = _select_winner(source_experiment, args.source_winner)
        iteration = winner["iteration"]
        job_summary = _validate_completed_job(
            _score_job_dir(source_experiment, winner),
            winner,
            source_identity,
            allow_errored_source=args.allow_errored_source,
        )
        snapshot = _snapshot_for_iteration(source_experiment, iteration)
        _check_snapshot_filesystem(snapshot)
        snapshot_hash = _mini_source_tree_sha256(snapshot)
        workspace = source_experiment / "workspace"
        reference = _reference_before_iteration(workspace, iteration)
        source_seed = _resolve_project_path(source_config["source_config_dir"], "source source_config_dir")
        if not source_seed.is_dir():
            raise TransferError(f"Source H0 seed is missing: {source_seed}")

        with tempfile.TemporaryDirectory(prefix="ahe-mini-transfer-") as temporary_text:
            temporary = Path(temporary_text)
            reference_root = temporary / "reference"
            _archive_reference(workspace, reference, reference_root)
            reference_tree = reference_root / "tree"
            if _mini_source_tree_sha256(reference_tree) != snapshot_hash:
                raise TransferError(
                    "Winner snapshot does not match the Git state evaluated at that iteration; refuse to export an ambiguous harness"
                )

            checked_source = temporary / "checked-source"
            _run(["git", "clone", "--no-hardlinks", str(workspace), str(checked_source)])
            _run(["git", "checkout", "--detach", reference], cwd=checked_source)
            if _mini_source_tree_sha256(checked_source) != snapshot_hash:
                raise TransferError("Historical Git checkout and winner snapshot have different normalized source trees")
            source_report = _run_validator(source_config, checked_source, source_seed, "Source winner")

            staged_seed = temporary / "staged-seed"
            _safe_copy_seed(snapshot, staged_seed)
            staged_hash = _mini_source_tree_sha256(staged_seed)
            if staged_hash != snapshot_hash:
                raise TransferError("Copy filtering changed the winner harness tree")
            target_stage = temporary / "target-h0"
            _stage_as_new_h0(staged_seed, target_stage)
            target_report = _run_validator(target_config, target_stage, staged_seed, "Transferred target H0")

            provenance = {
                "transfer_kind": "cross_dataset_warm_start",
                "created_at": datetime.now(UTC).isoformat(),
                "source": {
                    "experiment": str(source_experiment),
                    "winner_selector": args.source_winner,
                    "iteration": iteration,
                    "evaluated_git_reference": reference,
                    "snapshot": str(snapshot),
                    "tree_sha256": snapshot_hash,
                    "score": {key: winner.get(key) for key in ("iteration", "pass_rate", "k", "pass_at", "tasks", "trials", "job_dir")},
                    "completed_job": job_summary,
                    "dataset_manifest": source_identity,
                },
                "target": {
                    "base_config": str(target_config_path),
                    "generated_config": str(output_config),
                    "seed": str(output_seed),
                    "seed_tree_sha256": staged_hash,
                    "dataset_manifest": target_identity,
                },
                "runtime_contract": source_contract,
                "validation": {
                    "source_winner": source_report,
                    "transferred_target_h0": target_report,
                },
                "semantics": {
                    "fresh_target_h0": True,
                    "rerun_target_baseline": True,
                    "copied_source_frontier": False,
                    "copied_source_scores": False,
                    "copied_source_task_manifest": False,
                    "copied_source_harbor_outputs": False,
                    "copied_source_git_history": False,
                    "source_errored_trial_waiver": bool(args.allow_errored_source),
                },
            }
            if args.dry_run:
                print(json.dumps({"status": "dry-run-valid", "provenance": provenance}, ensure_ascii=False, indent=2))
                return 0

            _safe_copy_seed(staged_seed, output_seed)
            output_hash = _mini_source_tree_sha256(output_seed)
            if output_hash != staged_hash:
                raise TransferError("Final seed copy changed the validated tree")
            overlay = _generated_overlay(target_config_path, output_seed, output_hash, source_experiment, iteration)
            output_config.parent.mkdir(parents=True, exist_ok=True)
            output_config.write_text(yaml.safe_dump(overlay, allow_unicode=True, sort_keys=False), encoding="utf-8")
            provenance["target"]["seed_tree_sha256"] = output_hash
            _write_json(provenance_path, provenance)

        print("Cross-dataset Mini-SWE warm-start seed created.")
        print(f"  Seed:       {output_seed}")
        print(f"  Config:     {output_config}")
        print(f"  Provenance: {provenance_path}")
        print("\nStart a new target experiment (it will create a fresh H0 and rerun the target baseline):")
        print(f"  {sys.executable} {PROJECT_DIR / 'evolve.py'} --config {output_config}")
        return 0
    except TransferError as error:
        print(f"cross-dataset transfer blocked: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
