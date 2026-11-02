#!/usr/bin/env python3
"""Prepare E2B templates for any resolved Mini-SWE Harbor suite.

This is intentionally suite-driven rather than SWE/SWT-specific. It imports
the same `AheE2BEnvironment` used by rollout, keeps template build concurrency
within the runtime lock capacity, and never runs Mini or a verifier.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mini_runtime.contracts import ContractError, load_suite, sha256_file
from mini_runtime.harbor import validate_runtime_lock


async def _download_suite_tasks(suite, runtime: dict) -> list[Path]:
    if suite.source_kind == "local_path":
        path = Path(str(suite.path)).resolve()
        paths = [path] if (path / "task.toml").is_file() else [path / name for name in suite.task_names]
        if {task.name for task in paths} != set(suite.task_names):
            raise ContractError("local tasks do not match the suite selection")
        for task in paths:
            if not (task / "task.toml").is_file():
                raise ContractError(f"local suite task is missing: {task}")
        return paths
    from harbor.registry.client.factory import RegistryClientFactory
    from harbor.tasks.client import TaskClient

    metadata = await RegistryClientFactory.create().get_dataset_metadata(str(suite.dataset))
    task_ids = {task.get_name(): task for task in metadata.task_ids}
    missing = [name for name in suite.harbor_task_names if name not in task_ids]
    if missing:
        raise ContractError(f"resolved Harbor dataset does not contain suite tasks: {missing[:10]}")
    return (await TaskClient().download_tasks([task_ids[name] for name in suite.harbor_task_names])).paths


async def _prepare_one(task_path: Path, runtime: dict, semaphore: asyncio.Semaphore, force: bool) -> dict:
    from adapters.harbor_e2b_environment import AheE2BEnvironment
    from harbor.models.task.task import Task
    from harbor.models.trial.paths import TrialPaths

    async with semaphore:
        temp = Path(tempfile.mkdtemp(prefix="meta-mini-template-"))
        try:
            paths = TrialPaths(trial_dir=temp)
            paths.mkdir()
            task = Task(task_path)
            environment = AheE2BEnvironment(
                environment_dir=task.paths.environment_dir,
                environment_name=task.name,
                session_id="meta-harness-template-prebuild",
                trial_paths=paths,
                task_env_config=task.config.environment,
                sandbox_timeout_seconds=int(runtime["sandbox_timeout_seconds"]),
            )
            existed = await environment._does_template_exist()
            if force or not existed:
                await environment._create_template()
            if not await environment._does_template_exist():
                raise ContractError(f"E2B template not ready after build: {task.name}")
            return {
                "task_name": task.name,
                "template_alias": environment._template_name,
                "status": "rebuilt" if force else "reused" if existed else "built",
                "environment_dockerfile_sha256": sha256_file(task.paths.environment_dir / "Dockerfile") if (task.paths.environment_dir / "Dockerfile").is_file() else None,
                "e2b_adapter_sha256": runtime["e2b_adapter_sha256"],
            }
        finally:
            shutil.rmtree(temp, ignore_errors=True)


async def run(args: argparse.Namespace) -> dict:
    import meta_harness

    config = meta_harness.load_config(args.config.resolve())
    controller = meta_harness.EvolutionController(config, "template-preflight")
    # The preparation process itself imports the exact AHE adapters and Harbor
    # source used by the later subprocess, not whatever same-named package may
    # be installed in this Python environment.
    for import_root in (str(Path(controller.runtime["ahe_root"])), str(Path(controller.runtime["harbor_source_dir"]) / "src")):
        if import_root not in sys.path:
            sys.path.insert(0, import_root)
    suite_paths = [path.resolve() for path in args.suite] if args.suite else [meta_harness._config_path(str(item), args.config.resolve().parent) for item in config["suites"]]
    suites = [load_suite(path) for path in suite_paths]
    max_sandboxes = int(controller.runtime["max_sandboxes"])
    if any(suite.n_concurrent > max_sandboxes for suite in suites):
        raise ContractError(f"template preparation refuses suite concurrency above {max_sandboxes}")
    if not os.environ.get("E2B_API_KEY"):
        raise ContractError("E2B_API_KEY is required to query or build E2B templates")
    tasks_by_suite = [(suite, await _download_suite_tasks(suite, controller.runtime)) for suite in suites]
    semaphore = asyncio.Semaphore(args.template_concurrency)
    jobs = [_prepare_one(task, controller.runtime, semaphore, args.force_rebuild) for _suite, tasks in tasks_by_suite for task in tasks]
    records = [record async for record in _as_completed(jobs)]
    records.sort(key=lambda record: record["task_name"])
    return {
        "runtime_lock_sha256": controller.runtime["runtime_lock_sha256"],
        "suite_ids": [suite.suite_id for suite in suites],
        "template_count": len(records),
        "templates": records,
    }


async def _as_completed(jobs):
    for future in asyncio.as_completed(jobs):
        yield await future


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "configs" / "evolution" / "swebench.yaml")
    parser.add_argument("--suite", type=Path, action="append", default=[])
    parser.add_argument("--report", type=Path, default=ROOT / "runs" / "template-preparation.json")
    parser.add_argument("--template-concurrency", type=int, default=2)
    parser.add_argument("--force-rebuild", action="store_true")
    parser.add_argument("--check-only", action="store_true", help="validate runtime/suite configuration without E2B access")
    args = parser.parse_args()
    import meta_harness
    meta_harness.load_local_environment()
    if args.template_concurrency < 1:
        parser.error("--template-concurrency must be >= 1")
    try:
        if args.check_only:
            import meta_harness

            config = meta_harness.load_config(args.config.resolve())
            controller = meta_harness.EvolutionController(config, "template-check")
            suite_paths = [path.resolve() for path in args.suite] if args.suite else [meta_harness._config_path(str(item), args.config.resolve().parent) for item in config["suites"]]
            suites = [load_suite(path) for path in suite_paths]
            report = {"check_only": True, "runtime_lock_sha256": controller.runtime["runtime_lock_sha256"], "suites": [{"id": suite.suite_id, "tasks": len(suite.task_names), "n_concurrent": suite.n_concurrent} for suite in suites]}
        else:
            report = asyncio.run(run(args))
    except Exception as error:
        print(f"[prepare] failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.check_only:
        print(f"[prepare] runtime/suite check passed; report={args.report}")
    else:
        print(f"[prepare] ready templates: {report['template_count']}; report={args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
