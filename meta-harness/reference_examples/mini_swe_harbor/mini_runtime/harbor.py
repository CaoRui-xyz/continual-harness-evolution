"""AHE-aligned Harbor 0.8 command construction and execution."""

from __future__ import annotations

import os
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .contracts import ContractError, Suite, sha256_file, tree_sha256


def _required_file(value: str | Path, label: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_file():
        raise ContractError(f"{label} does not exist: {path}")
    return path


def _required_directory(value: str | Path, label: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_dir():
        raise ContractError(f"{label} does not exist: {path}")
    return path


def runtime_metadata(config: dict[str, Any]) -> dict[str, Any]:
    mini = config["mini_swe"]
    runner = _required_file(mini["harbor_runner_python"], "harbor_runner_python")
    harbor_source = _required_directory(mini["harbor_source_dir"], "harbor_source_dir")
    adapter_root = _required_directory(mini["ahe_root"], "ahe_root")
    bridge = harbor_source / "src" / "harbor" / "agents" / "installed" / "mini_swe_agent.py"
    adapter = adapter_root / "adapters" / "harbor_mini_swe_agent.py"
    e2b_adapter = adapter_root / "adapters" / "harbor_e2b_environment.py"
    if not bridge.is_file() or not adapter.is_file() or not e2b_adapter.is_file():
        raise ContractError("AHE/Harbor Mini host bridge or E2B adapter is missing")
    expected_commit = str(mini.get("harbor_source_commit", "")).strip()
    if expected_commit:
        completed = subprocess.run(["git", "-C", str(harbor_source), "rev-parse", "HEAD"], capture_output=True, text=True, check=False)
        actual_commit = completed.stdout.strip() if completed.returncode == 0 else ""
        if actual_commit != expected_commit:
            raise ContractError(f"Harbor source commit mismatch: expected {expected_commit}, got {actual_commit or 'unknown'}")
    probe = "import inspect; from harbor.agents.installed.mini_swe_agent import MiniSweAgent; from harbor.environments.e2b import E2BEnvironment; assert 'host_bridge' in inspect.getsource(MiniSweAgent); assert E2BEnvironment.type().value == 'e2b'"
    probe_env = os.environ.copy()
    probe_env["PYTHONPATH"] = str(harbor_source / "src") + os.pathsep + str(adapter_root) + (os.pathsep + probe_env["PYTHONPATH"] if probe_env.get("PYTHONPATH") else "")
    checked = subprocess.run([str(runner), "-c", probe], env=probe_env, capture_output=True, text=True, check=False)
    if checked.returncode:
        raise ContractError(f"AHE Harbor 0.8 Mini/E2B runtime preflight failed: {(checked.stderr or checked.stdout).strip()[-2000:]}")
    return {
        "runner_python": str(runner),
        "harbor_source_dir": str(harbor_source),
        "harbor_source_commit": str(mini.get("harbor_source_commit", "")),
        "ahe_root": str(adapter_root),
        "adapter": str(adapter),
        "e2b_adapter": str(e2b_adapter),
        "sandbox_timeout_seconds": int(mini["sandbox_timeout_seconds"]),
        "host_bridge_sha256": sha256_file(bridge),
        "candidate_adapter_sha256": sha256_file(adapter),
        "e2b_adapter_sha256": sha256_file(e2b_adapter),
    }


def runtime_fingerprint(config: dict[str, Any], runtime_lock_path: Path) -> dict[str, Any]:
    """Return every mutable external input that an evaluation actually uses.

    The controller writes these values into the run manifest before it creates
    H0.  Rechecking the same fingerprint immediately before and after Harbor
    makes a run fail closed if a shared AHE adapter, policy, evaluation lock,
    runner, or Harbor source changes underneath a frozen candidate.
    """
    import yaml

    mini = config["mini_swe"]
    runtime = runtime_metadata(config)
    runner = _required_file(mini["harbor_runner_python"], "harbor_runner_python")
    policy = _required_file(mini["workspace_policy"], "workspace_policy")
    evaluation_lock = _required_file(mini["evaluation_lock"], "evaluation_lock")
    runtime_lock = _required_file(runtime_lock_path, "runtime lock")
    lock = yaml.safe_load(runtime_lock.read_text(encoding="utf-8")) or {}
    max_sandboxes = int((lock.get("e2b") or {}).get("max_sandboxes", 0))
    seed = Path(config["source_config_dir"]).resolve()
    seed_tree = tree_sha256(
        seed,
        ignored_names={
            ".git", ".venv", ".env", "uv.lock", "__pycache__",
            ".ipynb_checkpoints", ".pytest_cache", ".mypy_cache",
            ".ruff_cache", ".idea", ".vscode", "dist", "build",
        },
    )
    return {
        **runtime,
        "runner_sha256": sha256_file(runner),
        "workspace_policy_sha256": sha256_file(policy),
        "evaluation_lock_sha256": sha256_file(evaluation_lock),
        "runtime_lock_sha256": sha256_file(runtime_lock),
        "mini_seed_tree_sha256": seed_tree,
        "max_sandboxes": max_sandboxes,
    }


def validate_runtime_lock(config: dict[str, Any], runtime_lock_path: Path) -> dict[str, Any]:
    """Verify the deployment against the versioned Mini/Harbor runtime lock."""
    import yaml

    lock = yaml.safe_load(runtime_lock_path.read_text(encoding="utf-8")) or {}
    if not isinstance(lock, dict):
        raise ContractError("runtime lock must be a YAML mapping")
    metadata = runtime_fingerprint(config, runtime_lock_path)
    expected_commit = str((lock.get("harbor") or {}).get("source_commit", ""))
    if expected_commit and metadata["harbor_source_commit"] != expected_commit:
        raise ContractError("evolution config Harbor commit disagrees with runtime lock")
    python = subprocess.run([metadata["runner_python"], "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"], capture_output=True, text=True, check=False)
    expected_python = str((lock.get("python") or {}).get("major_minor", ""))
    if python.returncode or python.stdout.strip() != expected_python:
        raise ContractError(f"Harbor runner must be Python {expected_python or 'declared runtime version'}")
    max_sandboxes = int((lock.get("e2b") or {}).get("max_sandboxes", 0))
    if max_sandboxes < 1:
        raise ContractError("runtime lock must declare a positive E2B capacity")
    if any(suite.n_concurrent > max_sandboxes for suite in [*config.get("_loaded_suites", [])]):
        raise ContractError("a suite exceeds the E2B concurrency capacity in runtime lock")
    metadata["max_sandboxes"] = max_sandboxes
    return metadata


def build_harbor_command(
    *,
    config: dict[str, Any],
    frozen_workspace: Path,
    suite: Suite,
    jobs_dir: Path,
) -> tuple[list[str], dict[str, str]]:
    """Use the exact Mini/E2B flag semantics already implemented by AHE."""
    runtime = runtime_metadata(config)
    lock_path = Path(config["mini_swe"]["evaluation_lock"]).resolve()
    lock = config["_evaluation_lock"]
    model = lock["model"]
    agent = lock["agent"]
    bridge = lock["harbor_runtime"]
    model_kwargs = model.get("model_kwargs", {})
    if not isinstance(model_kwargs, dict) or not model_kwargs.get("api_base"):
        raise ContractError("evaluation lock must fix model.model_kwargs.api_base")
    candidate_config = frozen_workspace / str(config["agent_config_filename"])
    workspace_src = frozen_workspace / "src"
    if not candidate_config.is_file() or not (workspace_src / "minisweagent").is_dir():
        raise ContractError("frozen Mini candidate lacks config or src/minisweagent")
    runner = runtime["runner_python"]
    command = [
        runner, "-m", "harbor.cli.main", "run",
        "--agent-import-path", "adapters.harbor_mini_swe_agent:CandidateMiniSweAgent",
        "--environment-import-path", "adapters.harbor_e2b_environment:AheE2BEnvironment",
        "--model", str(model["model_name"]),
        "--n-concurrent", str(suite.n_concurrent),
        "--n-attempts", str(suite.attempts),
        "--ak", "execution_mode=host_bridge",
        "--ak", f"mini_swe_agent_src={workspace_src}",
        "--ak", f"candidate_config_path={candidate_config}",
        "--ak", f"evaluation_lock_path={lock_path}",
        "--ak", f"step_limit={int(agent['step_limit'])}",
        "--ak", f"cost_limit={float(agent['cost_limit'])}",
        "--ak", f"command_timeout={int(bridge['command_timeout_seconds'])}",
        "--ak", f"api_base={model_kwargs['api_base']}",
        "--ek", f"sandbox_timeout_seconds={int(runtime['sandbox_timeout_seconds'])}",
        "--jobs-dir", str(jobs_dir),
        "--yes", "--max-retries", "0", "--no-force-build",
    ]
    if suite.source_kind == "harbor_dataset":
        command.extend(["--dataset", str(suite.dataset)])
    else:
        command.extend(["--path", str(Path(str(suite.path)).expanduser().resolve())])
    # Harbor's dataset filter can use qualified names (for example
    # `swe-bench/<instance>`), while Harbor's trial directory deliberately uses
    # the bare task basename. Suite keeps both identities so scoring never
    # mistakes a successful qualified dataset trial for a missing attempt.
    for task_name in suite.harbor_task_names:
        command.extend(["--include-task-name", task_name])
    env = os.environ.copy()
    paths = [str(Path(runtime["harbor_source_dir"]) / "src"), str(runtime["ahe_root"])]
    if env.get("PYTHONPATH"):
        paths.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(paths)
    provider_key_env = str(lock.get("model_api_key_env", "DEEPSEEK_API_KEY"))
    if env.get("LLM_API_KEY") and not env.get(provider_key_env):
        env[provider_key_env] = env["LLM_API_KEY"]
    return command, env


def _wait_for_finished_job(job_dir: Path, timeout_seconds: int, poll_seconds: float = 5.0) -> None:
    """Mirror the source/AHE Harbor completion contract, never score a snapshot.

    Harbor 0.8 may return from the CLI after scheduling the job while its own
    runner still writes trial artifacts.  The job's terminal `finished_at`
    field—not the launcher process exit—is the only accepted completion signal.
    """
    result_path = job_dir / "result.json"
    deadline = time.monotonic() + timeout_seconds
    last_state = "result.json not written"
    while time.monotonic() < deadline:
        if result_path.is_file():
            try:
                result = json.loads(result_path.read_text(encoding="utf-8"))
                stats = result.get("stats") or {}
                last_state = (
                    f"completed={stats.get('n_completed_trials')}, "
                    f"running={stats.get('n_running_trials')}, "
                    f"pending={stats.get('n_pending_trials')}, "
                    f"errored={stats.get('n_errored_trials')}"
                )
                if result.get("finished_at") is not None:
                    return
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
                last_state = f"unreadable result.json: {type(error).__name__}"
        time.sleep(poll_seconds)
    raise ContractError(
        f"Harbor job did not reach a terminal finished_at within {timeout_seconds}s ({last_state}); no partial score is accepted"
    )


def run_harbor(*, command: list[str], env: dict[str, str], jobs_dir: Path, timeout_seconds: int) -> Path:
    jobs_dir.mkdir(parents=True, exist_ok=True)
    before = {path.name for path in jobs_dir.iterdir() if path.is_dir()}
    try:
        completed = subprocess.run(command, env=env, cwd=jobs_dir.parent, timeout=timeout_seconds, check=False)
    except subprocess.TimeoutExpired as error:
        raise ContractError(f"Harbor job exceeded {timeout_seconds}s; no partial score is accepted") from error
    if completed.returncode:
        raise ContractError(f"Harbor exited {completed.returncode}; no partial score is accepted")
    created = sorted(path for path in jobs_dir.iterdir() if path.is_dir() and path.name not in before)
    if len(created) != 1:
        raise ContractError(f"expected exactly one Harbor job directory, found {len(created)}")
    _wait_for_finished_job(created[0], timeout_seconds)
    return created[0]
