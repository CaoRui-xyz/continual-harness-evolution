"""AHE-aligned Mini workspace construction, H0 validation and snapshots."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from .contracts import ContractError, tree_sha256


_IGNORED = {".git", ".venv", ".env", "uv.lock", "__pycache__", ".ipynb_checkpoints", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".idea", ".vscode", "dist", "build"}


def _ignore(directory: str, names: list[str]) -> set[str]:
    ignored = {name for name in names if name in _IGNORED or name.endswith((".pyc", ".egg-info"))}
    if Path(directory).name == "docs" and "site" in names:
        ignored.add("site")
    return ignored


def _git(workspace: Path, *arguments: str) -> str:
    env = os.environ.copy()
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    completed = subprocess.run(["git", *arguments], cwd=workspace, env=env, capture_output=True, text=True, check=False)
    if completed.returncode:
        raise ContractError(f"git {' '.join(arguments)} failed: {(completed.stderr or completed.stdout).strip()[-1000:]}")
    return completed.stdout.strip()


def create_h0(seed_dir: Path, workspace: Path) -> str:
    """Copy the full AHE Mini seed and create exactly one immutable root commit."""
    if workspace.exists():
        raise ContractError(f"refusing to overwrite workspace: {workspace}")
    if not (seed_dir / "src" / "minisweagent").is_dir():
        raise ContractError(f"Mini seed does not contain src/minisweagent: {seed_dir}")
    shutil.copytree(seed_dir, workspace, ignore=_ignore, symlinks=True)
    if any(path.is_symlink() for path in workspace.rglob("*")):
        raise ContractError("Mini seed contains symlink; H0 must be an ordinary self-contained tree")
    _git(workspace, "init", "--initial-branch=main")
    _git(workspace, "config", "user.email", "meta-harness@local")
    _git(workspace, "config", "user.name", "Meta-Harness")
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-m", "H0: frozen Mini-SWE seed")
    return _git(workspace, "rev-parse", "HEAD")


def h0_commit(workspace: Path) -> str:
    roots = _git(workspace, "rev-list", "--max-parents=0", "HEAD").splitlines()
    if len(roots) != 1:
        raise ContractError("Mini candidate must have exactly one H0 root commit")
    return roots[0]


def validate_git_provenance(workspace: Path, expected_h0: str) -> str:
    """Reject history tricks before delegating detailed checks to AHE.

    AHE validates the actual filesystem and index flags. Meta-Harness owns the
    lineage relationship too, because it selects the next parent candidate.
    """
    replace_refs = _git(workspace, "for-each-ref", "--format=%(refname)", "refs/replace")
    if replace_refs:
        raise ContractError("candidate repository must not contain git replace refs")
    actual_h0 = h0_commit(workspace)
    if actual_h0 != expected_h0:
        raise ContractError(f"candidate H0 changed: expected {expected_h0}, got {actual_h0}")
    head = _git(workspace, "rev-parse", "HEAD")
    _git(workspace, "merge-base", "--is-ancestor", expected_h0, head)
    return head


def create_candidate_worktree(repository: Path, branch: str, path: Path, parent_commit: str) -> None:
    """Create an independent candidate repository from the current H0 lineage.

    This deliberately does *not* use `git worktree add`. AHE's authoritative
    validator correctly inspects the physical filesystem and expects `.git` to
    be a directory it can skip; linked worktrees use a `.git` file. They also
    omit ignored Mini seed fixtures which AHE's baseline-source compatibility
    check still requires. A full local copy keeps the exact H0 physical tree
    and a normal Git directory, while `checkout -B` preserves parent lineage.
    """
    if path.exists():
        raise ContractError(f"candidate worktree already exists: {path}")
    shutil.copytree(repository, path, symlinks=True)
    if any(item.is_symlink() for item in path.rglob("*")):
        raise ContractError("candidate repository copy contains a symlink")
    _git(path, "checkout", "-B", branch, parent_commit)


def commit_candidate(workspace: Path, message: str) -> str:
    status = _git(workspace, "status", "--porcelain=v1")
    if not status:
        # TB2 accepts a newly declared candidate even when it happens to be a
        # behavior-identical copy of the baseline. A Mini candidate is a whole
        # workspace rather than a new filename, so preserve that source
        # behavior by reusing the H0 commit while the controller still creates
        # a candidate-specific immutable snapshot and evaluation contract.
        return _git(workspace, "rev-parse", "HEAD")
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-m", message)
    return _git(workspace, "rev-parse", "HEAD")


def snapshot_workspace(workspace: Path, destination: Path) -> str:
    """Copy a validated candidate to an evaluation-only immutable source tree."""
    if destination.exists():
        raise ContractError(f"candidate snapshot already exists: {destination}")
    shutil.copytree(workspace, destination, ignore=_ignore, symlinks=True)
    if any(path.is_symlink() for path in destination.rglob("*")):
        raise ContractError("candidate snapshot contains symlink")
    return tree_sha256(destination)


def ahe_root_from_environment(project_root: Path) -> Path:
    configured = os.environ.get("AHE_ROOT")
    return Path(configured).expanduser().resolve() if configured else project_root.parents[2] / "agentic-harness-engineering"


def validate_with_ahe(
    *,
    project_root: Path,
    workspace: Path,
    policy_path: Path,
    evaluation_lock_path: Path,
    runtime_python: Path,
    harbor_source_dir: Path,
    seed_dir: Path,
) -> None:
    """Invoke AHE's one authoritative Mini validator with an identical contract.

    Keeping this invocation centralized prevents the Meta-Harness experiment from
    silently drifting from AHE's policy/runtime behavior while Mini support is
    still shared between the two repositories.
    """
    ahe_root = ahe_root_from_environment(project_root)
    validator = ahe_root / "agents" / "evolve_agent" / "skills" / "mini-swe-evolution-guide" / "scripts" / "validate_workspace.py"
    if not validator.is_file():
        raise ContractError(f"AHE Mini validator not found: {validator}; set AHE_ROOT")
    command = [
        str(runtime_python), str(validator),
        "--workspace", str(workspace),
        "--policy", str(policy_path),
        "--evaluation-lock", str(evaluation_lock_path),
        "--runtime-python", str(runtime_python),
        "--harbor-source-dir", str(harbor_source_dir),
        "--baseline-source", str(seed_dir),
        "--json",
    ]
    env = os.environ.copy()
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    completed = subprocess.run(command, cwd=project_root, env=env, capture_output=True, text=True, check=False)
    if completed.returncode:
        detail = (completed.stdout + "\n" + completed.stderr).strip()[-4000:]
        raise ContractError(f"AHE-aligned Mini workspace validation failed:\n{detail}")
