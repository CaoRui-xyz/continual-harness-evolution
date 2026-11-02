"""Meta-Harness evolution loop with the original TB2 search semantics.

The evolved object is a Mini-SWE-Agent workspace and its evaluator is Harbor
0.8/E2B, but the *search method* intentionally follows
``reference_examples/terminal_bench_2/meta_harness.py``:

    baseline -> propose -> validate -> smoke -> full benchmark -> frontier
    -> optional 5-trial full evaluation.

In particular, every proposal starts from the fixed H0 baseline.  A frontier
winner is evidence for the next proposer; it is never silently used as the
next candidate's source tree.  That distinction prevents this experiment from
turning the original independent-variant search into hill climbing.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

import proposer_backend

# Compatibility name for integrations/tests that patched the former Claude
# wrapper. The implementation is now the LiteLLM backend above.
claude_wrapper = proposer_backend
from mini_runtime.contracts import ContractError, Suite, load_suite, make_contract, sha256_file, suite_with_execution, tree_sha256, write_contract
from mini_runtime.harbor import build_harbor_command, run_harbor, runtime_fingerprint, validate_runtime_lock
from mini_runtime.results import ScoreResult, score_job, write_score
from mini_runtime.workspace import commit_candidate, create_candidate_worktree, create_h0, snapshot_workspace, validate_git_provenance, validate_with_ahe
from adapters import expand_runtime_paths, runtime_paths


ROOT = Path(__file__).parent.resolve()
RUNS = ROOT / "runs"
WORKSPACES = ROOT / "workspaces"
PROPOSER_ALLOWED_TOOLS = ["Read", "Glob", "Grep", "Agent", "Write", "Edit", "Bash"]
_INTERRUPTED = False


def load_local_environment() -> None:
    """Load ignored deployment credentials without putting them in artifacts."""
    dotenv_path = ROOT / ".env"
    # A caller may deliberately select another E2B account for one run.  Keep
    # that process-local choice while retaining the source TB2-compatible .env
    # loading behavior for every other setting (LLM credentials, API base,
    # GitHub token, and so on).  This avoids mutating the shared .env merely to
    # launch an isolated evaluation.
    caller_e2b_api_key = os.environ.get("E2B_API_KEY")
    if dotenv_path.is_file():
        # The source TB2 reference uses load_dotenv(override=True).
        load_dotenv(dotenv_path, override=True)
    if caller_e2b_api_key:
        os.environ["E2B_API_KEY"] = caller_e2b_api_key
    load_dotenv(Path(__file__).resolve().parents[3] / ".env", override=False)
    if os.environ.get("HARNESS_ENV_FILE"):
        load_dotenv(Path(os.environ["HARNESS_ENV_FILE"]).expanduser(), override=False)
    for key, value in runtime_paths().items():
        os.environ.setdefault(key, value)


def _on_signal(_signum: int, _frame: object) -> None:
    global _INTERRUPTED
    _INTERRUPTED = True
    print("\nInterrupted, finishing the current Harbor call without updating a partial frontier.", flush=True)


def _expand(value: str) -> str:
    return expand_runtime_paths(value)


def _config_path(value: str, base: Path) -> Path:
    candidate = Path(_expand(value))
    return candidate.resolve() if candidate.is_absolute() else (base / candidate).resolve()


def load_config(config_path: Path) -> dict[str, Any]:
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ContractError("evolution config must be a YAML mapping")
    for field_name in ("name", "source_config_dir", "agent_config_filename", "suites", "mini_swe"):
        if field_name not in raw:
            raise ContractError(f"evolution config lacks {field_name}")
    if not isinstance(raw["suites"], list) or not raw["suites"]:
        raise ContractError("evolution config suites must be non-empty")
    raw["source_config_dir"] = str(_config_path(str(raw["source_config_dir"]), config_path.parent))
    mini = raw["mini_swe"]
    if not isinstance(mini, dict):
        raise ContractError("mini_swe must be a mapping")
    for key in ("harbor_runner_python", "harbor_source_dir", "ahe_root", "evaluation_lock", "workspace_policy", "sandbox_timeout_seconds"):
        if key not in mini:
            raise ContractError(f"mini_swe lacks {key}")
    for key in ("harbor_runner_python", "harbor_source_dir", "ahe_root"):
        mini[key] = _expand(str(mini[key]))
    for key in ("evaluation_lock", "workspace_policy", "runtime_lock"):
        if key not in mini:
            continue
        mini[key] = str(_config_path(str(mini[key]), config_path.parent))
    lock_path = Path(mini["evaluation_lock"])
    lock = yaml.safe_load(lock_path.read_text(encoding="utf-8")) or {}
    if not isinstance(lock, dict) or not all(isinstance(lock.get(key), dict) for key in ("agent", "harbor_runtime", "model")):
        raise ContractError("evaluation lock must provide agent, harbor_runtime and model mappings")
    raw["_evaluation_lock"] = lock
    raw["_config_path"] = str(config_path.resolve())
    return raw


def _read_json(path: Path, default: Any) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # A killed process must leave either the previous checkpoint or the new
    # one, never a truncated state/frontier file that cannot be resumed.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _write_json(path: Path, payload: Any) -> None:
    _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _suite_manifest_payload(suites: tuple[Suite, ...]) -> list[dict[str, Any]]:
    """Use JSON-normalized suites both when writing and checking a run."""
    # Dataclass tuples become JSON lists. Normalizing once here avoids a false
    # resume rejection caused solely by comparing Python tuples to JSON lists.
    return json.loads(json.dumps([suite.__dict__ for suite in suites], ensure_ascii=False))


def _search_contract_payload(config: dict[str, Any], suites: tuple[Suite, ...]) -> dict[str, Any]:
    """Fields that select what a source-style search evaluates.

    The source loop exposes attempts/concurrency on the CLI.  Mini keeps that
    behavior, while also recording the candidate config pathname because the
    fixed Harbor bridge loads it directly rather than an AgentHarness import.
    """
    return {
        "agent_config_filename": str(config["agent_config_filename"]),
        "suites": _suite_manifest_payload(suites),
    }


def _search_scoring_identity(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the immutable scoring part of a recorded search contract.

    Source TB2 exposes Harbor concurrency as an outer CLI scheduling knob, so
    a resumed run may change it without changing the task set, rollout count,
    or score definition.  Keep the full requested contract in the manifest for
    auditability, but compare this identity when deciding whether an existing
    run can resume.
    """
    normalized = json.loads(json.dumps(payload, ensure_ascii=False))
    for suite in normalized.get("suites", []):
        if isinstance(suite, dict):
            suite.pop("n_concurrent", None)
            # ``suite_with_execution`` deliberately recomputes this digest
            # after applying the outer CLI concurrency override.  It is an
            # audit identity for the whole execution shape, not an input to
            # task/reward scoring, so omit the derived value alongside the
            # scheduling field itself.
            suite.pop("suite_sha256", None)
    return normalized


def _task_key(suite_id: str, task_name: str) -> str:
    """Namespace task names because generic Harbor suites may overlap."""
    return f"{suite_id}/{task_name}"


def parse_trial_metrics(job_dir: Path, suite_id: str) -> dict[str, list[dict[str, Any]]]:
    """Mini/Harbor counterpart to source TB2's per-trial metric parser."""
    per_task: dict[str, list[dict[str, Any]]] = {}
    for trial_dir in sorted(path for path in job_dir.iterdir() if path.is_dir() and "__" in path.name):
        result_path = trial_dir / "result.json"
        if not result_path.is_file():
            continue
        try:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
        agent_result = payload.get("agent_result") or {}
        verifier = payload.get("verifier_result") or {}
        api_calls = None
        trajectory_path = trial_dir / "agent" / "mini-swe-agent.trajectory.json"
        if trajectory_path.is_file():
            try:
                trajectory = json.loads(trajectory_path.read_text(encoding="utf-8"))
                api_calls = ((trajectory.get("info") or {}).get("model_stats") or {}).get("api_calls")
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pass
        task_name = str(payload.get("task_name") or trial_dir.name.rsplit("__", 1)[0])
        per_task.setdefault(_task_key(suite_id, task_name), []).append({
            "n_input_tokens": agent_result.get("n_input_tokens"),
            "n_output_tokens": agent_result.get("n_output_tokens"),
            "n_cache_tokens": agent_result.get("n_cache_tokens"),
            "cost_usd": agent_result.get("cost_usd"),
            "n_turns": api_calls,
            "n_api_calls": api_calls,
            "reward": (verifier.get("rewards") or {}).get("reward"),
        })
    return per_task


def summarize_trial_metrics(per_task: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Keep source TB2 summary fields with Mini's model-call measure."""
    costs: list[float] = []
    inputs: list[int] = []
    outputs: list[int] = []
    caches: list[int] = []
    turns: list[int] = []
    calls: list[int] = []
    task_summary: dict[str, dict[str, Any]] = {}
    for task_name, trials in per_task.items():
        task_costs = [float(item["cost_usd"]) for item in trials if item.get("cost_usd") is not None]
        task_turns = [int(item["n_turns"]) for item in trials if item.get("n_turns") is not None]
        task_calls = [int(item["n_api_calls"]) for item in trials if item.get("n_api_calls") is not None]
        task_summary[task_name] = {
            "mean_cost": round(sum(task_costs) / len(task_costs), 3) if task_costs else None,
            "mean_turns": round(sum(task_turns) / len(task_turns), 1) if task_turns else None,
            "mean_api_calls": round(sum(task_calls) / len(task_calls), 1) if task_calls else None,
            "n_trials": len(trials),
        }
        costs.extend(task_costs); turns.extend(task_turns); calls.extend(task_calls)
        inputs.extend(int(item["n_input_tokens"]) for item in trials if item.get("n_input_tokens") is not None)
        outputs.extend(int(item["n_output_tokens"]) for item in trials if item.get("n_output_tokens") is not None)
        caches.extend(int(item["n_cache_tokens"]) for item in trials if item.get("n_cache_tokens") is not None)
    return {
        "n_trials": sum(len(trials) for trials in per_task.values()),
        "total_cost_usd": round(sum(costs), 2) if costs else None,
        "mean_cost_usd": round(sum(costs) / len(costs), 3) if costs else None,
        "total_input_tokens": sum(inputs) if inputs else None,
        "total_output_tokens": sum(outputs) if outputs else None,
        "total_cache_tokens": sum(caches) if caches else None,
        "mean_turns": round(sum(turns) / len(turns), 1) if turns else None,
        "total_api_calls": sum(calls) if calls else None,
        "mean_api_calls": round(sum(calls) / len(calls), 1) if calls else None,
        "per_task": task_summary,
    }


@dataclass(frozen=True)
class CandidateOutcome:
    candidate_id: str
    candidate_commit: str
    candidate_tree_sha256: str
    complete: bool
    score: float | None
    detail: dict[str, Any]
    per_task: dict[str, float] = field(default_factory=dict)

    def payload(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "candidate_commit": self.candidate_commit,
            "candidate_tree_sha256": self.candidate_tree_sha256,
            "complete": self.complete,
            "score": self.score,
            "detail": self.detail,
            "per_task": self.per_task,
        }


@dataclass(frozen=True)
class PreparedCandidate:
    candidate_id: str
    candidate_commit: str
    candidate_tree_sha256: str
    snapshot: Path


class EvolutionController:
    """One isolated original-style Meta-Harness run for a Mini H0 baseline."""

    baseline_id = "H0"

    def __init__(
        self,
        config: dict[str, Any],
        run_name: str,
        *,
        search_attempts: int | None = None,
        search_concurrency: int | None = None,
        proposer_model: str | None = None,
        proposer_api_base: str | None = None,
        proposer_api_key_env: str | None = None,
        proposer_max_steps: int = 200,
    ) -> None:
        self.config = config
        self.run_name = run_name
        self.run_dir = RUNS / run_name
        self.repository = self.run_dir / "repository"
        self.candidates_dir = self.run_dir / "candidates"
        self.evaluations_dir = self.run_dir / "evaluations"
        self.contracts_dir = self.run_dir / "contracts"
        self.baselines_dir = self.run_dir / "baselines"
        self.state_path = self.run_dir / "state.json"
        self.frontier_path = self.run_dir / "frontier.json"
        self.summary_path = self.run_dir / "evolution_summary.jsonl"
        self.pending_path = self.run_dir / "pending_eval.json"
        self.iteration_path = self.run_dir / "iteration_in_progress.json"
        self.seed_dir = Path(config["source_config_dir"]).resolve()
        self.policy_path = Path(config["mini_swe"]["workspace_policy"]).resolve()
        self.lock_path = Path(config["mini_swe"]["evaluation_lock"]).resolve()
        self.runtime_python = Path(config["mini_swe"]["harbor_runner_python"])
        self.harbor_source = Path(config["mini_swe"]["harbor_source_dir"])
        locked_model = config["_evaluation_lock"]["model"]
        self.proposer_model = proposer_model or str(locked_model["model_name"])
        locked_kwargs = locked_model.get("model_kwargs") or {}
        self.proposer_api_base = proposer_api_base or locked_kwargs.get("api_base")
        self.proposer_api_key_env = proposer_api_key_env or (
            str(config["_evaluation_lock"].get("model_api_key_env", ""))
            if proposer_model is None
            else None
        )
        self.proposer_max_steps = proposer_max_steps
        config_base = Path(config["_config_path"]).parent
        loaded_suites = tuple(load_suite(_config_path(str(item), config_base)) for item in config["suites"])
        self.suites = tuple(
            suite_with_execution(suite, attempts=search_attempts, n_concurrent=search_concurrency)
            for suite in loaded_suites
        )
        smoke_text = config.get("smoke_suite")
        self.smoke_suite = load_suite(_config_path(str(smoke_text), config_base)) if smoke_text else None
        suite_ids = [suite.suite_id for suite in self.suites]
        if len(set(suite_ids)) != len(suite_ids):
            raise ContractError("evolution config contains duplicate suite ids")
        config["_loaded_suites"] = self.suites
        # A transfer run may use a distinct immutable seed while sharing the
        # same Harbor/E2B runtime.  Let its experiment config name a matching
        # runtime lock instead of mutating the shared H0 lock used by existing
        # runs.  Old configs deliberately retain the historical default.
        self.runtime_lock_path = Path(
            config["mini_swe"].get("runtime_lock", ROOT / "configs" / "runtime.lock.yaml")
        ).resolve()
        self.runtime = validate_runtime_lock(config, self.runtime_lock_path)

    def _assert_runtime_is_frozen(self) -> None:
        """Reject an evaluation if its shared runtime inputs drifted in place."""
        current = runtime_fingerprint(self.config, self.runtime_lock_path)
        if current != self.runtime:
            raise ContractError("Mini/Harbor/E2B runtime inputs changed after this run was initialized")

    def initialize(self) -> dict[str, Any]:
        if self.state_path.exists():
            state = _read_json(self.state_path, {})
            if int(state.get("schema_version", 0)) != 3:
                raise ContractError("existing run uses a pre-frozen-input state schema; start a fresh run")
            manifest = _read_json(self.run_dir / "run_manifest.json", {})
            requested_contract = _search_contract_payload(self.config, self.suites)
            recorded_contract = manifest.get("search_contract")
            if not isinstance(recorded_contract, dict) or _search_scoring_identity(recorded_contract) != _search_scoring_identity(requested_contract):
                raise ContractError("existing run task set or trial count differs from the requested search contract")
            if recorded_contract != requested_contract:
                history_path = self.run_dir / "resume_execution_overrides.jsonl"
                existing_rows = history_path.read_text(encoding="utf-8").splitlines() if history_path.is_file() else []
                row = {
                    "recorded_at": datetime.now().isoformat(),
                    "reason": "resume scheduling override",
                    "previous_search_contract": recorded_contract,
                    "requested_search_contract": requested_contract,
                }
                serialized = json.dumps(row, ensure_ascii=False, sort_keys=True)
                if serialized not in existing_rows:
                    with history_path.open("a", encoding="utf-8") as stream:
                        stream.write(serialized + "\n")
            if manifest.get("runtime") != self.runtime:
                raise ContractError("existing run runtime differs from the requested Harbor/Mini/E2B contract")
            if manifest.get("workspace_policy_sha256") != sha256_file(self.policy_path) or manifest.get("evaluation_lock_sha256") != sha256_file(self.lock_path):
                raise ContractError("existing run policy/evaluation lock differs from the requested contract")
            if str(state.get("candidate_base_commit", "")) != str(state.get("h0_commit", "")) or state.get("candidate_base_id") != self.baseline_id:
                raise ContractError("existing run violates the fixed-H0 candidate-base invariant")
            if not self.repository.is_dir():
                raise ContractError("existing run is missing its immutable H0 repository")
            if tree_sha256(self.repository) != str(state.get("h0_tree_sha256", "")):
                raise ContractError("existing run H0 repository contents differ from its recorded immutable baseline")
            return state
        self.run_dir.mkdir(parents=True, exist_ok=False)
        h0 = create_h0(self.seed_dir, self.repository)
        h0_tree = tree_sha256(self.repository)
        state = {
            "schema_version": 3,
            "run_name": self.run_name,
            "config_path": self.config["_config_path"],
            "h0_commit": h0,
            "h0_tree_sha256": h0_tree,
            # Kept explicit to make the no-hill-climbing invariant auditable.
            "candidate_base_commit": h0,
            "candidate_base_id": self.baseline_id,
            "next_iteration": 1,
            "created_at": datetime.now().isoformat(),
        }
        _write_json(self.state_path, state)
        _write_json(self.run_dir / "run_manifest.json", {
            "state": state,
            "search_contract": _search_contract_payload(self.config, self.suites),
            "runtime": self.runtime,
            "workspace_policy_sha256": sha256_file(self.policy_path),
            "evaluation_lock_sha256": sha256_file(self.lock_path),
            "search_method": "fixed-H0 independent variants; per-task plus overall frontier",
        })
        return state

    def fresh_start(self) -> None:
        """Source-style fresh reset, scoped to this isolated Mini run only.

        TB2's ``--fresh`` clears generated candidates and the run's logs. Mini
        candidates and their worktrees are already run-scoped, so deleting
        precisely these two generated roots is the equivalent operation; it
        never mutates the Mini seed or another run.
        """
        # This reset keeps H0 and its scoring contract. Reject incompatible
        # trials/tasks/runtime *before* deleting any candidate or search log.
        if self.run_dir.exists():
            self.initialize()
        # TB2's jobs directory is intentionally not cleared by --fresh. Keep
        # every Harbor artifact and Claude session for diagnosis, but remove
        # generated candidate source plus the source-style frontier/summary
        # files so no old candidate can be proposed/imported by fresh search.
        if self.candidates_dir.is_dir():
            for child in self.candidates_dir.iterdir():
                if child.name != self.baseline_id:
                    shutil.rmtree(child)
        for path in (
            self.summary_path,
            self.frontier_path,
            self.pending_path,
            self.iteration_path,
        ):
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
        workspace_root = WORKSPACES / self.run_name
        if workspace_root.exists():
            shutil.rmtree(workspace_root)
        if self.state_path.is_file():
            state = _read_json(self.state_path, {})
            state["next_iteration"] = 1
            _write_json(self.state_path, state)
        print(f"[fresh] cleared generated artifacts for run={self.run_name}", flush=True)

    def count_iterations(self) -> int:
        """Match TB2 resume semantics: highest recorded iteration plus one."""
        if not self.summary_path.is_file():
            return 0
        maximum = 0
        for line in self.summary_path.read_text(encoding="utf-8").splitlines():
            try:
                maximum = max(maximum, int(json.loads(line).get("iteration", 0)))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        return maximum

    def _prepare_candidate(self, workspace: Path, candidate_id: str) -> PreparedCandidate:
        """Validate then freeze a workspace before any Harbor process sees it."""
        state = _read_json(self.state_path, {})
        validate_git_provenance(workspace, str(state["h0_commit"]))
        validate_with_ahe(
            project_root=ROOT,
            workspace=workspace,
            policy_path=self.policy_path,
            evaluation_lock_path=self.lock_path,
            runtime_python=self.runtime_python,
            harbor_source_dir=self.harbor_source,
            seed_dir=self.seed_dir,
        )
        snapshot = self.candidates_dir / candidate_id / "workspace"
        if snapshot.exists():
            # Only H0 is a reusable baseline artifact. Source TB2 candidate
            # names identify distinct files; reusing one would overwrite or
            # ambiguously alias a previous variant.
            if candidate_id != self.baseline_id:
                raise ContractError(f"candidate name already exists in this run: {candidate_id}")
            if not snapshot.is_dir():
                raise ContractError(f"candidate snapshot is not a directory: {snapshot}")
            tree = tree_sha256(snapshot)
        else:
            tree = snapshot_workspace(workspace, snapshot)
        commit = subprocess_output(workspace, "rev-parse", "HEAD")
        _write_json(self.candidates_dir / candidate_id / "provenance.json", {
            "candidate_id": candidate_id,
            "candidate_commit": commit,
            "candidate_tree_sha256": tree,
            "h0_commit": state["h0_commit"],
            "candidate_base_commit": state["candidate_base_commit"],
            "candidate_base_id": self.baseline_id,
        })
        return PreparedCandidate(candidate_id, commit, tree, snapshot)

    # Compatibility entry point used by local integrations. The controller's
    # run path separates smoke from full benchmark below, just as TB2 does.
    def _validate_and_snapshot(self, workspace: Path, candidate_id: str) -> tuple[str, Path, str]:
        prepared = self._prepare_candidate(workspace, candidate_id)
        return prepared.candidate_commit, prepared.snapshot, prepared.candidate_tree_sha256

    def _per_task_scores(self, suites: tuple[Suite, ...], scores: list[ScoreResult]) -> dict[str, float]:
        result: dict[str, float] = {}
        for suite, score in zip(suites, scores, strict=True):
            by_task: dict[str, list[float]] = {task_name: [] for task_name in suite.task_names}
            for trial in score.trials:
                if trial.reward is not None:
                    by_task[trial.task_name].append(float(trial.reward))
            for task_name, rewards in by_task.items():
                # ``score.complete`` below guarantees exactly ``attempts``
                # terminal records. This is the generic equivalent of TB2's
                # per-task pass-rate calculation.
                if len(rewards) != suite.attempts:
                    continue
                if suite.metric == "fraction_solved":
                    value = sum(reward >= suite.solved_reward_threshold for reward in rewards) / len(rewards)
                else:
                    value = sum(rewards) / len(rewards)
                result[_task_key(suite.suite_id, task_name)] = value
        return result

    def _score_candidate_for_suites(
        self,
        candidate_id: str,
        commit: str,
        snapshot: Path,
        tree: str,
        suites: tuple[Suite, ...],
        *,
        evaluation_root: Path,
        contract_root: Path,
    ) -> CandidateOutcome:
        scores: list[ScoreResult] = []
        details: list[dict[str, Any]] = []
        if tree_sha256(snapshot) != tree:
            raise ContractError("frozen candidate snapshot changed before Harbor evaluation")
        self._assert_runtime_is_frozen()
        adapter_paths = [Path(self.runtime["adapter"]), Path(self.runtime["e2b_adapter"])]
        timeout_minutes = int(self.config.get("harbor_job_timeout_minutes", 0))
        for suite in suites:
            if tree_sha256(snapshot) != tree:
                raise ContractError("frozen candidate snapshot changed before a Harbor suite started")
            self._assert_runtime_is_frozen()
            contract = make_contract(
                candidate_id=candidate_id,
                candidate_tree=tree,
                candidate_snapshot=snapshot,
                suite=suite,
                runtime=self.runtime,
                policy_path=self.policy_path,
                evaluation_lock_path=self.lock_path,
                adapter_paths=adapter_paths,
            )
            contract_path = contract_root / f"{contract.contract_id}.json"
            write_contract(contract, contract_path)
            eval_root = evaluation_root / candidate_id / suite.suite_id
            command, environment = build_harbor_command(config=self.config, frozen_workspace=snapshot, suite=suite, jobs_dir=eval_root)
            # Match source TB2's outer Harbor default (8 hours). Individual
            # Mini configs may override it for a deliberately bounded smoke;
            # E2B's lower concurrency is a resource adaptation, not a new
            # task-count-times-timeout search policy.
            timeout = timeout_minutes * 60 if timeout_minutes > 0 else int(os.environ.get("HARBOR_TIMEOUT_SECONDS", 8 * 60 * 60))
            job_dir = run_harbor(command=command, env=environment, jobs_dir=eval_root, timeout_seconds=timeout)
            if tree_sha256(snapshot) != tree:
                raise ContractError("frozen candidate snapshot changed during Harbor evaluation")
            self._assert_runtime_is_frozen()
            score = score_job(contract, job_dir)
            write_score(score, eval_root / "score.json")
            scores.append(score)
            metrics = summarize_trial_metrics(parse_trial_metrics(job_dir, suite.suite_id))
            details.append({"suite_id": suite.suite_id, "contract": str(contract_path), "job_dir": str(job_dir), "score": score.payload(), "rollout_metrics": metrics})
        if not all(score.complete for score in scores):
            return CandidateOutcome(candidate_id, commit, tree, False, None, {"suites": details})
        # Original TB2 uses total successful trials / total trials, rather
        # than macro-averaging task scores. Retain that flat aggregation across
        # generic suites too.
        denominator = sum(score.expected_count for score in scores)
        aggregate = sum(float(score.score) * score.expected_count for score in scores if score.score is not None) / denominator
        return CandidateOutcome(candidate_id, commit, tree, True, aggregate, {"suites": details}, self._per_task_scores(suites, scores))

    def _score_candidate(self, prepared: PreparedCandidate, suites: tuple[Suite, ...] | None = None, *, evaluation_root: Path | None = None, contract_root: Path | None = None) -> CandidateOutcome:
        selected = self.suites if suites is None else suites
        return self._score_candidate_for_suites(
            prepared.candidate_id,
            prepared.candidate_commit,
            prepared.snapshot,
            prepared.candidate_tree_sha256,
            selected,
            evaluation_root=self.evaluations_dir if evaluation_root is None else evaluation_root,
            contract_root=self.contracts_dir if contract_root is None else contract_root,
        )

    def _smoke(self, prepared: PreparedCandidate) -> tuple[bool, dict[str, Any]]:
        if self.smoke_suite is None:
            return True, {"enabled": False}
        outcome = self._score_candidate(
            prepared,
            (self.smoke_suite,),
            evaluation_root=self.evaluations_dir / "smoke",
            contract_root=self.contracts_dir / "smoke",
        )
        # TB2 smoke is a crash/integration gate, not a reward gate. Its Harbor
        # 0.18 implementation accepts any terminal one-task result whose job
        # reports zero errors. Harbor 0.8 names that counter
        # ``n_errored_trials``. Do not require a reward: an unsolved but
        # healthy smoke task must proceed to the search benchmark.
        job_results: list[dict[str, Any]] = []
        for suite_detail in outcome.detail.get("suites", []):
            result_path = Path(str(suite_detail["job_dir"])) / "result.json"
            try:
                job_results.append(json.loads(result_path.read_text(encoding="utf-8")))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pass
        # ``score_job`` intentionally rejects a terminal result that lacks a
        # verifier reward, because such an artifact must never update the
        # search frontier.  TB2's smoke gate has a different purpose: it only
        # rejects a runtime crash.  Keep those contracts separate so an
        # unsolved but healthy one-task smoke can proceed to search.
        passed = len(job_results) == 1 and all(
            result.get("finished_at") is not None
            and int((result.get("stats") or {}).get("n_errored_trials", 0)) == 0
            for result in job_results
        )
        return passed, {"enabled": True, "passed": passed, "outcome": outcome.payload()}

    def evaluate_workspace(self, workspace: Path, candidate_id: str) -> CandidateOutcome:
        """Evaluate a workspace for callers that need the complete pipeline."""
        prepared = self._prepare_candidate(workspace, candidate_id)
        if candidate_id != self.baseline_id:
            smoke_ok, smoke = self._smoke(prepared)
            if not smoke_ok:
                return CandidateOutcome(candidate_id, prepared.candidate_commit, prepared.candidate_tree_sha256, False, None, {"smoke": smoke})
        return self._score_candidate(prepared)

    def _frontier(self) -> dict[str, Any]:
        return _read_json(self.frontier_path, {})

    def _update_frontier(self, outcomes: dict[str, CandidateOutcome]) -> dict[str, Any]:
        """Mirror TB2: independent per-task records plus one overall best."""
        frontier = self._frontier()
        for candidate_id, outcome in outcomes.items():
            if not outcome.complete or outcome.score is None:
                continue
            for task_name, pass_rate in outcome.per_task.items():
                current = float((frontier.get(task_name) or {}).get("pass_rate", -1))
                if pass_rate > current:
                    frontier[task_name] = {
                        "best_agent": candidate_id,
                        "pass_rate": pass_rate,
                        "candidate_commit": outcome.candidate_commit,
                        "candidate_tree_sha256": outcome.candidate_tree_sha256,
                    }
            best = frontier.get("_best") or {}
            if outcome.score > float(best.get("avg_pass_rate", -1)):
                frontier["_best"] = {
                    "agent": candidate_id,
                    "avg_pass_rate": outcome.score,
                    "candidate_commit": outcome.candidate_commit,
                    "candidate_tree_sha256": outcome.candidate_tree_sha256,
                }
        if frontier:
            _write_json(self.frontier_path, frontier)
        return frontier

    def _append_summary(
        self,
        iteration: int,
        candidate: dict[str, Any],
        outcome: CandidateOutcome,
        *,
        stage: str,
        propose_time: float | None = None,
        bench_time: float | None = None,
    ) -> None:
        # This intentionally uses the *current* frontier, after
        # ``_update_frontier()``, because that is exactly how the source
        # ``update_evolution_summary()`` computes its displayed delta.
        best_current = float((self._frontier().get("_best") or {}).get("avg_pass_rate", 0.0))
        # Source TB2 logs failed validation/crashed Harbor candidates as a
        # display score of 0.0. Keep Mini's official score as None (so it can
        # never reach _update_frontier), while presenting the same historical
        # signal to the next proposer.
        reported_score = 0.0 if outcome.score is None else outcome.score
        rollout_metrics = {
            suite["suite_id"]: suite.get("rollout_metrics")
            for suite in outcome.detail.get("suites", [])
            if suite.get("rollout_metrics") is not None
        }
        row = {
            "iteration": iteration,
            "agent": outcome.candidate_id,
            # The Mini adapter is fixed just like TB2's expected AgentHarness
            # class name; candidate identity is the frozen snapshot/commit.
            "import_path": "adapters.harbor_mini_swe_agent:CandidateMiniSweAgent",
            "candidate_commit": outcome.candidate_commit,
            "candidate_tree_sha256": outcome.candidate_tree_sha256,
            "candidate_snapshot": str(self.candidates_dir / outcome.candidate_id / "workspace"),
            "avg_pass_rate": round(reported_score, 3),
            "per_task": {task_name: round(value, 3) for task_name, value in outcome.per_task.items()},
            "hypothesis": str(candidate.get("hypothesis", "")),
            "changes": str(candidate.get("changes", "")),
            "expected_efficiency": str(candidate.get("expected_efficiency", "")),
            "stage": stage,
            "complete": outcome.complete,
            "delta": round(reported_score - best_current, 3) if best_current else None,
            "outcome": (
                f"{reported_score:.1%} ({reported_score - best_current:+.1%})"
                if reported_score > 0
                else "failed"
            ),
            "details": outcome.detail,
        }
        if propose_time is not None:
            row["timing_s"] = {
                "propose": round(propose_time, 1),
                "bench": round(bench_time, 1) if bench_time is not None else None,
            }
        if rollout_metrics:
            row["rollout_metrics"] = rollout_metrics
        previous = self.summary_path.read_text(encoding="utf-8") if self.summary_path.is_file() else ""
        if previous and not previous.endswith("\n"):
            previous += "\n"
        _atomic_write_text(self.summary_path, previous + json.dumps(row, ensure_ascii=False) + "\n")

    # Retain the narrow public helper used by state tests and integrations.
    # Unlike the previous implementation it never changes a future parent.
    def record(self, iteration: int, outcome: CandidateOutcome, hypothesis: str, changes: str) -> bool:
        before = self._frontier()
        old_best = float((before.get("_best") or {}).get("avg_pass_rate", -1))
        self._update_frontier({outcome.candidate_id: outcome})
        self._append_summary(iteration, {"hypothesis": hypothesis, "changes": changes}, outcome, stage="benchmark")
        return outcome.complete and outcome.score is not None and outcome.score > old_best

    def _propose(self, workspace: Path, iteration: int, timeout: int) -> dict[str, Any]:
        if self.pending_path.exists():
            self.pending_path.unlink()
        n_tasks = sum(len(suite.task_names) for suite in self.suites)
        attempts = ", ".join(f"{suite.suite_id}: {suite.attempts}" for suite in self.suites)
        prompt = (
            f"Run iteration {iteration} of the Mini-SWE-Agent scaffold evolution loop. "
            f"The controller created {workspace} from fixed H0.\n\n"
            f"## Eval split: {n_tasks} frozen Harbor task(s); attempts ({attempts})\n\n"
            "Read the frontier, previous summaries, validator reports, and successful and failed Mini trajectories under "
            f"{self.run_dir}. You may inspect prior frozen candidate snapshots there and deliberately copy an allowed-file mechanism into the provided H0 workspace, "
            "just as the source TB2 proposer may copy an earlier agent file; never import a prior candidate or modify a snapshot. "
            "Implement one general-purpose candidate.\n"
            f"Write pending_eval.json to: {self.pending_path}\n\n"
            f"The fixed rollout model is {self.config['_evaluation_lock']['model']['model_name']}. "
            "The outer controller owns validation, smoke testing, Harbor/E2B evaluation, scoring, and frontier selection. Do not run a benchmark."
        )
        result = claude_wrapper.run(
            prompt=prompt,
            model=self.proposer_model,
            api_base=self.proposer_api_base,
            api_key_env=self.proposer_api_key_env,
            allowed_tools=["bash"],
            skills=[str(ROOT / ".claude" / "skills" / "meta-harness-mini-swe-harbor")],
            cwd=str(workspace),
            log_dir=str(self.run_dir / "proposer_sessions"),
            name=f"iter{iteration}",
            timeout_seconds=timeout,
            max_steps=self.proposer_max_steps,
        )
        if result.exit_code:
            result.show()
            raise ContractError(f"proposer failed with exit code {result.exit_code}")
        result.show()
        payload = _read_json(self.pending_path, {})
        candidates = payload.get("candidates") if isinstance(payload, dict) else None
        # The shipped source TB2 skill requires one candidate per iteration.
        # A Mini candidate is a whole workspace, so one isolated workspace is
        # the exact corresponding physical representation.
        if not isinstance(candidates, list) or len(candidates) != 1 or not isinstance(candidates[0], dict):
            raise ContractError("proposer must declare exactly one Mini workspace candidate")
        candidate = candidates[0]
        # Source TB2 records these descriptive fields opportunistically via
        # ``dict.get``. The skill asks for all of them, but an otherwise legal
        # candidate must not be rejected merely because one annotation is
        # absent. Name validity is checked below because it becomes a snapshot
        # directory on the trusted host.
        if not isinstance(candidate.get("name"), str) or not candidate["name"].strip():
            raise ContractError("pending candidate lacks a non-empty name")
        return candidate

    def _baseline_outcome(self, state: dict[str, Any], *, allow_evaluation: bool = True) -> CandidateOutcome | None:
        cache_path = self.baselines_dir / f"{self.baseline_id}.json"
        if cache_path.is_file():
            cached = _read_json(cache_path, {})
            try:
                outcome = CandidateOutcome(
                    str(cached["candidate_id"]), str(cached["candidate_commit"]), str(cached["candidate_tree_sha256"]),
                    bool(cached["complete"]), cached.get("score"), dict(cached.get("detail") or {}), dict(cached.get("per_task") or {}),
                )
                snapshot = self.candidates_dir / self.baseline_id / "workspace"
                if outcome.candidate_id != self.baseline_id or not snapshot.is_dir():
                    raise ContractError("cached H0 baseline does not have its frozen snapshot")
                if outcome.candidate_commit != str(state["h0_commit"]) or outcome.candidate_tree_sha256 != tree_sha256(snapshot):
                    raise ContractError("cached H0 baseline does not match the frozen H0 identity")
                if outcome.complete and outcome.score is not None:
                    return outcome
            except (KeyError, TypeError, ValueError) as error:
                raise ContractError(f"invalid cached H0 baseline outcome: {cache_path}") from error
        if not allow_evaluation:
            return None
        prepared = self._prepare_candidate(self.repository, self.baseline_id)
        outcome = self._score_candidate(prepared)
        _write_json(cache_path, outcome.payload())
        return outcome

    def _invalid_outcome(self, candidate_id: str, workspace: Path, error: Exception, *, detail_key: str = "error") -> CandidateOutcome:
        commit = ""
        try:
            commit = subprocess_output(workspace, "rev-parse", "HEAD")
        except ContractError:
            pass
        return CandidateOutcome(candidate_id, commit, "", False, None, {detail_key: f"{type(error).__name__}: {error}"})

    def _archive_interrupted(self, path: Path) -> None:
        """Keep an unfinished staging copy for diagnosis before retrying H0."""
        archive_root = self.run_dir / "interrupted"
        archive_root.mkdir(parents=True, exist_ok=True)
        archive = Path(tempfile.mkdtemp(prefix=f"{path.name}-", dir=archive_root))
        shutil.move(str(path), str(archive / path.name))
        print(f"[resume] archived unfinished artifact: {archive / path.name}", flush=True)

    def _resume_candidate(self, candidate_id: str, expected: dict[str, Any] | None = None) -> PreparedCandidate:
        """Reuse only an intact frozen snapshot belonging to this run's H0."""
        state = _read_json(self.state_path, {})
        provenance = _read_json(self.candidates_dir / candidate_id / "provenance.json", {})
        identity = {key: provenance.get(key) for key in (
            "candidate_id", "candidate_commit", "candidate_tree_sha256",
        )}
        if (identity["candidate_id"] != candidate_id or not identity["candidate_commit"]
                or provenance.get("h0_commit") != state["h0_commit"]
                or provenance.get("candidate_base_commit") != state["h0_commit"]
                or provenance.get("candidate_base_id") != self.baseline_id
                or (expected is not None and identity != expected)):
            raise ContractError("interrupted candidate does not match its recorded H0 identity")
        snapshot = self.candidates_dir / candidate_id / "workspace"
        if not snapshot.is_dir() or tree_sha256(snapshot) != identity["candidate_tree_sha256"]:
            raise ContractError("interrupted candidate snapshot changed or is missing")
        return PreparedCandidate(candidate_id, identity["candidate_commit"], identity["candidate_tree_sha256"], snapshot)

    def run(self, iterations: int, propose_timeout: int, skip_baseline: bool, skip_smoke: bool = False) -> None:
        """Run source-style independent candidate iterations from H0."""
        state = self.initialize()
        # Source TB2 always seeds the frontier from any existing baseline
        # artifacts, even under --skip-baseline. The switch suppresses only a
        # fresh baseline run.
        baseline_cache = self.baselines_dir / f"{self.baseline_id}.json"
        if not skip_baseline or baseline_cache.is_file():
            try:
                baseline = self._baseline_outcome(state, allow_evaluation=not skip_baseline)
                if baseline is not None:
                    self._update_frontier({self.baseline_id: baseline})
                if not skip_baseline and baseline is not None:
                    print(f"[baseline] complete={baseline.complete} score={baseline.score}", flush=True)
            except ContractError as error:
                # The source loop records a failed baseline but continues with
                # independent candidates; it does not promote an empty result.
                print(f"[baseline] failed: {error}", file=sys.stderr, flush=True)

        start_iteration = self.count_iterations() + 1
        for offset in range(iterations):
            if _INTERRUPTED:
                return
            state = _read_json(self.state_path, {})
            iteration = start_iteration + offset
            staging_id = f"iter-{iteration:03d}-proposal"
            workspace = WORKSPACES / self.run_name / staging_id
            started = time.monotonic()
            candidate: dict[str, Any] = {}
            propose_time = None
            progress = _read_json(self.iteration_path, {})
            if progress and progress.get("iteration") != iteration:
                if int(progress.get("iteration", 0)) <= self.count_iterations():
                    # The summary was committed before checkpoint cleanup.
                    progress = {}
                else:
                    raise ContractError("unfinished iteration does not match the next summary iteration")
            if progress and progress.get("h0_commit") != state["h0_commit"]:
                raise ContractError("unfinished iteration belongs to a different H0")
            try:
                candidate = progress.get("candidate") or {}
                resuming_candidate = bool(candidate)
                if resuming_candidate:
                    propose_time = progress.get("propose_time")
                    print(f"[resume] continuing candidate {candidate.get('name')} at iteration {iteration}", flush=True)
                else:
                    # Old runs or a kill during proposal can leave a directory
                    # without a completed proposal. Preserve it, then retry
                    # from fixed H0 instead of overwriting it or blocking resume.
                    if workspace.exists():
                        self._archive_interrupted(workspace)
                    create_candidate_worktree(self.repository, staging_id, workspace, str(state["h0_commit"]))
                    propose_started = time.monotonic()
                    candidate = self._propose(workspace, iteration, propose_timeout)
                    propose_time = time.monotonic() - propose_started
                candidate_id = candidate.get("name")
                if not isinstance(candidate_id, str) or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", candidate_id) is None:
                    raise ContractError("candidate name must be a 1..64 character lowercase snake_case identifier")
                # Source order is proposal -> validation/smoke -> benchmark.
                # Mini needs a commit only to identify a frozen whole-workspace
                # artifact; _prepare_candidate immediately runs the single
                # authoritative validation before snapshotting or Harbor.
                candidate_root = self.candidates_dir / candidate_id
                if not resuming_candidate:
                    if candidate_root.exists():
                        raise ContractError(f"candidate name already exists in this run: {candidate_id}")
                    progress = {"iteration": iteration, "h0_commit": state["h0_commit"],
                                "candidate": candidate, "propose_time": propose_time}
                    # Record ownership before snapshot creation, so an
                    # interrupted copy can be distinguished from a duplicate.
                    _write_json(self.iteration_path, progress)
                if progress.get("prepared"):
                    prepared = self._resume_candidate(candidate_id, progress["prepared"])
                elif resuming_candidate and (candidate_root / "provenance.json").is_file():
                    # Snapshot/provenance finished just before the checkpoint.
                    prepared = self._resume_candidate(candidate_id)
                else:
                    if resuming_candidate and candidate_root.exists():
                        self._archive_interrupted(candidate_root)
                    commit_candidate(workspace, f"candidate: {candidate_id}")
                    prepared = self._prepare_candidate(workspace, candidate_id)
                progress["prepared"] = {
                    "candidate_id": prepared.candidate_id,
                    "candidate_commit": prepared.candidate_commit,
                    "candidate_tree_sha256": prepared.candidate_tree_sha256,
                }
                _write_json(self.iteration_path, progress)
                if not progress.get("outcome"):
                    if not skip_smoke and not progress.get("smoke_passed"):
                        smoke_ok, smoke_detail = self._smoke(prepared)
                        if not smoke_ok:
                            outcome = CandidateOutcome(candidate_id, prepared.candidate_commit, prepared.candidate_tree_sha256, False, None, {"smoke": smoke_detail})
                            progress.update(outcome=outcome.payload(), stage="smoke")
                        else:
                            progress["smoke_passed"] = True
                        _write_json(self.iteration_path, progress)
                    if not progress.get("outcome"):
                        bench_started = time.monotonic()
                        outcome = self._score_candidate(prepared)
                        progress.update(outcome=outcome.payload(), stage="benchmark", bench_time=time.monotonic() - bench_started)
                        _write_json(self.iteration_path, progress)
                outcome = CandidateOutcome(**progress["outcome"])
                if (outcome.candidate_id != prepared.candidate_id
                        or outcome.candidate_commit != prepared.candidate_commit
                        or outcome.candidate_tree_sha256 != prepared.candidate_tree_sha256):
                    raise ContractError("interrupted evaluation outcome does not match its frozen candidate")
                self._update_frontier({candidate_id: outcome})
                self._append_summary(iteration, candidate, outcome, stage=progress["stage"], propose_time=propose_time, bench_time=progress.get("bench_time"))
                print(f"[iteration {iteration}] complete={outcome.complete} score={outcome.score} elapsed={time.monotonic() - started:.1f}s", flush=True)
            except ContractError as error:
                candidate_id = str(candidate.get("name") or staging_id)
                outcome = self._invalid_outcome(candidate_id, workspace, error)
                # The source only records a row after parsing a proposal. A
                # proposer launch failure has no candidate row; a later
                # validation failure does, and is explicitly recorded as an
                # invalid Mini adaptation of its empty-results row.
                if candidate:
                    self._append_summary(
                        iteration,
                        candidate,
                        outcome,
                        stage="propose_or_validate",
                        propose_time=propose_time,
                    )
                print(f"[iteration {iteration}] invalid/incomplete: {error}", file=sys.stderr, flush=True)
            finally:
                # Keep unfinished candidates resumable until their summary is
                # durable. Failed proposals have no candidate to resume.
                if not candidate and workspace.exists():
                    self._archive_interrupted(workspace)
                if not candidate or self.count_iterations() >= iteration:
                    self.iteration_path.unlink(missing_ok=True)
                state = _read_json(self.state_path, {})
                state["next_iteration"] = self.count_iterations() + 1
                # Assert the invariant in the persisted run state as well as
                # the create_candidate_worktree call above.
                state["candidate_base_commit"] = state["h0_commit"]
                state["candidate_base_id"] = self.baseline_id
                _write_json(self.state_path, state)

    def full_evaluate(self) -> CandidateOutcome | None:
        """Original TB2 final phase: same search suite, winner, five trials."""
        frontier = self._frontier()
        best = frontier.get("_best") or {}
        winner = str(best.get("agent", ""))
        # Exactly as the source loop: H0 has already been measured, so the
        # optional expensive final pass is only for a non-baseline winner.
        if not winner or winner == self.baseline_id:
            print("[full-eval] no non-baseline frontier winner; skipped", flush=True)
            return None
        snapshot = self.candidates_dir / winner / "workspace"
        if not snapshot.is_dir():
            raise ContractError("frontier snapshot is missing; cannot perform full evaluation")
        # The source TB2 implementation has a fixed final five-trial pass.
        suites = tuple(suite_with_execution(suite, attempts=5) for suite in self.suites)
        outcome = self._score_candidate_for_suites(
            winner,
            str(best.get("candidate_commit", "")),
            snapshot,
            str(best.get("candidate_tree_sha256", "")),
            suites,
            evaluation_root=self.run_dir / "full_evaluations",
            contract_root=self.run_dir / "full_contracts",
        )
        _write_json(self.run_dir / "full_evaluation.json", {"attempts": 5, "outcome": outcome.payload()})
        print(f"[full-eval] winner={winner} complete={outcome.complete} score={outcome.score}", flush=True)
        return outcome


def subprocess_output(workspace: Path, *arguments: str) -> str:
    environment = os.environ.copy()
    environment["GIT_NO_REPLACE_OBJECTS"] = "1"
    completed = subprocess.run(["git", *arguments], cwd=workspace, env=environment, capture_output=True, text=True, check=False)
    if completed.returncode:
        raise ContractError((completed.stderr or completed.stdout).strip())
    return completed.stdout.strip()


def offline_evolution_test() -> None:
    """Provider-free complete-vs-partial scoring check."""
    from tempfile import TemporaryDirectory
    from mini_runtime.contracts import Suite, make_contract
    from mini_runtime.results import score_job

    with TemporaryDirectory(prefix="meta-mini-offline-") as temporary:
        root = Path(temporary)
        policy = root / "policy.yaml"; policy.write_text("allowed_paths: [x]\n", encoding="utf-8")
        lock = root / "lock.yaml"; lock.write_text("agent: {}\n", encoding="utf-8")
        adapter = root / "adapter.py"; adapter.write_text("# trusted\n", encoding="utf-8")
        snapshot = root / "candidate"; snapshot.mkdir(); (snapshot / "candidate.py").write_text("pass\n", encoding="utf-8")
        suite = Suite("offline", "local_path", None, str(root), str(root / "manifest.json"), "", ("alpha", "beta"), ("alpha", "beta"), 2, 4, 1, 1, "fraction_solved", 1.0, "suite", "manifest")
        contract = make_contract(candidate_id="offline", candidate_tree=tree_sha256(snapshot), candidate_snapshot=snapshot, suite=suite, runtime={"offline": True}, policy_path=policy, evaluation_lock_path=lock, adapter_paths=[adapter])
        job = root / "job"; job.mkdir()
        for task in suite.task_names:
            for attempt in range(2):
                trial = job / f"{task}__trial{attempt:01d}"; trial.mkdir()
                (trial / "result.json").write_text(json.dumps({"verifier_result": {"rewards": {"reward": 1}}}), encoding="utf-8")
        complete = score_job(contract, job)
        if not complete.complete or complete.score != 1.0:
            raise AssertionError(f"offline complete matrix failed: {complete}")
        shutil.rmtree(job / "beta__trial1")
        incomplete = score_job(contract, job)
        if incomplete.complete or incomplete.score is not None:
            raise AssertionError(f"partial matrix was incorrectly scored: {incomplete}")
    print("offline evolution test passed: complete matrix promoted; partial matrix rejected")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "configs" / "evolution" / "swebench.yaml")
    parser.add_argument("--run-name", default=None)
    parser.add_argument("--fresh", action="store_true", help="clear generated artifacts for this run before starting, matching TB2 --fresh")
    parser.add_argument("--iterations", type=int, default=5, help="number of evolution iterations (source TB2 default: 5)")
    parser.add_argument("--trials", type=int, default=2, help="trials per task during search (source TB2 default: 2)")
    parser.add_argument("--concurrent", type=int, default=20, help="Harbor concurrent trials; capped by configs/runtime.lock.yaml")
    parser.add_argument("--propose-timeout", type=int, default=2400)
    parser.add_argument("--proposer-model", default=None, help="Override the proposer model; default is the evaluation-lock model")
    parser.add_argument("--proposer-api-base", default=None, help="Override the proposer OpenAI-compatible API base")
    parser.add_argument("--proposer-api-key-env", default=None, help="Environment variable containing the proposer API key")
    parser.add_argument("--proposer-max-steps", type=int, default=200, help="Maximum proposer tool-loop turns")
    parser.add_argument("--skip-baseline", action="store_true", help="skip H0 execution; cached/no baseline frontier is allowed as in TB2")
    parser.add_argument("--skip-smoke", action="store_true", help="skip candidate smoke, matching TB2's optional switch")
    parser.add_argument("--full-eval", action="store_true", help="run the source-style optional five-trial evaluation of the non-baseline overall winner")
    parser.add_argument("--offline-evolution-test", action="store_true")
    args = parser.parse_args()
    load_local_environment()
    if args.offline_evolution_test:
        offline_evolution_test()
        return 0
    config = load_config(args.config.resolve())
    if args.iterations < 1:
        parser.error("--iterations must be >= 1")
    if args.trials < 1:
        parser.error("--trials must be >= 1")
    if args.concurrent < 1:
        parser.error("--concurrent must be >= 1")
    run_name = args.run_name or datetime.now().strftime("%Y%m%d_%H%M%S")
    controller = EvolutionController(
        config,
        run_name,
        search_attempts=args.trials,
        search_concurrency=args.concurrent,
        proposer_model=args.proposer_model,
        proposer_api_base=args.proposer_api_base,
        proposer_api_key_env=args.proposer_api_key_env,
        proposer_max_steps=args.proposer_max_steps,
    )
    if args.fresh:
        controller.fresh_start()
    controller.run(args.iterations, args.propose_timeout, args.skip_baseline, args.skip_smoke)
    if args.full_eval and not _INTERRUPTED:
        controller.full_evaluate()
    return 0


if __name__ == "__main__":
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)
    raise SystemExit(main())
