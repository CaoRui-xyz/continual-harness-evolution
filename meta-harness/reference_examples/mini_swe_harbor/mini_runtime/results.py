"""Strict Harbor result parsing for a predeclared task x attempt matrix."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .contracts import EvaluationContract


_TRIAL_SUFFIX = re.compile(r"__[A-Za-z0-9]{6,}$")


@dataclass(frozen=True)
class TrialRecord:
    task_name: str
    attempt_index: int
    status: str
    reward: float | None
    trial_dir: str | None
    reason: str | None


@dataclass(frozen=True)
class ScoreResult:
    contract_id: str
    complete: bool
    reason: str
    metric: str | None
    score: float | None
    expected_count: int
    observed_count: int
    trials: tuple[TrialRecord, ...]

    def payload(self) -> dict[str, Any]:
        return asdict(self)


def _task_name(trial_dir: Path) -> str:
    """Read Harbor's recorded task identity before falling back to its directory name.

    Harbor abbreviates long local task names when creating a trial directory.
    FeatureBench identifiers exceed that limit, so directory-name parsing would
    collapse distinct tasks (and make the strict task×attempt contract appear
    incomplete).  Each Harbor trial records the original identity in its
    result/config artifact; registry suites keep working with the legacy
    directory fallback when a nonterminal trial has not written either file.
    """
    for name in ("result.json", "config.json"):
        path = trial_dir / name
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        task_name = payload.get("task_name")
        if isinstance(task_name, str) and task_name:
            return task_name
        task = payload.get("task")
        if isinstance(task, dict):
            task_path = task.get("path")
            if isinstance(task_path, str) and task_path:
                return Path(task_path).name
        config = payload.get("config")
        if isinstance(config, dict):
            task = config.get("task")
            if isinstance(task, dict):
                task_path = task.get("path")
                if isinstance(task_path, str) and task_path:
                    return Path(task_path).name
    return _TRIAL_SUFFIX.sub("", trial_dir.name)


def _read_terminal_trial(trial_dir: Path) -> tuple[str, float | None, str | None] | None:
    result_path = trial_dir / "result.json"
    exception_path = trial_dir / "exception.txt"
    if result_path.exists():
        try:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
            verifier = payload.get("verifier_result") or {}
            reward = (verifier.get("rewards") or {}).get("reward")
            if reward is None:
                exception_info = payload.get("exception_info")
                if exception_info:
                    if isinstance(exception_info, dict):
                        reason = str(
                            exception_info.get("exception_message")
                            or exception_info.get("exception_type")
                            or "terminal Harbor exception"
                        )
                    else:
                        reason = str(exception_info)
                    # Harbor 0.8 writes a result artifact for failures that
                    # occur after the trial has begun (notably
                    # AgentTimeoutError), rather than an exception.txt file.
                    # It is terminal and semantically the same explicit zero
                    # as its exception.txt representation.
                    return "exception", 0.0, reason[-1000:]
                # A result file without a verifier reward is not a scored
                # terminal trial.  Treating it as an explicit zero would make
                # a crashed/incomplete Harbor job look complete and could
                # promote it to the frontier.
                return "invalid", None, "terminal result lacks verifier reward"
            return "scored", float(reward), None
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
            return "invalid", None, f"malformed terminal result: {type(error).__name__}"
    if exception_path.exists():
        return "exception", 0.0, exception_path.read_text(encoding="utf-8", errors="replace")[-1000:]
    return None


def score_job(contract: EvaluationContract, job_dir: Path) -> ScoreResult:
    """Score only when every contract-declared task/attempt has one terminal trial.

    A terminal Harbor exception is an explicit zero-reward trial. A missing,
    duplicate, corrupt directory, or task outside the contract makes the whole
    evaluation incomplete and prevents frontier promotion.
    """
    suite = contract.suite
    task_names = tuple(suite["task_names"])
    attempts = int(suite["attempts"])
    expected_count = len(task_names) * attempts
    observed: dict[str, list[tuple[Path, tuple[str, float | None, str | None]]]] = {name: [] for name in task_names}
    task_name_prefix = str(suite.get("task_name_prefix") or "")
    unexpected: list[str] = []
    if not job_dir.is_dir():
        return ScoreResult(contract.contract_id, False, "Harbor job directory is missing", None, None, expected_count, 0, ())
    for child in sorted(job_dir.iterdir()):
        if not child.is_dir() or "__" not in child.name:
            continue
        # An unexpected task directory is evidence that the Harbor job did
        # not implement the frozen suite contract, even when it has not yet
        # produced a result/exception file.  Do not silently discard it.
        task_name = _task_name(child)
        # The contract stores canonical manifest identities (for example the
        # bare ``astropy__astropy-14539``), while a Harbor registry trial can
        # record its qualified registry identity (``swe-bench/<instance>``) in
        # result.json.  Normalize only the prefix declared by this frozen
        # contract, and only when the stripped value is an expected task.  This
        # keeps extra registry tasks as strict ``unexpected`` failures rather
        # than accepting arbitrary path-like names.
        if task_name not in observed and task_name_prefix and task_name.startswith(task_name_prefix):
            canonical_task_name = task_name.removeprefix(task_name_prefix)
            if canonical_task_name in observed:
                task_name = canonical_task_name
        if task_name not in observed:
            unexpected.append(child.name)
            continue
        terminal = _read_terminal_trial(child)
        if terminal is None:
            # A known task with a nonterminal trial directory is incomplete.
            # Record it now so a later missing-count diagnostic cannot hide
            # the concrete directory that caused the contract failure.
            observed[task_name].append((child, ("missing", None, "nonterminal Harbor trial directory")))
            continue
        observed[task_name].append((child, terminal))
    records: list[TrialRecord] = []
    issues: list[str] = []
    if unexpected:
        issues.append(f"unexpected trial directories: {', '.join(unexpected)}")
    for task_name in task_names:
        task_trials = observed[task_name]
        if len(task_trials) != attempts:
            issues.append(f"{task_name} has {len(task_trials)}/{attempts} terminal attempts")
        for index in range(1, attempts + 1):
            if index <= len(task_trials):
                trial_dir, (status, reward, reason) = task_trials[index - 1]
                records.append(TrialRecord(task_name, index, status, reward, str(trial_dir), reason))
            else:
                records.append(TrialRecord(task_name, index, "missing", None, None, "missing terminal Harbor trial"))
    observed_count = sum(len(values) for values in observed.values())
    if issues:
        return ScoreResult(contract.contract_id, False, "; ".join(issues), None, None, expected_count, observed_count, tuple(records))
    invalid_records = [record for record in records if record.status in {"invalid", "missing"}]
    if invalid_records:
        reasons = "; ".join(
            f"{record.task_name} attempt {record.attempt_index}: {record.reason or 'invalid terminal result'}"
            for record in invalid_records
        )
        return ScoreResult(contract.contract_id, False, reasons, None, None, expected_count, observed_count, tuple(records))
    rewards = [record.reward for record in records]
    if any(reward is None for reward in rewards):
        return ScoreResult(contract.contract_id, False, "terminal trial has no reward", None, None, expected_count, observed_count, tuple(records))
    numeric_rewards = [float(reward) for reward in rewards if reward is not None]
    metric = str(suite["metric"])
    if metric == "mean_reward":
        score = sum(numeric_rewards) / len(numeric_rewards)
    elif metric == "fraction_solved":
        threshold = float(suite["solved_reward_threshold"])
        score = sum(reward >= threshold for reward in numeric_rewards) / len(numeric_rewards)
    else:
        return ScoreResult(contract.contract_id, False, f"unknown contract metric: {metric}", None, None, expected_count, observed_count, tuple(records))
    return ScoreResult(contract.contract_id, True, "complete", metric, score, expected_count, observed_count, tuple(records))


def write_score(score: ScoreResult, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(score.payload(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
