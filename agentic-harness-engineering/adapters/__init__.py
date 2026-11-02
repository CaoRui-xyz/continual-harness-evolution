"""Lightweight paths and task selection shared by AHE and Meta-Harness."""

import os
from pathlib import Path
import re

REPO_ROOT = Path(__file__).resolve().parents[2]


def runtime_paths() -> dict[str, str]:
    """Internal paths follow this checkout; external dependencies are configurable."""
    runtime = Path(os.environ.get("HARNESS_RUNTIME_DIR", REPO_ROOT.parent / "runtime")).expanduser()
    defaults = {
        "REPO_ROOT": REPO_ROOT,
        "AHE_ROOT": REPO_ROOT / "agentic-harness-engineering",
        "DATASETS_ROOT": REPO_ROOT / "datasets",
        "MINI_SWE_SEED_DIR": REPO_ROOT / "mini-swe-agent-seed",
        "MINI_SWE_SEEDS_DIR": runtime / "mini-swe-agent-seeds",
        "META_HARBOR_SOURCE_DIR": runtime / "harbor",
        "META_HARBOR_RUNNER_PYTHON": runtime / "harbor-venv/bin/python",
        "AHE_PYTHON": runtime / "ahe-venv/bin/python",
    }
    return {key: str(Path(os.environ.get(key, str(value))).expanduser()) for key, value in defaults.items()}


def expand_runtime_paths(value: str) -> str:
    values = {**runtime_paths(), **os.environ}
    pattern = r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}|\$([A-Za-z_][A-Za-z0-9_]*)"
    def replace(match):
        name = match.group(1) or match.group(3)
        return values.get(name, match.group(2) if match.group(2) is not None else match.group(0))
    return os.path.expanduser(re.sub(pattern, replace, value))


def load_task_names(path: Path, tasks_dir: Path | None = None, expected_count: int | None = None) -> tuple[str, ...]:
    """Read literal task directory names, rejecting ambiguous Harbor glob filters."""
    names = tuple(line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    if not names or len(set(names)) != len(names):
        raise ValueError(f"Empty or duplicate task names: {path}")
    if any(not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", name) or name in {".", ".."} for name in names):
        raise ValueError(f"Task names must be literal directory basenames: {path}")
    if expected_count is not None and len(names) != expected_count:
        raise ValueError(f"Expected {expected_count} tasks, found {len(names)}: {path}")
    if tasks_dir is not None:
        for name in names:
            task = tasks_dir / name
            for required in ("task.toml", "instruction.md", "tests/test.sh"):
                if not (task / required).is_file():
                    raise ValueError(f"Missing task input: {task / required}")
            if not (task / "environment").is_dir():
                raise ValueError(f"Missing task environment: {task}")
    return names
