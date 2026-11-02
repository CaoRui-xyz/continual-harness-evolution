#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$PROJECT_ROOT/.." && pwd)"
RUNNER="${AHE_PYTHON:-${HARNESS_RUNTIME_DIR:-$REPO_ROOT/../runtime}/ahe-venv/bin/python}"

usage() {
    cat <<'EOF'
用法: ./scripts/evolve.sh [选项] <config_file>

在 tmux 后台 session 中启动 Agentic Harness Engineering 进化实验。

参数:
  config_file              配置文件路径 (相对于 Agentic Harness Engineering/ 或绝对路径)

选项:
  --experiment NAME        恢复已有实验 (experiments/ 下的目录名)
  --start-iteration N      从第 N 轮开始 (默认 1)
  --skip-eval              跳过评测，直接用已有结果
  --session NAME           自定义 tmux session 名称
  --batch                  批量模式：启动 configs/experiments/ 下所有实验
  --attach                 启动后自动 attach 到 tmux session
  -h, --help               显示帮助

示例:
  ./scripts/evolve.sh configs/experiments/exp-simple-code-gpt54.yaml

  ./scripts/evolve.sh --experiment 2026-03-13__18-02-54__gpt54 --start-iteration 16 configs/experiments/exp-simple-code-gpt54.yaml

  ./scripts/evolve.sh --batch

  ./scripts/evolve.sh --attach configs/experiments/exp-simple-code-gpt54.yaml

管理 tmux session:
  tmux ls
  tmux attach -t <session>
  Ctrl-b d
  tmux kill-session -t <session>
EOF
}

EXPERIMENT=""
START_ITER=""
SKIP_EVAL=""
SESSION_NAME=""
BATCH_MODE=false
AUTO_ATTACH=false
CONFIG_FILE=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --experiment)
            EXPERIMENT="$2"; shift 2 ;;
        --start-iteration)
            START_ITER="$2"; shift 2 ;;
        --skip-eval)
            SKIP_EVAL="--skip-eval"; shift ;;
        --session)
            SESSION_NAME="$2"; shift 2 ;;
        --batch)
            BATCH_MODE=true; shift ;;
        --attach)
            AUTO_ATTACH=true; shift ;;
        -h|--help)
            usage; exit 0 ;;
        -*)
            echo "未知选项: $1"; usage; exit 1 ;;
        *)
            CONFIG_FILE="$1"; shift ;;
    esac
done

if ! command -v tmux &>/dev/null; then
    echo "错误: tmux 未安装。请先运行: brew install tmux"
    exit 1
fi

if [[ ! -x "$RUNNER" ]]; then
    echo "错误: Python 环境不存在: $RUNNER；请按仓库 README 配置 runtime，或设置 AHE_PYTHON。"
    exit 1
fi

if $BATCH_MODE; then
    CONFIGS_DIR="$PROJECT_ROOT/configs/experiments"
    if [[ ! -d "$CONFIGS_DIR" ]]; then
        echo "错误: 找不到配置目录 $CONFIGS_DIR"
        exit 1
    fi

    count=0
    for cfg in "$CONFIGS_DIR"/*.yaml; do
        name=$(basename "$cfg" .yaml | sed 's/^exp-[0-9]*-//')
        ts=$(date +%Y%m%d-%H%M)
        sess="ahe-${name}-${ts}"

        if tmux has-session -t "$sess" 2>/dev/null; then
            echo "[跳过] session '$sess' 已存在"
            continue
        fi

        printf -v cmd '%q ' "$RUNNER" "$PROJECT_ROOT/evolve.py" --config "$cfg"
        tmux new-session -d -s "$sess" -c "$PROJECT_ROOT" bash -c "$cmd"
        echo "[启动] $name -> tmux session '$sess'"
        count=$((count + 1))
    done

    echo ""
    echo "已启动 $count 个实验。"
    echo "查看: tmux ls"
    echo "进入: tmux attach -t <session>"
    exit 0
fi

if [[ -z "$CONFIG_FILE" ]]; then
    echo "错误: 请指定配置文件"
    usage
    exit 1
fi

if [[ ! "$CONFIG_FILE" = /* ]]; then
    if [[ -f "$SCRIPT_DIR/$CONFIG_FILE" ]]; then
        CONFIG_FILE="$SCRIPT_DIR/$CONFIG_FILE"
    elif [[ -f "$PROJECT_ROOT/$CONFIG_FILE" ]]; then
        CONFIG_FILE="$PROJECT_ROOT/$CONFIG_FILE"
    fi
fi

if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "错误: 配置文件不存在: $CONFIG_FILE"
    exit 1
fi

if [[ -z "$SESSION_NAME" ]]; then
    cfg_basename=$(basename "$CONFIG_FILE" .yaml | sed 's/^exp-[0-9]*-//')
    ts=$(date +%Y%m%d-%H%M)
    SESSION_NAME="ahe-${cfg_basename}-${ts}"
fi

if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
    echo "tmux session '$SESSION_NAME' 已存在。"
    echo "  进入: tmux attach -t $SESSION_NAME"
    echo "  终止: tmux kill-session -t $SESSION_NAME"
    exit 1
fi

CONFIG_FILE="$(cd "$(dirname "$CONFIG_FILE")" && pwd)/$(basename "$CONFIG_FILE")"
COMMAND=("$RUNNER" "$PROJECT_ROOT/evolve.py" --config "$CONFIG_FILE")

if [[ -n "$EXPERIMENT" ]]; then
    COMMAND+=(--experiment "$EXPERIMENT")
fi
if [[ -n "$START_ITER" ]]; then
    COMMAND+=(--start-iteration "$START_ITER")
fi
if [[ -n "$SKIP_EVAL" ]]; then
    COMMAND+=("$SKIP_EVAL")
fi
printf -v CMD '%q ' "${COMMAND[@]}"

CMD="$CMD; echo ''; echo '=== 实验结束 ==='; echo '按 Enter 关闭此 session'; read"

tmux new-session -d -s "$SESSION_NAME" -c "$PROJECT_ROOT" bash -c "$CMD"

echo "============================================"
echo "  Agentic Harness Engineering 实验已在 tmux 后台启动"
echo "============================================"
echo "  Session:  $SESSION_NAME"
echo "  Config:   $(basename "$CONFIG_FILE")"
[[ -n "$EXPERIMENT" ]] && echo "  恢复实验: $EXPERIMENT"
[[ -n "$START_ITER" ]] && echo "  起始轮次: $START_ITER"
echo ""
echo "  进入 session:  tmux attach -t $SESSION_NAME"
echo "  Detach 回后台: Ctrl-b d"
echo "  查看所有:      tmux ls"
echo "  终止实验:      tmux kill-session -t $SESSION_NAME"
echo "============================================"

if $AUTO_ATTACH; then
    exec tmux attach -t "$SESSION_NAME"
fi
