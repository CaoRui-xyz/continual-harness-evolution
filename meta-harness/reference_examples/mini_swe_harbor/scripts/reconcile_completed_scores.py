#!/usr/bin/env python3
"""Reconcile strict score artifacts for completed, frozen candidate jobs.

Use only when a scorer implementation bug is fixed after Harbor jobs already
finished.  The utility never changes a job, candidate snapshot, contract, or
raw Harbor result.  It backs up every rewritten derived artifact and rebuilds
the baseline/frontier/summary views from the repaired contract scores.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from meta_harness import EvolutionController, CandidateOutcome, _read_json, _write_json, load_config  # noqa: E402
from mini_runtime.contracts import ContractError, EvaluationContract  # noqa: E402
from mini_runtime.results import ScoreResult, score_job, write_score  # noqa: E402


def _contract_for_candidate(run_dir: Path, candidate_id: str) -> EvaluationContract:
    matches = []
    for path in sorted((run_dir / "contracts").glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("candidate_id") == candidate_id:
            matches.append(EvaluationContract(**payload))
    if len(matches) != 1:
        raise ContractError(f"expected exactly one frozen contract for {candidate_id}, found {len(matches)}")
    return matches[0]


def _job_dir_from_detail(detail: dict[str, Any]) -> Path:
    suites = detail.get("suites") if isinstance(detail, dict) else None
    if not isinstance(suites, list) or len(suites) != 1 or not isinstance(suites[0], dict):
        raise ContractError("reconciliation currently requires exactly one recorded suite detail")
    job_dir = suites[0].get("job_dir")
    if not isinstance(job_dir, str):
        raise ContractError("candidate detail lacks job_dir")
    result_path = Path(job_dir) / "result.json"
    if not result_path.is_file() or json.loads(result_path.read_text(encoding="utf-8")).get("finished_at") is None:
        raise ContractError(f"Harbor job is not terminal: {job_dir}")
    return Path(job_dir)


def _replace_score(detail: dict[str, Any], score: ScoreResult) -> dict[str, Any]:
    copied = json.loads(json.dumps(detail, ensure_ascii=False))
    copied["suites"][0]["score"] = score.payload()
    return copied


def _backup(paths: list[Path], destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    for path in paths:
        if path.exists():
            shutil.copy2(path, destination / path.name)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--run-name", required=True)
    parser.add_argument(
        "--iterations",
        nargs="+",
        type=int,
        required=True,
        help="Recorded benchmark iterations to reconcile, in historical order.",
    )
    args = parser.parse_args()

    config = load_config(args.config.resolve())
    controller = EvolutionController(config, args.run_name)
    state = controller.initialize()
    run_dir = controller.run_dir
    summary_path = controller.summary_path
    rows = [json.loads(line) for line in summary_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    rows_by_iteration = {int(row["iteration"]): row for row in rows}
    if len(rows_by_iteration) != len(rows):
        raise ContractError("summary has duplicate iteration numbers; refusing reconciliation")
    wanted = sorted(args.iterations)
    if wanted != args.iterations:
        raise ContractError("iterations must be supplied in ascending historical order")
    if any(iteration not in rows_by_iteration for iteration in wanted):
        raise ContractError("requested iteration is absent from summary")

    baseline_path = controller.baselines_dir / "H0.json"
    baseline = _read_json(baseline_path, {})
    if not isinstance(baseline, dict) or baseline.get("candidate_id") != "H0":
        raise ContractError("frozen H0 baseline cache is missing")
    backup_dir = run_dir / "reconciliations" / datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    _backup([baseline_path, controller.frontier_path, summary_path], backup_dir)

    candidates: list[tuple[int | None, str, str, str, dict[str, Any], dict[str, Any]]] = [
        (
            None,
            "H0",
            str(baseline["candidate_commit"]),
            str(baseline["candidate_tree_sha256"]),
            dict(baseline.get("detail") or {}),
            baseline,
        )
    ]
    for iteration in wanted:
        row = rows_by_iteration[iteration]
        candidates.append(
            (
                iteration,
                str(row["agent"]),
                str(row["candidate_commit"]),
                str(row["candidate_tree_sha256"]),
                dict(row.get("details") or {}),
                row,
            )
        )

    reconciled: list[tuple[int | None, CandidateOutcome, dict[str, Any] | None]] = []
    for iteration, candidate_id, commit, tree, detail, owner in candidates:
        contract = _contract_for_candidate(run_dir, candidate_id)
        job_dir = _job_dir_from_detail(detail)
        score = score_job(contract, job_dir)
        if not score.complete or score.score is None:
            raise ContractError(f"reconciliation refuses incomplete {candidate_id}: {score.reason}")
        score_path = job_dir.parent / "score.json"
        if score_path.exists():
            shutil.copy2(score_path, backup_dir / f"{candidate_id}.score.json")
        write_score(score, score_path)
        repaired_detail = _replace_score(detail, score)
        outcome = CandidateOutcome(
            candidate_id,
            commit,
            tree,
            True,
            float(score.score),
            repaired_detail,
            controller._per_task_scores(controller.suites, [score]),
        )
        reconciled.append((iteration, outcome, owner if isinstance(owner, dict) else None))

    # Rebuild the derived frontier deterministically in historical order.
    if controller.frontier_path.exists():
        controller.frontier_path.unlink()
    baseline_outcome = reconciled[0][1]
    baseline["complete"] = True
    baseline["score"] = baseline_outcome.score
    baseline["detail"] = baseline_outcome.detail
    baseline["per_task"] = baseline_outcome.per_task
    _write_json(baseline_path, baseline)
    controller._update_frontier({baseline_outcome.candidate_id: baseline_outcome})

    repaired_rows: list[dict[str, Any]] = []
    outcomes_by_iteration = {iteration: outcome for iteration, outcome, _ in reconciled if iteration is not None}
    for row in rows:
        iteration = int(row["iteration"])
        outcome = outcomes_by_iteration.get(iteration)
        if outcome is None:
            repaired_rows.append(row)
            continue
        controller._update_frontier({outcome.candidate_id: outcome})
        best_current = float((controller._frontier().get("_best") or {}).get("avg_pass_rate", 0.0))
        repaired = dict(row)
        repaired["complete"] = True
        repaired["avg_pass_rate"] = round(float(outcome.score), 3)
        repaired["per_task"] = {task: round(value, 3) for task, value in outcome.per_task.items()}
        repaired["details"] = outcome.detail
        repaired["delta"] = round(float(outcome.score) - best_current, 3) if best_current else None
        repaired["outcome"] = (
            f"{float(outcome.score):.1%} ({float(outcome.score) - best_current:+.1%})"
            if float(outcome.score) > 0
            else "failed"
        )
        repaired["reconciled_at"] = datetime.now(timezone.utc).isoformat()
        repaired_rows.append(repaired)
    summary_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in repaired_rows),
        encoding="utf-8",
    )
    _write_json(
        backup_dir / "reconciliation.json",
        {
            "run_name": args.run_name,
            "iterations": wanted,
            "reason": "strict scorer now normalizes only the frozen suite task_name_prefix",
            "scores": {outcome.candidate_id: outcome.score for _, outcome, _ in reconciled},
            "raw_harbor_artifacts_modified": False,
        },
    )
    print(json.dumps({"backup": str(backup_dir), "frontier": controller._frontier()}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
