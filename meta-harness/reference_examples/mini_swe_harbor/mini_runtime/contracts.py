"""Frozen suite/candidate contracts shared by Meta-Harness and Harbor evaluation.

The module deliberately has no Harbor import: all schema, identity and result
completeness checks remain executable before creating a remote E2B resource.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

import yaml
from adapters import expand_runtime_paths, load_task_names


class ContractError(ValueError):
    """A suite, manifest, or immutable evaluation input is invalid."""


def _expand_path(value: str, base: Path) -> Path:
    expanded = expand_runtime_paths(value)
    path = Path(expanded)
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def tree_sha256(root: Path, *, ignored_names: set[str] | None = None) -> str:
    """Hash a candidate snapshot without following links or trusting Git state."""
    # Evaluation tools and notebook integrations may create these caches while
    # a frozen candidate is being used.  They are already excluded when the
    # snapshot is copied; exclude them here as well so the pre/post evaluation
    # identity check hashes the same logical workspace.
    ignored = ignored_names or {
        ".git",
        "__pycache__",
        ".ipynb_checkpoints",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
    }
    digest = hashlib.sha256()
    for directory, directories, files in os.walk(root, topdown=True, followlinks=False):
        if Path(directory).name == "docs" and "site" in directories:
            ignored = set(ignored)
            ignored.add("site")
        directories[:] = sorted(name for name in directories if name not in ignored and not name.endswith(".egg-info"))
        directory_path = Path(directory)
        for name in sorted(name for name in files if name not in ignored and not name.endswith((".pyc", ".egg-info"))):
            path = directory_path / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                raise ContractError(f"candidate snapshot must not contain symlink: {relative}")
            if not path.is_file():
                raise ContractError(f"candidate snapshot contains non-regular file: {relative}")
            payload = path.read_bytes()
            executable = b"1" if path.stat().st_mode & 0o111 else b"0"
            digest.update(relative.encode("utf-8") + b"\0" + executable + b"\0")
            digest.update(str(len(payload)).encode("ascii") + b"\0" + payload)
    return digest.hexdigest()


@dataclass(frozen=True)
class Suite:
    suite_id: str
    source_kind: str
    dataset: str | None
    path: str | None
    manifest_path: str
    task_name_prefix: str
    task_names: tuple[str, ...]
    harbor_task_names: tuple[str, ...]
    attempts: int
    n_concurrent: int
    task_timeout_seconds: int
    sandbox_timeout_seconds: int
    metric: str
    solved_reward_threshold: float
    suite_sha256: str
    manifest_sha256: str


@dataclass(frozen=True)
class EvaluationContract:
    schema_version: int
    contract_id: str
    candidate_id: str
    candidate_tree_sha256: str
    candidate_snapshot: str
    suite: dict[str, Any]
    runtime: dict[str, Any]
    policy_sha256: str
    evaluation_lock_sha256: str
    adapter_sha256: dict[str, str]
    expected_trials: tuple[dict[str, Any], ...]

    def payload(self) -> dict[str, Any]:
        return asdict(self)


def suite_with_execution(
    suite: Suite,
    *,
    attempts: int | None = None,
    n_concurrent: int | None = None,
) -> Suite:
    """Return a Suite with CLI-selected execution settings and a new identity.

    Meta-Harness TB2 exposes search trials and concurrency as outer-loop CLI
    settings.  Mini keeps the frozen suite schema, but needs the same ability
    without leaving a stale ``suite_sha256`` in an EvaluationContract.
    """
    resolved_attempts = suite.attempts if attempts is None else attempts
    resolved_concurrency = suite.n_concurrent if n_concurrent is None else n_concurrent
    if not isinstance(resolved_attempts, int) or resolved_attempts < 1:
        raise ContractError("execution attempts must be a positive integer")
    if not isinstance(resolved_concurrency, int) or resolved_concurrency < 1:
        raise ContractError("n_concurrent must be a positive integer")
    normalized = {
        "schema_version": 1,
        "id": suite.suite_id,
        "source": {"kind": suite.source_kind, "dataset": suite.dataset, "path": suite.path},
        "manifest_sha256": suite.manifest_sha256,
        "task_name_prefix": suite.task_name_prefix,
        "task_names": list(suite.task_names),
        "harbor_task_names": list(suite.harbor_task_names),
        "execution": {
            "attempts": resolved_attempts,
            "n_concurrent": resolved_concurrency,
            "task_timeout_seconds": suite.task_timeout_seconds,
            "sandbox_timeout_seconds": suite.sandbox_timeout_seconds,
        },
        "scoring": {"metric": suite.metric, "solved_reward_threshold": suite.solved_reward_threshold},
    }
    return replace(
        suite,
        attempts=resolved_attempts,
        n_concurrent=resolved_concurrency,
        suite_sha256=sha256_bytes(canonical_json(normalized).encode()),
    )


def _load_mapping(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError as error:
        raise ContractError(f"cannot read YAML: {path}") from error
    if not isinstance(value, dict):
        raise ContractError(f"YAML must be a mapping: {path}")
    return value


def _manifest_task_names(manifest: dict[str, Any], path: Path) -> tuple[str, ...]:
    tasks = manifest.get("tasks")
    task_names = manifest.get("task_names")
    names: list[str]
    if isinstance(tasks, list) and tasks:
        names = []
        for item in tasks:
            if not isinstance(item, dict):
                raise ContractError(f"manifest task must be a mapping: {path}")
            name = item.get("instance_id", item.get("name"))
            if not isinstance(name, str) or not name:
                raise ContractError(f"manifest task lacks instance_id/name: {path}")
            names.append(name)
    elif isinstance(task_names, list) and task_names:
        if not all(isinstance(name, str) and name for name in task_names):
            raise ContractError(f"manifest task_names must be non-empty strings: {path}")
        names = list(task_names)
    else:
        raise ContractError(f"manifest must declare a non-empty tasks or task_names list: {path}")
    if len(set(names)) != len(names):
        raise ContractError(f"manifest contains duplicate task names: {path}")
    return tuple(names)


def load_suite(path: Path) -> Suite:
    """Load one generic Harbor suite without dataset-specific conditionals."""
    path = path.resolve()
    raw = _load_mapping(path)
    if int(raw.get("schema_version", 0)) != 1:
        raise ContractError(f"suite schema_version must be 1: {path}")
    suite_id = raw.get("id")
    source = raw.get("source")
    selection = raw.get("selection")
    execution = raw.get("execution")
    scoring = raw.get("scoring", {})
    if not isinstance(suite_id, str) or not suite_id:
        raise ContractError(f"suite id is required: {path}")
    if not isinstance(source, dict) or not isinstance(selection, dict) or not isinstance(execution, dict):
        raise ContractError(f"suite source/selection/execution must be mappings: {path}")
    kind = source.get("kind")
    if kind not in {"harbor_dataset", "local_path"}:
        raise ContractError(f"source.kind must be harbor_dataset or local_path: {path}")
    dataset = source.get("dataset") if kind == "harbor_dataset" else None
    local_path = source.get("path") if kind == "local_path" else None
    if kind == "harbor_dataset" and (not isinstance(dataset, str) or not dataset):
        raise ContractError(f"harbor_dataset suite requires source.dataset: {path}")
    if kind == "local_path" and (not isinstance(local_path, str) or not local_path):
        raise ContractError(f"local_path suite requires source.path: {path}")
    if kind == "local_path":
        # Local suites use the same portable ${AHE_ROOT:-...} convention as
        # their manifests.  Store a concrete path in the immutable contract so
        # Harbor never receives an unexpanded shell expression.
        local_path = str(_expand_path(local_path, path.parent))
    manifest_text = selection.get("task_names_file") or selection.get("manifest")
    if not isinstance(manifest_text, str) or not manifest_text:
        raise ContractError(f"suite requires selection.task_names_file: {path}")
    manifest_path = _expand_path(manifest_text, path.parent)
    if selection.get("task_names_file"):
        if selection.get("manifest"):
            raise ContractError("Choose task_names_file or manifest, not both")
        try:
            manifest_task_names = load_task_names(manifest_path, Path(local_path) if local_path else None)
        except (OSError, ValueError) as error:
            raise ContractError(str(error)) from error
    else:
        # Compatibility for other benchmarks (e.g. LiveCodeBench provenance).
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ContractError(f"cannot read JSON manifest: {manifest_path}") from error
        if not isinstance(manifest, dict):
            raise ContractError(f"manifest must be a mapping: {manifest_path}")
        manifest_task_names = _manifest_task_names(manifest, manifest_path)
    task_name_prefix = selection.get("task_name_prefix", "")
    if not isinstance(task_name_prefix, str):
        raise ContractError(f"selection.task_name_prefix must be a string: {path}")
    task_names = manifest_task_names
    requested = selection.get("task_names", [])
    excluded = selection.get("exclude_task_names", [])
    if not isinstance(requested, list) or not isinstance(excluded, list):
        raise ContractError(f"selection task_names/exclude_task_names must be lists: {path}")
    if requested:
        unknown = set(requested) - set(task_names)
        if unknown:
            raise ContractError(f"selection requests tasks absent from manifest: {sorted(unknown)}")
        task_names = tuple(name for name in task_names if name in set(requested))
    if excluded:
        unknown = set(excluded) - set(task_names)
        if unknown:
            raise ContractError(f"selection excludes tasks absent from selected manifest: {sorted(unknown)}")
        task_names = tuple(name for name in task_names if name not in set(excluded))
    expected_count = selection.get("expected_task_count", len(task_names))
    if not isinstance(expected_count, int) or expected_count != len(task_names) or not task_names:
        raise ContractError(f"selection expected_task_count does not match resolved tasks: {path}")
    attempts = execution.get("attempts")
    concurrency = execution.get("n_concurrent", 1)
    task_timeout = execution.get("task_timeout_seconds", 1800)
    sandbox_timeout = execution.get("sandbox_timeout_seconds", 3600)
    if not all(isinstance(value, int) and value > 0 for value in (attempts, concurrency, task_timeout, sandbox_timeout)):
        raise ContractError(f"execution attempts/concurrency/timeouts must be positive integers: {path}")
    if sandbox_timeout > 3600:
        raise ContractError("E2B sandbox timeout must be <= 3600 seconds")
    metric = scoring.get("metric", "mean_reward")
    if metric not in {"mean_reward", "fraction_solved"}:
        raise ContractError(f"unknown scoring metric: {metric}")
    threshold = scoring.get("solved_reward_threshold", 1.0)
    if not isinstance(threshold, (int, float)):
        raise ContractError("scoring.solved_reward_threshold must be numeric")
    # Local Harbor filters match directory basenames, even when task.toml
    # retains a qualified registry name that Harbor records in result.json.
    filter_prefix = task_name_prefix if kind == "harbor_dataset" else ""
    harbor_task_names = tuple(f"{filter_prefix}{name}" for name in task_names)
    normalized = {
        "schema_version": 1,
        "id": suite_id,
        "source": {"kind": kind, "dataset": dataset, "path": local_path},
        "manifest_sha256": sha256_file(manifest_path),
        "task_name_prefix": task_name_prefix,
        "task_names": list(task_names),
        "harbor_task_names": list(harbor_task_names),
        "execution": {"attempts": attempts, "n_concurrent": concurrency, "task_timeout_seconds": task_timeout, "sandbox_timeout_seconds": sandbox_timeout},
        "scoring": {"metric": metric, "solved_reward_threshold": float(threshold)},
    }
    return Suite(
        suite_id=suite_id,
        source_kind=kind,
        dataset=dataset,
        path=local_path,
        manifest_path=str(manifest_path),
        task_name_prefix=task_name_prefix,
        task_names=task_names,
        harbor_task_names=harbor_task_names,
        attempts=attempts,
        n_concurrent=concurrency,
        task_timeout_seconds=task_timeout,
        sandbox_timeout_seconds=sandbox_timeout,
        metric=metric,
        solved_reward_threshold=float(threshold),
        suite_sha256=sha256_bytes(canonical_json(normalized).encode()),
        manifest_sha256=sha256_file(manifest_path),
    )


def expected_trials(suite: Suite) -> tuple[dict[str, Any], ...]:
    return tuple(
        {"suite_id": suite.suite_id, "task_name": task_name, "attempt_index": attempt}
        for task_name in suite.task_names
        for attempt in range(1, suite.attempts + 1)
    )


def make_contract(
    *,
    candidate_id: str,
    candidate_tree: str,
    candidate_snapshot: Path,
    suite: Suite,
    runtime: dict[str, Any],
    policy_path: Path,
    evaluation_lock_path: Path,
    adapter_paths: list[Path],
) -> EvaluationContract:
    adapter_sha256 = {str(path.resolve()): sha256_file(path.resolve()) for path in adapter_paths}
    draft = {
        "schema_version": 1,
        "candidate_id": candidate_id,
        "candidate_tree_sha256": candidate_tree,
        "candidate_snapshot": str(candidate_snapshot.resolve()),
        "suite": asdict(suite),
        "runtime": runtime,
        "policy_sha256": sha256_file(policy_path.resolve()),
        "evaluation_lock_sha256": sha256_file(evaluation_lock_path.resolve()),
        "adapter_sha256": adapter_sha256,
        "expected_trials": expected_trials(suite),
    }
    contract_id = sha256_bytes(canonical_json(draft).encode())
    return EvaluationContract(contract_id=contract_id, **draft)


def write_contract(contract: EvaluationContract, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(contract.payload(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
