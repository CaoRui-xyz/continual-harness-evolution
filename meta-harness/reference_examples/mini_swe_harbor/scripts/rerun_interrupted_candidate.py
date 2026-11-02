#!/usr/bin/env python3
"""Complete one interrupted frozen candidate before a normal Meta-Harness resume.

This utility intentionally does not reconstruct a candidate workspace or call
the proposer.  It evaluates the exact snapshot that had already passed
validation and smoke when an outer Harbor job was intentionally stopped to
change only scheduling concurrency.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from meta_harness import (  # noqa: E402
    EvolutionController,
    PreparedCandidate,
    _read_json,
    _write_json,
    load_config,
    load_local_environment,
)
from mini_runtime.contracts import ContractError, tree_sha256  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--run-name", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--iteration", type=int, required=True)
    parser.add_argument("--trials", type=int, required=True)
    parser.add_argument("--concurrent", type=int, required=True)
    args = parser.parse_args()

    load_local_environment()
    config = load_config(args.config.resolve())
    controller = EvolutionController(
        config,
        args.run_name,
        search_attempts=args.trials,
        search_concurrency=args.concurrent,
    )
    state = controller.initialize()

    if controller.count_iterations() != args.iteration - 1:
        raise ContractError(
            "refusing interrupted-candidate replay: summary does not end immediately "
            f"before iteration {args.iteration}"
        )

    candidate_root = controller.candidates_dir / args.candidate
    snapshot = candidate_root / "workspace"
    provenance = _read_json(candidate_root / "provenance.json", {})
    if not snapshot.is_dir() or not isinstance(provenance, dict):
        raise ContractError("frozen interrupted candidate snapshot/provenance is missing")
    tree = tree_sha256(snapshot)
    if tree != str(provenance.get("candidate_tree_sha256", "")):
        raise ContractError("interrupted candidate snapshot no longer matches its provenance hash")
    if str(provenance.get("candidate_base_commit", "")) != str(state.get("h0_commit", "")):
        raise ContractError("interrupted candidate was not derived from this run's H0")

    prepared = PreparedCandidate(
        args.candidate,
        str(provenance.get("candidate_commit", "")),
        tree,
        snapshot,
    )
    proposer_meta = _read_json(controller.run_dir / "proposer_sessions" / f"iter{args.iteration}" / "meta.json", {})
    proposer_time = proposer_meta.get("duration_seconds") if isinstance(proposer_meta, dict) else None
    replay_started = time.monotonic()
    outcome = controller._score_candidate(prepared)
    replay_time = time.monotonic() - replay_started

    # Preserve the previously declared candidate identity and explanatory
    # evidence.  The source proposal session is retained separately in its
    # original meta.json; this descriptor supplies the normal summary fields.
    candidate = {
        "name": args.candidate,
        "hypothesis": (
            "A general verification-and-completeness discipline at decisive "
            "test/submission moments reduces incomplete source fixes and "
            "regressions without changing the frozen evaluation contract."
        ),
        "changes": (
            "Replayed the exact frozen `exact_behavior_discipline` candidate "
            "after its original c4 Harbor job was intentionally stopped; only "
            "Harbor scheduling concurrency is changed to c20."
        ),
        "expected_efficiency": (
            "Same candidate, task manifest, rollout count, model, and scoring "
            "contract as the interrupted run; c20 changes scheduling only."
        ),
    }
    controller._update_frontier({args.candidate: outcome})
    controller._append_summary(
        args.iteration,
        candidate,
        outcome,
        stage="benchmark",
        propose_time=float(proposer_time) if isinstance(proposer_time, (int, float)) else None,
        bench_time=replay_time,
    )
    state["next_iteration"] = args.iteration + 1
    state["candidate_base_commit"] = state["h0_commit"]
    state["candidate_base_id"] = controller.baseline_id
    _write_json(controller.state_path, state)
    print(
        f"[interrupted-replay] iteration={args.iteration} candidate={args.candidate} "
        f"complete={outcome.complete} score={outcome.score}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
