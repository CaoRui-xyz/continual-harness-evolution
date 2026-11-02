#!/usr/bin/env bash
# Evaluate one Mini-SWE workspace with the repository's Harbor/E2B bridge.
# This is evaluation-only: it never calls evolve.py or changes the workspace.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$PROJECT_ROOT/.." && pwd)"
RUNNER="${META_HARBOR_RUNNER_PYTHON:-${HARNESS_RUNTIME_DIR:-$REPO_ROOT/../runtime}/harbor-venv/bin/python}"
HARBOR_SOURCE_DIR="${META_HARBOR_SOURCE_DIR:-${HARNESS_RUNTIME_DIR:-$REPO_ROOT/../runtime}/harbor}"
DATASETS_ROOT="${DATASETS_ROOT:-$REPO_ROOT/datasets}"
LOCK_PATH="$PROJECT_ROOT/configs/mini_swe/evaluation_lock.yaml"

WORKSPACE=""
PRESET=""
DATASET=""
TASK_PATH=""
JOBS_DIR=""
ATTEMPTS=1
CONCURRENT=20
SANDBOX_TIMEOUT=3600
FORCE_BUILD=false
ALLOW_ALL=false
DRY_RUN=false
declare -a TASKS=()

usage() {
    cat <<'EOF'
Usage:
  scripts/evaluate_mini_swe_workspace_harbor.sh --workspace PATH \
    --preset {swebench-verified|swtbench-verified-paraphrase|featurebench} [options]

  scripts/evaluate_mini_swe_workspace_harbor.sh --workspace PATH \
    (--dataset HARBOR_DATASET | --path LOCAL_HARBOR_TASK_DIR) [--task TASK ... | --allow-all] [options]

Required:
  --workspace PATH       An experiment workspace containing src/minisweagent/ and
                         src/minisweagent/config/mini.yaml.

Frozen presets:
  swebench-random50      Harbor SWE-bench Verified fixed random-50 split.
  swtbench-random50      Local paraphrased SWTBench, 50 tasks.
  featurebench-random30  Local FeatureBench Fast E2B-eligible fixed random-30 split.

Dataset selection:
  --dataset NAME         Any Harbor registry dataset identifier.
  --path PATH            A local Harbor task or task-directory path.
  --task NAME            Include one task; may be supplied multiple times.
  --allow-all            Required for --dataset/--path without --task, to avoid
                         accidentally evaluating an entire dataset.

Execution options:
  --attempts N           Independent rollouts per included task (default: 1).
  --concurrent N         Harbor concurrent trials (default: 20).
  --jobs-dir PATH        Artifact directory; default is harbor_evaluations/ in
                         this repository, never inside the workspace.
  --sandbox-timeout N    E2B sandbox lifetime in seconds (default: 3600).
  --force-build          Rebuild E2B task templates instead of reusing aliases.
  --dry-run              Print the resolved Harbor command without evaluating.
  -h, --help             Show this help.

Examples:
  scripts/evaluate_mini_swe_workspace_harbor.sh \
    --workspace experiments/EXPERIMENT/workspace \
    --preset swtbench-random50 --attempts 2 --concurrent 20

  scripts/evaluate_mini_swe_workspace_harbor.sh \
    --workspace /path/to/workspace --dataset swtbench-verified@1.0 \
    --task django__django-11066
EOF
}

die() {
    printf 'error: %s\n' "$*" >&2
    exit 2
}

positive_integer() {
    [[ "$1" =~ ^[1-9][0-9]*$ ]]
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --workspace) WORKSPACE="${2:-}"; shift 2 ;;
        --preset) PRESET="${2:-}"; shift 2 ;;
        --dataset) DATASET="${2:-}"; shift 2 ;;
        --path) TASK_PATH="${2:-}"; shift 2 ;;
        --task) TASKS+=("${2:-}"); shift 2 ;;
        --attempts) ATTEMPTS="${2:-}"; shift 2 ;;
        --concurrent) CONCURRENT="${2:-}"; shift 2 ;;
        --jobs-dir) JOBS_DIR="${2:-}"; shift 2 ;;
        --sandbox-timeout) SANDBOX_TIMEOUT="${2:-}"; shift 2 ;;
        --force-build) FORCE_BUILD=true; shift ;;
        --allow-all) ALLOW_ALL=true; shift ;;
        --dry-run) DRY_RUN=true; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option: $1" ;;
    esac
done

[[ -n "$WORKSPACE" ]] || die "--workspace is required"
positive_integer "$ATTEMPTS" || die "--attempts must be a positive integer"
positive_integer "$CONCURRENT" || die "--concurrent must be a positive integer"
positive_integer "$SANDBOX_TIMEOUT" || die "--sandbox-timeout must be a positive integer"
[[ -x "$RUNNER" ]] || die "Harbor runner is not executable: $RUNNER"
[[ -d "$HARBOR_SOURCE_DIR/src" ]] || die "Harbor source directory is invalid: $HARBOR_SOURCE_DIR"
[[ -f "$LOCK_PATH" ]] || die "evaluation lock is missing: $LOCK_PATH"

WORKSPACE="$(cd "$WORKSPACE" && pwd -P)"
WORKSPACE_SRC="$WORKSPACE/src"
CANDIDATE_CONFIG="$WORKSPACE/src/minisweagent/config/mini.yaml"
[[ -d "$WORKSPACE_SRC/minisweagent" ]] || die "workspace lacks src/minisweagent: $WORKSPACE"
[[ -f "$CANDIDATE_CONFIG" ]] || die "workspace lacks candidate mini.yaml: $CANDIDATE_CONFIG"

if [[ -n "$PRESET" ]]; then
    [[ -z "$DATASET" && -z "$TASK_PATH" && ${#TASKS[@]} -eq 0 && "$ALLOW_ALL" == false ]] || die "--preset cannot be combined with --dataset, --path, --task, or --allow-all"
    case "$PRESET" in
        swebench-verified|swebench-random50) DATASET_NAME=swebench-verified; TASK_COUNT=50 ;;
        swtbench-verified-paraphrase|swtbench-random50) DATASET_NAME=swtbench-verified-paraphrase; TASK_COUNT=50 ;;
        featurebench|featurebench-random30) DATASET_NAME=featurebench; TASK_COUNT=30 ;;
        *) die "unknown preset: $PRESET" ;;
    esac
    TASK_PATH="$DATASETS_ROOT/$DATASET_NAME/tasks"
    NAMES_FILE="$DATASETS_ROOT/$DATASET_NAME/task_names.txt"
    TASK_TEXT="$(PYTHONPATH="$PROJECT_ROOT${PYTHONPATH:+:$PYTHONPATH}" "$RUNNER" - "$NAMES_FILE" "$TASK_PATH" "$TASK_COUNT" <<'PYTHON'
import sys
from pathlib import Path
from adapters import load_task_names
print("\n".join(load_task_names(Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3]))))
PYTHON
    )" || die "could not load task_names.txt"
    mapfile -t TASKS <<< "$TASK_TEXT"

else
    [[ -n "$DATASET" || -n "$TASK_PATH" ]] || die "provide --preset, --dataset, or --path"
    [[ -z "$DATASET" || -z "$TASK_PATH" ]] || die "--dataset and --path are mutually exclusive"
    if [[ -n "$TASK_PATH" ]]; then
        TASK_PATH="$(cd "$TASK_PATH" && pwd -P)"
    fi
    if [[ ${#TASKS[@]} -eq 0 && "$ALLOW_ALL" == false ]]; then
        die "custom --dataset/--path requires at least one --task or explicit --allow-all"
    fi
fi

if [[ -z "$JOBS_DIR" ]]; then
    LABEL="${PRESET:-${DATASET:-$(basename "$TASK_PATH")}}"
    LABEL="${LABEL//\//_}"
    LABEL="${LABEL//@/_}"
    JOBS_DIR="$PROJECT_ROOT/harbor_evaluations/${LABEL}-$(date -u +%Y%m%dT%H%M%SZ)"
else
    JOBS_DIR="$(mkdir -p "$JOBS_DIR" && cd "$JOBS_DIR" && pwd -P)"
fi

mapfile -t LOCK_VALUES < <("$RUNNER" - "$LOCK_PATH" <<'PY'
import sys
import yaml

lock = yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}
model = lock.get("model") or {}
agent = lock.get("agent") or {}
runtime = lock.get("harbor_runtime") or {}
api_base = (model.get("model_kwargs") or {}).get("api_base")
values = [model.get("model_name"), api_base, agent.get("step_limit"), agent.get("cost_limit"), runtime.get("command_timeout_seconds")]
if any(value is None or value == "" for value in values):
    raise SystemExit("evaluation lock lacks model, API base, agent, or Harbor runtime fields")
print("\n".join(str(value) for value in values))
PY
)
[[ ${#LOCK_VALUES[@]} -eq 5 ]] || die "could not load the evaluation lock"
MODEL_NAME="${LOCK_VALUES[0]}"
API_BASE="${LOCK_VALUES[1]}"
STEP_LIMIT="${LOCK_VALUES[2]}"
COST_LIMIT="${LOCK_VALUES[3]}"
COMMAND_TIMEOUT="${LOCK_VALUES[4]}"

ENV_FILE="${HARNESS_ENV_FILE:-$PROJECT_ROOT/.env}"
if [[ ! -f "$ENV_FILE" && -z "${HARNESS_ENV_FILE:-}" ]]; then
    ENV_FILE="$REPO_ROOT/.env"
fi
if [[ -f "$ENV_FILE" ]]; then
    # Values explicitly exported by the caller take precedence over the shared
    # project .env.  This lets one evaluation use another E2B account without
    # mutating credentials used by other running experiments.
    CALLER_E2B_API_KEY="${E2B_API_KEY-}"
    set -a
    # shellcheck disable=SC1091
    source "$ENV_FILE"
    set +a
    if [[ -n "$CALLER_E2B_API_KEY" ]]; then
        export E2B_API_KEY="$CALLER_E2B_API_KEY"
    fi
fi
if [[ -z "${DEEPSEEK_API_KEY:-}" && -n "${LLM_API_KEY:-}" ]]; then
    export DEEPSEEK_API_KEY="$LLM_API_KEY"
fi

declare -a COMMAND=(
    "$RUNNER" -m harbor.cli.main run
    --agent-import-path adapters.harbor_mini_swe_agent:CandidateMiniSweAgent
    --environment-import-path adapters.harbor_e2b_environment:AheE2BEnvironment
    --model "$MODEL_NAME"
    --n-concurrent "$CONCURRENT"
    --n-attempts "$ATTEMPTS"
    --ak execution_mode=host_bridge
    --ak "mini_swe_agent_src=$WORKSPACE_SRC"
    --ak "candidate_config_path=$CANDIDATE_CONFIG"
    --ak "evaluation_lock_path=$LOCK_PATH"
    --ak "step_limit=$STEP_LIMIT"
    --ak "cost_limit=$COST_LIMIT"
    --ak "command_timeout=$COMMAND_TIMEOUT"
    --ak "api_base=$API_BASE"
    --ek "sandbox_timeout_seconds=$SANDBOX_TIMEOUT"
    --jobs-dir "$JOBS_DIR"
    --yes --max-retries 0
)
if [[ -n "$DATASET" ]]; then
    COMMAND+=(--dataset "$DATASET")
else
    COMMAND+=(--path "$TASK_PATH")
fi
for task in "${TASKS[@]}"; do
    COMMAND+=(--include-task-name "$task")
done
if [[ "$FORCE_BUILD" == true ]]; then
    COMMAND+=(--force-build)
else
    COMMAND+=(--no-force-build)
fi

if [[ ${#TASKS[@]} -eq 0 ]]; then
    TASK_LABEL="all"
else
    TASK_LABEL="${#TASKS[@]}"
fi
printf 'Workspace: %s\n' "$WORKSPACE"
printf 'Source: %s\n' "${DATASET:-$TASK_PATH}"
printf 'Tasks: %s; attempts/task: %s; concurrency: %s\n' "$TASK_LABEL" "$ATTEMPTS" "$CONCURRENT"
printf 'Artifacts: %s\n' "$JOBS_DIR"
printf 'Harbor command:\n  '
printf '%q ' "${COMMAND[@]}"
printf '\n'

if [[ "$DRY_RUN" == true ]]; then
    exit 0
fi
[[ -n "${E2B_API_KEY:-}" ]] || die "E2B_API_KEY is not set (export it or add it to $PROJECT_ROOT/.env)"
[[ -n "${DEEPSEEK_API_KEY:-}" ]] || die "DEEPSEEK_API_KEY or LLM_API_KEY is not set"

mkdir -p "$JOBS_DIR"
export PYTHONPATH="$HARBOR_SOURCE_DIR/src:$PROJECT_ROOT${PYTHONPATH:+:$PYTHONPATH}"
exec "${COMMAND[@]}"
