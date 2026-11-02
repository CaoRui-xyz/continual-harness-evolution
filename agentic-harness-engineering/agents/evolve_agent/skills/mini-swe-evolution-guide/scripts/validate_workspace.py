"""Validate Mini-SWE-agent candidate scope, configuration, templates, and runtime imports."""

from __future__ import annotations

import argparse
import ast
import fnmatch
import hashlib
import json
import os
import signal
import stat
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, TemplateSyntaxError


_MISSING = object()
_RUNTIME_RESULT_MARKER = "__AHE_MINI_VALIDATOR_JSON__="
_GENERATED_PATH_PATTERNS = [
    "**/__pycache__/*.pyc",
    "**/*.pyc",
    ".pytest_cache/**",
    ".mypy_cache/**",
    ".ruff_cache/**",
]
_SENSITIVE_ENV_TOKENS = (
    "KEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "CREDENTIAL",
    "WEBHOOK",
)


@dataclass
class Issue:
    severity: str
    category: str
    message: str
    path: str | None = None


@dataclass
class Report:
    workspace: str
    issues: list[Issue] = field(default_factory=list)
    infrastructure_error: bool = False

    @property
    def is_valid(self) -> bool:
        return not any(issue.severity == "ERROR" for issue in self.issues)

    @property
    def error_count(self) -> int:
        return sum(1 for issue in self.issues if issue.severity == "ERROR")

    @property
    def warning_count(self) -> int:
        return sum(1 for issue in self.issues if issue.severity == "WARNING")

    def add(self, severity: str, category: str, message: str, path: str | None = None) -> None:
        self.issues.append(Issue(severity, category, message, path))

    def add_infrastructure_error(self, category: str, message: str) -> None:
        self.infrastructure_error = True
        self.add("ERROR", category, message)


@dataclass(frozen=True)
class FileEntry:
    mode: str
    object_id: str


def _matches(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in patterns)


def _git_bytes(workspace: Path, arguments: list[str]) -> bytes:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=workspace,
        capture_output=True,
        check=True,
    )
    return completed.stdout


def _git_text(workspace: Path, arguments: list[str]) -> str:
    return _git_bytes(workspace, arguments).decode("utf-8", errors="surrogateescape")


def _baseline_commit(workspace: Path) -> str:
    roots = [line.strip() for line in _git_text(workspace, ["rev-list", "--max-parents=0", "HEAD"]).splitlines() if line.strip()]
    if len(roots) != 1:
        raise RuntimeError("Mini workspace must have exactly one H0 root commit")
    return roots[0]


def _object_format(workspace: Path) -> str:
    value = _git_text(workspace, ["rev-parse", "--show-object-format"]).strip()
    if value not in {"sha1", "sha256"}:
        raise RuntimeError(f"Unsupported Git object format: {value or 'empty'}")
    return value


def _git_blob_hash(payload: bytes, object_format: str) -> str:
    digest = hashlib.new(object_format)
    digest.update(f"blob {len(payload)}\0".encode("ascii"))
    digest.update(payload)
    return digest.hexdigest()


def _baseline_entries(workspace: Path, baseline: str) -> dict[str, FileEntry]:
    entries: dict[str, FileEntry] = {}
    raw_records = _git_bytes(workspace, ["ls-tree", "-rz", "--full-tree", baseline]).split(b"\0")
    for raw_record in raw_records:
        if not raw_record:
            continue
        metadata, raw_path = raw_record.split(b"\t", 1)
        mode_bytes, object_type, object_id = metadata.split(b" ", 2)
        path = raw_path.decode("utf-8", errors="surrogateescape")
        if object_type != b"blob":
            raise RuntimeError(f"Unsupported H0 Git object at {path}: {object_type.decode(errors='replace')}")
        entries[path] = FileEntry(mode_bytes.decode("ascii"), object_id.decode("ascii"))
    return entries


def _filesystem_entry(path: Path, object_format: str) -> FileEntry | None:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode):
        payload = os.fsencode(os.readlink(path))
        return FileEntry("120000", _git_blob_hash(payload, object_format))
    if stat.S_ISREG(metadata.st_mode):
        payload = path.read_bytes()
        mode = "100755" if metadata.st_mode & 0o111 else "100644"
        return FileEntry(mode, _git_blob_hash(payload, object_format))
    return None


def _is_generated_path(path: str) -> bool:
    return _matches(path, _GENERATED_PATH_PATTERNS)


def _physical_entries(workspace: Path, object_format: str) -> tuple[dict[str, FileEntry], list[str]]:
    entries: dict[str, FileEntry] = {}
    special_paths: list[str] = []
    for root, directory_names, file_names in os.walk(workspace, topdown=True, followlinks=False):
        root_path = Path(root)
        relative_root = root_path.relative_to(workspace)
        if relative_root == Path("."):
            directory_names[:] = [name for name in directory_names if name != ".git"]
        for directory_name in list(directory_names):
            directory_path = root_path / directory_name
            if directory_path.is_symlink():
                relative_path = directory_path.relative_to(workspace).as_posix()
                entry = _filesystem_entry(directory_path, object_format)
                if entry is not None:
                    entries[relative_path] = entry
                directory_names.remove(directory_name)
        for file_name in file_names:
            absolute_path = root_path / file_name
            relative_path = absolute_path.relative_to(workspace).as_posix()
            if _is_generated_path(relative_path):
                continue
            entry = _filesystem_entry(absolute_path, object_format)
            if entry is None:
                special_paths.append(relative_path)
            else:
                entries[relative_path] = entry
    return entries, special_paths


def _source_entry(baseline_source: Path | None, relative_path: str, object_format: str) -> FileEntry | None:
    if baseline_source is None:
        return None
    relative_parts = Path(relative_path).parts
    if relative_path == "uv.lock" or any(part in {".git", ".venv", ".env", "__pycache__", ".ipynb_checkpoints", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".idea", ".vscode", "dist", "build"} or part.endswith(".egg-info") for part in relative_parts) or relative_path.startswith("docs/site/") or relative_path.endswith(".pyc"):
        return None
    source_path = baseline_source / relative_path
    if not source_path.exists() and not source_path.is_symlink():
        return None
    return _filesystem_entry(source_path, object_format)


def _source_paths(baseline_source: Path | None) -> set[str]:
    if baseline_source is None:
        return set()
    paths: set[str] = set()
    for root, directory_names, file_names in os.walk(baseline_source, topdown=True, followlinks=False):
        root_path = Path(root)
        directory_names[:] = [
            name
            for name in directory_names
            if name not in {".git", ".venv", "__pycache__", ".ipynb_checkpoints", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".idea", ".vscode", "dist", "build"}
            and not name.endswith(".egg-info")
            and not (root_path == baseline_source / "docs" and name == "site")
        ]
        for directory_name in list(directory_names):
            directory_path = root_path / directory_name
            if directory_path.is_symlink():
                paths.add(directory_path.relative_to(baseline_source).as_posix())
                directory_names.remove(directory_name)
        for file_name in file_names:
            source_path = root_path / file_name
            relative_path = source_path.relative_to(baseline_source).as_posix()
            if file_name in {".env", "uv.lock"}:
                continue
            if _is_generated_path(relative_path):
                continue
            paths.add(relative_path)
    return paths


def _actual_changed_paths(
    workspace: Path,
    baseline: str,
    baseline_source: Path | None,
) -> tuple[list[str], list[str], dict[str, FileEntry]]:
    object_format = _object_format(workspace)
    baseline_entries = _baseline_entries(workspace, baseline)
    actual_entries, special_paths = _physical_entries(workspace, object_format)
    changed: set[str] = set()
    for relative_path, baseline_entry in baseline_entries.items():
        if actual_entries.get(relative_path) != baseline_entry:
            changed.add(relative_path)
    for relative_path, actual_entry in actual_entries.items():
        if relative_path in baseline_entries:
            continue
        source_entry = _source_entry(baseline_source, relative_path, object_format)
        if source_entry != actual_entry:
            changed.add(relative_path)
    for relative_path in _source_paths(baseline_source) - set(baseline_entries) - set(actual_entries):
        changed.add(relative_path)
    changed.update(special_paths)
    return sorted(changed), sorted(special_paths), actual_entries


def _index_flag_issues(workspace: Path) -> tuple[list[str], list[str]]:
    assume_unchanged: list[str] = []
    for record in _git_bytes(workspace, ["ls-files", "-v", "-z"]).split(b"\0"):
        if not record:
            continue
        tag = chr(record[0])
        path = record[2:].decode("utf-8", errors="surrogateescape")
        if tag.isalpha() and tag.islower():
            assume_unchanged.append(path)
    skip_worktree: list[str] = []
    for record in _git_bytes(workspace, ["ls-files", "-t", "-z"]).split(b"\0"):
        if not record:
            continue
        tag = chr(record[0])
        path = record[2:].decode("utf-8", errors="surrogateescape")
        if tag == "S":
            skip_worktree.append(path)
    return sorted(set(assume_unchanged)), sorted(set(skip_worktree))


def _load_yaml_mapping_from_text(text: str, label: str) -> dict[str, Any]:
    payload = yaml.safe_load(text) or {}
    if not isinstance(payload, dict):
        raise RuntimeError(f"YAML root must be a mapping: {label}")
    return payload


def _load_yaml_mapping(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise RuntimeError(f"{label} does not exist: {path}")
    return _load_yaml_mapping_from_text(path.read_text(encoding="utf-8"), f"{label}: {path}")


def _baseline_yaml(workspace: Path, baseline: str, relative_path: str) -> dict[str, Any]:
    content = _git_text(workspace, ["show", f"{baseline}:{relative_path}"])
    return _load_yaml_mapping_from_text(content, f"H0:{relative_path}")


def _candidate_yaml(workspace: Path, relative_path: str) -> dict[str, Any]:
    candidate_path = workspace / relative_path
    if not candidate_path.is_file() or candidate_path.is_symlink():
        raise RuntimeError(f"Candidate Mini YAML must be a regular file: {relative_path}")
    return _load_yaml_mapping(candidate_path, "candidate Mini config")


def _field_value(payload: Mapping[str, Any], dotted_field: str) -> Any:
    current: Any = payload
    for part in dotted_field.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return _MISSING
        current = current[part]
    return current


def _recursive_merge(*payloads: Mapping[str, Any] | None) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for payload in payloads:
        if payload is None:
            continue
        for key, value in payload.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, Mapping):
                result[key] = _recursive_merge(result[key], value)
            elif isinstance(value, Mapping):
                result[key] = _recursive_merge(value)
            else:
                result[key] = value
    return result


def _resolved_config(candidate: dict[str, Any], evaluation_lock: dict[str, Any]) -> dict[str, Any]:
    agent_lock = evaluation_lock.get("agent", {})
    model_lock = evaluation_lock.get("model", {})
    harbor_runtime = evaluation_lock.get("harbor_runtime", {})
    if not isinstance(agent_lock, Mapping) or not isinstance(model_lock, Mapping) or not isinstance(harbor_runtime, Mapping):
        raise RuntimeError("evaluation lock agent/model/harbor_runtime must be mappings")
    model_kwargs = model_lock.get("model_kwargs", {})
    if not isinstance(model_kwargs, Mapping):
        raise RuntimeError("evaluation lock model.model_kwargs must be a mapping")
    runtime_payload = {
        "agent": {
            "mode": "yolo",
            "confirm_exit": False,
            "cost_limit": agent_lock.get("cost_limit", 0.0),
            "step_limit": agent_lock.get("step_limit", 0),
            "output_path": "/tmp/ahe-mini-validator-trajectory.json",
        },
        "environment": {
            "timeout": harbor_runtime.get("command_timeout_seconds", 0),
        },
        "model": {
            "model_name": model_lock.get("model_name", ""),
            "cost_tracking": "ignore_errors",
            "model_kwargs": ({"api_base": model_kwargs.get("api_base", "")} if model_kwargs.get("api_base") else {}),
        },
    }
    resolved = _recursive_merge(candidate, evaluation_lock, runtime_payload)
    resolved.pop("harbor_runtime", None)
    resolved.pop("model_api_key_env", None)
    return resolved


def _validate_policy(
    report: Report,
    workspace: Path,
    policy: Mapping[str, Any],
    baseline_source: Path | None,
) -> tuple[str, dict[str, Any] | None, str | None]:
    allowed = [str(item) for item in policy.get("allowed_paths", [])]
    forbidden = [str(item) for item in policy.get("forbidden_paths", [])]
    if not allowed:
        raise RuntimeError("workspace policy must declare non-empty allowed_paths")
    baseline = _baseline_commit(workspace)
    baseline_entries = _baseline_entries(workspace, baseline)
    changed_paths, special_paths, actual_entries = _actual_changed_paths(workspace, baseline, baseline_source)
    assume_unchanged, skip_worktree = _index_flag_issues(workspace)
    for relative_path in assume_unchanged:
        report.add("ERROR", "git_index", "tracked file has assume-unchanged flag", relative_path)
    for relative_path in skip_worktree:
        report.add("ERROR", "git_index", "tracked file has skip-worktree flag", relative_path)
    for relative_path in special_paths:
        report.add("ERROR", "filesystem", "special filesystem node is not allowed", relative_path)
    for relative_path in changed_paths:
        if _matches(relative_path, forbidden) or not _matches(relative_path, allowed):
            report.add("ERROR", "scope", "path changed outside the permitted Mini harness boundary", relative_path)
    fixed_seed_files = [str(item) for item in policy.get("fixed_seed_files", [])]
    for relative_path in fixed_seed_files:
        if relative_path not in baseline_entries:
            report.add("ERROR", "fixed_seed", "fixed Mini seed infrastructure file is absent from H0", relative_path)
        if relative_path in changed_paths:
            report.add("ERROR", "fixed_seed", "fixed Mini seed infrastructure file changed", relative_path)
    fixed_seed_markers = policy.get("fixed_seed_text_markers", {})
    if not isinstance(fixed_seed_markers, Mapping):
        raise RuntimeError("workspace policy fixed_seed_text_markers must be a mapping")
    for relative_path, raw_markers in fixed_seed_markers.items():
        marker_path = str(relative_path)
        if not isinstance(raw_markers, list) or not raw_markers or not all(isinstance(marker, str) and marker for marker in raw_markers):
            raise RuntimeError(f"workspace policy fixed seed markers must be non-empty strings: {marker_path}")
        try:
            baseline_text = _git_text(workspace, ["show", f"{baseline}:{marker_path}"])
        except subprocess.CalledProcessError:
            baseline_text = ""
        current_path = workspace / marker_path
        current_text = current_path.read_text(encoding="utf-8") if current_path.is_file() and not current_path.is_symlink() else ""
        for marker in raw_markers:
            if baseline_text.count(marker) != 1:
                report.add("ERROR", "fixed_seed", f"fixed Mini H0 must contain marker exactly once: {marker}", marker_path)
            if current_text.count(marker) != 1:
                report.add("ERROR", "fixed_seed", f"fixed Mini provider must contain marker exactly once: {marker}", marker_path)
    required_candidate_files = [str(item) for item in policy.get("required_candidate_files", [])]
    for relative_path in required_candidate_files:
        if relative_path not in baseline_entries:
            report.add("ERROR", "required_file", "required candidate tool protocol file is absent from H0", relative_path)
        required_path = workspace / relative_path
        if not required_path.is_file() or required_path.is_symlink():
            report.add("ERROR", "required_file", "required candidate tool protocol file is missing or is not a regular file", relative_path)
    for relative_path, entry in actual_entries.items():
        if entry.mode == "120000" and (_matches(relative_path, allowed) or relative_path in changed_paths):
            report.add("ERROR", "symlink", "symbolic links are not allowed in candidate-editable paths", relative_path)
    fields = [str(item) for item in policy.get("locked_yaml_fields", [])]
    yaml_paths = [str(item) for item in policy.get("locked_yaml_paths", [])]
    if not fields or not yaml_paths:
        raise RuntimeError("workspace policy must declare locked_yaml_fields and locked_yaml_paths")
    candidate_config_path = str(policy.get("candidate_config_path", "")).strip()
    if not candidate_config_path:
        raise RuntimeError("workspace policy must declare candidate_config_path")
    if candidate_config_path not in yaml_paths:
        raise RuntimeError("candidate_config_path must also appear in locked_yaml_paths")
    candidate_payload: dict[str, Any] | None = None
    for relative_path in yaml_paths:
        baseline_payload = _baseline_yaml(workspace, baseline, relative_path)
        current_payload = _candidate_yaml(workspace, relative_path)
        if relative_path == candidate_config_path:
            candidate_payload = current_payload
        for dotted_field in fields:
            if _field_value(baseline_payload, dotted_field) != _field_value(current_payload, dotted_field):
                report.add("ERROR", "locked_config", f"locked YAML field changed: {dotted_field}", relative_path)
    return baseline, candidate_payload, candidate_config_path


def _validate_python_syntax(report: Report, workspace: Path) -> None:
    source_directories = [
        workspace / "src" / "minisweagent" / "agents",
        workspace / "src" / "minisweagent" / "models" / "utils",
    ]
    for source_directory in source_directories:
        relative_directory = source_directory.relative_to(workspace).as_posix()
        if not source_directory.is_dir() or source_directory.is_symlink():
            report.add("ERROR", "python", "candidate Python directory is missing or is a symbolic link", relative_directory)
            continue
        for python_path in sorted(source_directory.rglob("*.py")):
            relative_path = python_path.relative_to(workspace).as_posix()
            if python_path.is_symlink():
                continue
            try:
                source = python_path.read_text(encoding="utf-8")
                ast.parse(source, filename=str(python_path))
            except UnicodeDecodeError as error:
                report.add("ERROR", "python_syntax", f"Python source is not UTF-8: {error}", relative_path)
            except SyntaxError as error:
                report.add("ERROR", "python_syntax", f"line {error.lineno}: {error.msg}", relative_path)
    agents_dir = source_directories[0]
    package_init_path = agents_dir / "__init__.py"
    if package_init_path.is_file() and not package_init_path.is_symlink():
        try:
            package_tree = ast.parse(package_init_path.read_text(encoding="utf-8"), filename=str(package_init_path))
            for node in package_tree.body:
                if not isinstance(node, ast.Assign):
                    continue
                if not any(isinstance(target, ast.Name) and target.id == "_AGENT_MAPPING" for target in node.targets):
                    continue
                if isinstance(node.value, ast.Dict) and any(
                    isinstance(key, ast.Constant) and isinstance(key.value, str) and key.value not in {"default", "interactive"}
                    for key in node.value.keys
                    if key is not None
                ):
                    report.add("WARNING", "ineffective_code", "custom _AGENT_MAPPING entries are ignored because the evaluated adapter imports DefaultAgent directly", package_init_path.relative_to(workspace).as_posix())
                break
        except (SyntaxError, UnicodeDecodeError):
            pass


def _validate_import_time_side_effects(report: Report, workspace: Path) -> None:
    dangerous_exact_calls = {
        "__import__", "builtins.__import__", "builtins.compile", "builtins.eval", "builtins.exec", "builtins.open",
        "compile", "eval", "exec", "open", "importlib.import_module", "os.getenv", "os.putenv", "os.system", "os.unsetenv",
        "pathlib.Path.open", "pathlib.Path.read_bytes", "pathlib.Path.read_text", "pathlib.Path.write_bytes", "pathlib.Path.write_text",
        "urllib.request.urlopen",
    }
    safe_os_path_calls = {
        "os.path.basename", "os.path.commonpath", "os.path.commonprefix", "os.path.dirname", "os.path.isabs",
        "os.path.join", "os.path.normcase", "os.path.normpath", "os.path.split", "os.path.splitdrive", "os.path.splitext",
    }
    dangerous_module_prefixes = ("httpx.", "requests.", "socket.", "subprocess.")
    dangerous_terminal_methods = {
        "exists", "glob", "is_dir", "is_file", "iterdir", "listdir", "lstat", "mkdir", "open", "read_bytes", "read_text",
        "readlink", "rename", "replace", "rglob", "rmdir", "scandir", "stat", "touch", "unlink", "write_bytes", "write_text",
    }

    def qualified_name(node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            prefix = qualified_name(node.value)
            return f"{prefix}.{node.attr}" if prefix else node.attr
        if isinstance(node, ast.Call):
            return qualified_name(node.func)
        return ""

    class ImportTimeVisitor(ast.NodeVisitor):
        def __init__(self, import_aliases: Mapping[str, str], local_functions: Mapping[str, ast.FunctionDef | ast.AsyncFunctionDef], local_lambdas: Mapping[str, ast.Lambda]) -> None:
            self.calls: list[tuple[int, str]] = []
            self.import_aliases = import_aliases
            self.local_functions = local_functions
            self.local_lambdas = local_lambdas
            self.active_local_calls: set[str] = set()

        def resolved_name(self, call_name: str) -> str:
            first, separator, remainder = call_name.partition(".")
            resolved_first = self.import_aliases.get(first, first)
            return f"{resolved_first}.{remainder}" if separator else resolved_first

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            for decorator in node.decorator_list:
                self.visit(decorator)
            for default in node.args.defaults:
                self.visit(default)
            for default in node.args.kw_defaults:
                if default is not None:
                    self.visit(default)
            if node.returns is not None:
                self.visit(node.returns)

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Lambda(self, node: ast.Lambda) -> None:
            return

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            for decorator in node.decorator_list:
                self.visit(decorator)
            for base in node.bases:
                self.visit(base)
            for keyword in node.keywords:
                self.visit(keyword.value)
            for statement in node.body:
                self.visit(statement)

        def visit_Call(self, node: ast.Call) -> None:
            call_name = self.resolved_name(qualified_name(node.func))
            terminal_name = call_name.rsplit(".", 1)[-1] if call_name else ""
            unsafe_os_call = call_name.startswith("os.") and call_name not in safe_os_path_calls
            if call_name in dangerous_exact_calls or call_name.startswith(dangerous_module_prefixes) or terminal_name in dangerous_terminal_methods or unsafe_os_call:
                self.calls.append((node.lineno, call_name or "dynamic call"))
            local_name = node.func.id if isinstance(node.func, ast.Name) else ""
            if local_name in self.local_functions and local_name not in self.active_local_calls:
                self.active_local_calls.add(local_name)
                for statement in self.local_functions[local_name].body:
                    self.visit(statement)
                self.active_local_calls.remove(local_name)
            elif local_name in self.local_lambdas and local_name not in self.active_local_calls:
                self.active_local_calls.add(local_name)
                self.visit(self.local_lambdas[local_name].body)
                self.active_local_calls.remove(local_name)
            elif isinstance(node.func, ast.Lambda):
                self.visit(node.func.body)
            self.generic_visit(node)

        def visit_Subscript(self, node: ast.Subscript) -> None:
            value_name = self.resolved_name(qualified_name(node.value))
            if value_name == "os.environ":
                self.calls.append((node.lineno, value_name))
            self.generic_visit(node)

        def visit_Attribute(self, node: ast.Attribute) -> None:
            value_name = self.resolved_name(qualified_name(node))
            if value_name == "os.environ":
                self.calls.append((node.lineno, value_name))
            self.generic_visit(node)

    source_directories = [
        workspace / "src" / "minisweagent" / "agents",
        workspace / "src" / "minisweagent" / "models" / "utils",
    ]
    for source_directory in source_directories:
        if not source_directory.is_dir() or source_directory.is_symlink():
            continue
        for python_path in sorted(source_directory.rglob("*.py")):
            if python_path.is_symlink():
                continue
            try:
                tree = ast.parse(python_path.read_text(encoding="utf-8"), filename=str(python_path))
            except (SyntaxError, UnicodeDecodeError):
                continue
            import_aliases: dict[str, str] = {}
            local_functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
            local_lambdas: dict[str, ast.Lambda] = {}
            for statement in tree.body:
                if isinstance(statement, ast.Import):
                    for alias in statement.names:
                        import_aliases[alias.asname or alias.name.split(".", 1)[0]] = alias.name
                elif isinstance(statement, ast.ImportFrom) and statement.module:
                    for alias in statement.names:
                        import_aliases[alias.asname or alias.name] = f"{statement.module}.{alias.name}"
                elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    local_functions[statement.name] = statement
                elif isinstance(statement, ast.Assign) and isinstance(statement.value, ast.Lambda):
                    for target in statement.targets:
                        if isinstance(target, ast.Name):
                            local_lambdas[target.id] = statement.value
            visitor = ImportTimeVisitor(import_aliases, local_functions, local_lambdas)
            visitor.visit(tree)
            relative_path = python_path.relative_to(workspace).as_posix()
            for line_number, call_name in visitor.calls:
                report.add("ERROR", "import_side_effect", f"host-side call at import time is not allowed at line {line_number}: {call_name}", relative_path)


def _local_module_exists(workspace: Path, module_name: str) -> bool:
    if module_name == "minisweagent":
        return (workspace / "src" / "minisweagent" / "__init__.py").is_file()
    if not module_name.startswith("minisweagent."):
        return True
    module_path = workspace / "src" / Path(*module_name.split("."))
    return module_path.with_suffix(".py").is_file() or (module_path / "__init__.py").is_file()


def _validate_local_imports(report: Report, workspace: Path) -> None:
    source_directories = [
        workspace / "src" / "minisweagent" / "agents",
        workspace / "src" / "minisweagent" / "models" / "utils",
    ]
    for source_directory in source_directories:
        if not source_directory.is_dir() or source_directory.is_symlink():
            continue
        for python_path in sorted(source_directory.rglob("*.py")):
            if python_path.is_symlink():
                continue
            relative_path = python_path.relative_to(workspace).as_posix()
            try:
                tree = ast.parse(python_path.read_text(encoding="utf-8"), filename=str(python_path))
            except (SyntaxError, UnicodeDecodeError):
                continue
            source_relative = python_path.relative_to(workspace / "src")
            current_package_parts = list(source_relative.parent.parts)
            for node in ast.walk(tree):
                module_names: list[str] = []
                if isinstance(node, ast.Import):
                    module_names.extend(alias.name for alias in node.names if alias.name.startswith("minisweagent"))
                elif isinstance(node, ast.ImportFrom):
                    if node.level > 0:
                        parent_steps = node.level - 1
                        if parent_steps >= len(current_package_parts):
                            report.add("ERROR", "local_import", f"relative import escapes candidate package at line {node.lineno}", relative_path)
                            continue
                        anchor_parts = current_package_parts[: len(current_package_parts) - parent_steps]
                        if node.module:
                            anchor_parts.extend(node.module.split("."))
                        module_names.append(".".join(anchor_parts))
                    elif node.module and node.module.startswith("minisweagent"):
                        module_names.append(node.module)
                for module_name in module_names:
                    if not _local_module_exists(workspace, module_name):
                        report.add("ERROR", "local_import", f"local Mini module does not exist at line {getattr(node, 'lineno', '?')}: {module_name}", relative_path)


def _validate_unwired_helpers(report: Report, workspace: Path) -> None:
    agents_dir = workspace / "src" / "minisweagent" / "agents"
    entry_path = agents_dir / "default.py"
    if not entry_path.is_file() or entry_path.is_symlink():
        return
    module_paths = {
        "minisweagent.agents." + ".".join(path.relative_to(agents_dir).with_suffix("").parts): path
        for path in agents_dir.rglob("*.py")
        if not path.is_symlink() and path.name != "__init__.py" and ".ipynb_checkpoints" not in path.parts and "__pycache__" not in path.parts
    }
    reachable: set[Path] = set()
    pending = [entry_path]
    while pending:
        current_path = pending.pop()
        if current_path in reachable:
            continue
        reachable.add(current_path)
        try:
            tree = ast.parse(current_path.read_text(encoding="utf-8"), filename=str(current_path))
        except (SyntaxError, UnicodeDecodeError):
            continue
        source_relative = current_path.relative_to(workspace / "src")
        current_package_parts = list(source_relative.parent.parts)
        imported_modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.update(alias.name for alias in node.names if alias.name.startswith("minisweagent.agents."))
            elif isinstance(node, ast.ImportFrom):
                base_module = ""
                if node.level > 0:
                    parent_steps = node.level - 1
                    if parent_steps < len(current_package_parts):
                        anchor_parts = current_package_parts[: len(current_package_parts) - parent_steps]
                        if node.module:
                            anchor_parts.extend(node.module.split("."))
                        base_module = ".".join(anchor_parts)
                elif node.module:
                    base_module = node.module
                if base_module.startswith("minisweagent.agents."):
                    imported_modules.add(base_module)
                    for alias in node.names:
                        imported_modules.add(f"{base_module}.{alias.name}")
        for module_name in imported_modules:
            imported_path = module_paths.get(module_name)
            if imported_path is not None and imported_path not in reachable:
                pending.append(imported_path)
    baseline = _baseline_commit(workspace)
    for module_name, python_path in sorted(module_paths.items()):
        if python_path in reachable or python_path.name == "interactive.py":
            continue
        relative_path = python_path.relative_to(workspace).as_posix()
        baseline_probe = subprocess.run(["git", "cat-file", "-e", f"{baseline}:{relative_path}"], cwd=workspace, capture_output=True, check=False)
        if baseline_probe.returncode != 0:
            report.add("WARNING", "ineffective_code", "new helper is not reachable from the fixed DefaultAgent import graph", relative_path)


def _validate_jinja_templates(report: Report, resolved: Mapping[str, Any], candidate_config_path: str) -> None:
    template_fields = [
        ("agent", "system_template"),
        ("agent", "instance_template"),
        ("model", "observation_template"),
        ("model", "format_error_template"),
    ]
    environment = Environment()
    for section_name, field_name in template_fields:
        section = resolved.get(section_name)
        dotted_name = f"{section_name}.{field_name}"
        if not isinstance(section, Mapping):
            report.add("ERROR", "config_schema", f"{section_name} must be a mapping", candidate_config_path)
            continue
        value = section.get(field_name)
        if not isinstance(value, str):
            report.add("ERROR", "config_schema", f"{dotted_name} must be a string", candidate_config_path)
            continue
        try:
            environment.parse(value)
        except TemplateSyntaxError as error:
            report.add("ERROR", "jinja_syntax", f"{dotted_name} line {error.lineno}: {error.message}", candidate_config_path)


_RUNTIME_PROBE = r'''import builtins
import io
import json
import os
import platform
import re
import socket
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace

payload = json.load(sys.stdin)

def disabled_network(*args, **kwargs):
    raise RuntimeError("network access is disabled during Mini validator runtime smoke")

socket.socket.connect = disabled_network
socket.create_connection = disabled_network
workspace = Path(payload["workspace"])
candidate_src = workspace / "src"
harbor_source = Path(payload["harbor_source_dir"])
sys.path.insert(0, str(candidate_src))
sys.path.insert(0, str(harbor_source / "src"))
for module_name in list(sys.modules):
    if module_name == "minisweagent" or module_name.startswith("minisweagent."):
        del sys.modules[module_name]
issues = []

runtime_source_directories = [
    candidate_src / "minisweagent" / "agents",
    candidate_src / "minisweagent" / "models" / "utils",
]
for source_directory in runtime_source_directories:
    for python_path in sorted(source_directory.rglob("*.py")):
        if python_path.is_symlink():
            continue
        try:
            compile(python_path.read_text(encoding="utf-8"), str(python_path), "exec")
        except Exception as error:
            issues.append({"severity": "ERROR", "category": "python_runtime_syntax", "message": f"{type(error).__name__}: {error}", "path": python_path.relative_to(workspace).as_posix()})

if not any(issue["severity"] == "ERROR" for issue in issues):
    try:
        from jinja2 import StrictUndefined, Template
        from jsonschema import Draft7Validator
        from jsonschema.exceptions import SchemaError, ValidationError
        from minisweagent.agents.default import DefaultAgent
        from minisweagent.models import get_model
        from minisweagent.models.host_guard import guarded_candidate_utility
        from minisweagent.models.utils.actions_toolcall import format_toolcall_observation_messages, parse_toolcall_actions
        from minisweagent.models.utils.anthropic_utils import _reorder_anthropic_thinking_blocks
        from minisweagent.models.utils.cache_control import set_cache_control
        from minisweagent.models.utils.openai_multimodal import expand_multimodal_content
        from minisweagent.models.utils.tool_registry import TOOL_REGISTRY, build_tool_action, get_tool_schemas
        from harbor.agents.installed.mini_swe_agent import HarborMiniEnvironmentConfig

        resolved = payload["resolved"]
        model = get_model(config=resolved.get("model", {}))
        model_config = model.config
        environment_config = HarborMiniEnvironmentConfig(**resolved.get("environment", {}))
        if not isinstance(TOOL_REGISTRY, dict) or not TOOL_REGISTRY:
            raise ValueError("TOOL_REGISTRY must be a non-empty dict")

        def blocked_builder_side_effect(*args, **kwargs):
            raise RuntimeError("tool builders must be pure argument-to-command transformations")

        class BlockedEnvironment(dict):
            def __getitem__(self, key):
                return blocked_builder_side_effect(key)

            def get(self, key, default=None):
                return blocked_builder_side_effect(key, default)

            def __contains__(self, key):
                return blocked_builder_side_effect(key)

        validator_host_environment = os.environ
        dangerous_builder_names = {
            "Path", "Popen", "__import__", "builtins", "call", "check_call", "check_output",
            "compile", "connect", "create_connection", "ctypes", "datetime", "environ", "eval", "exec",
            "exists", "getattr", "getenv", "glob", "globals", "hash", "httpx", "id", "importlib", "is_dir", "is_file",
            "iterdir", "listdir", "locals", "lstat", "multiprocessing", "open", "os", "pathlib", "popen",
            "putenv", "random", "read_bytes", "read_text", "readlink", "requests", "rglob", "run", "scandir",
            "secrets", "socket", "stat", "subprocess", "sys", "system", "tempfile", "time", "unsetenv", "urlopen",
            "setattr", "urllib", "uuid", "vars", "write_bytes", "write_text",
        }
        dangerous_module_roots = {
            "builtins", "ctypes", "datetime", "httpx", "importlib", "multiprocessing", "os", "pathlib",
            "random", "requests", "secrets", "socket", "subprocess", "sys", "tempfile", "time", "urllib", "uuid",
        }
        dangerous_utility_names = dangerous_builder_names - {
            "Path", "datetime", "hash", "id", "os", "pathlib", "time", "vars",
        }
        dangerous_utility_module_roots = dangerous_module_roots - {"datetime", "os", "pathlib", "time"}

        utility_path_by_name = {
            "get_tool_schemas": get_tool_schemas,
            "build_tool_action": build_tool_action,
            "parse_toolcall_actions": parse_toolcall_actions,
            "format_toolcall_observation_messages": format_toolcall_observation_messages,
            "_reorder_anthropic_thinking_blocks": _reorder_anthropic_thinking_blocks,
            "set_cache_control": set_cache_control,
            "expand_multimodal_content": expand_multimodal_content,
        }

        def validate_builder_object(value, tool_name, pending, seen_objects, forbidden_name_set, forbidden_module_root_set):
            value_id = id(value)
            if value_id in seen_objects:
                return
            seen_objects.add(value_id)
            if value is validator_host_environment:
                raise ValueError(f"tool '{tool_name}' builder must not capture host environment")
            if isinstance(value, Path):
                raise ValueError(f"tool '{tool_name}' builder must not capture host filesystem paths")
            if value is None or isinstance(value, (str, bytes, int, float, bool)):
                return
            if isinstance(value, dict):
                for nested_key, nested_value in value.items():
                    validate_builder_object(nested_key, tool_name, pending, seen_objects, forbidden_name_set, forbidden_module_root_set)
                    validate_builder_object(nested_value, tool_name, pending, seen_objects, forbidden_name_set, forbidden_module_root_set)
                return
            if isinstance(value, (list, tuple, set, frozenset)):
                for nested_value in value:
                    validate_builder_object(nested_value, tool_name, pending, seen_objects, forbidden_name_set, forbidden_module_root_set)
                return
            if type(value).__module__.startswith("minisweagent.") and hasattr(value, "__dict__"):
                for nested_value in vars(value).values():
                    validate_builder_object(nested_value, tool_name, pending, seen_objects, forbidden_name_set, forbidden_module_root_set)
                return
            if isinstance(value, types.ModuleType):
                module_root = value.__name__.split(".", 1)[0]
                if module_root in forbidden_module_root_set:
                    raise ValueError(f"tool '{tool_name}' builder references forbidden module: {value.__name__}")
                return
            if callable(value):
                module_root = str(getattr(value, "__module__", "")).split(".", 1)[0]
                callable_name = str(getattr(value, "__name__", ""))
                if module_root in forbidden_module_root_set or callable_name in forbidden_name_set:
                    raise ValueError(f"tool '{tool_name}' builder references forbidden callable module: {module_root}")
                if isinstance(value, types.FunctionType) and str(getattr(value, "__module__", "")).startswith("minisweagent."):
                    pending.append(value)
                    return
                if isinstance(value, types.FunctionType):
                    return
                if isinstance(value, type):
                    return
                raise ValueError(f"tool '{tool_name}' builder captures a non-inspectable callable")
            raise ValueError(f"tool '{tool_name}' builder captures unsupported object type: {type(value).__name__}")

        def validate_candidate_callable(callable_object, callable_label, forbidden_name_set, forbidden_module_root_set):
            pending = [callable_object]
            visited = set()
            seen_objects = set()
            while pending:
                current = pending.pop()
                current_id = id(current)
                if current_id in visited:
                    continue
                visited.add(current_id)
                code = getattr(current, "__code__", None)
                if code is None:
                    raise ValueError(f"candidate utility '{callable_label}' must be an inspectable Python function")
                referenced_names = set(code.co_names)
                forbidden_names = sorted(referenced_names & forbidden_name_set)
                if forbidden_names:
                    raise ValueError(f"candidate utility '{callable_label}' references forbidden names: {', '.join(forbidden_names)}")
                function_globals = getattr(current, "__globals__", {})
                for referenced_name in referenced_names:
                    if referenced_name in function_globals:
                        validate_builder_object(function_globals[referenced_name], callable_label, pending, seen_objects, forbidden_name_set, forbidden_module_root_set)
                defaults = tuple(getattr(current, "__defaults__", ()) or ())
                keyword_defaults = tuple((getattr(current, "__kwdefaults__", {}) or {}).values())
                for captured_value in defaults + keyword_defaults:
                    validate_builder_object(captured_value, callable_label, pending, seen_objects, forbidden_name_set, forbidden_module_root_set)
                closure = getattr(current, "__closure__", ()) or ()
                for cell in closure:
                    try:
                        captured_value = cell.cell_contents
                    except ValueError:
                        continue
                    validate_builder_object(captured_value, callable_label, pending, seen_objects, forbidden_name_set, forbidden_module_root_set)

        for utility_name, utility_callable in utility_path_by_name.items():
            validate_candidate_callable(utility_callable, utility_name, dangerous_utility_names, dangerous_utility_module_roots)

        protected_operations = [
            (builtins, "open"),
            (io, "open"),
            (subprocess, "Popen"),
            (subprocess, "run"),
            (subprocess, "call"),
            (subprocess, "check_call"),
            (subprocess, "check_output"),
            (os, "system"),
            (os, "popen"),
            (os, "open"),
            (os, "listdir"),
            (os, "scandir"),
            (os, "stat"),
            (os, "lstat"),
            (os, "readlink"),
            (os, "getenv"),
            (os, "putenv"),
            (os, "unsetenv"),
            (Path, "open"),
            (Path, "read_text"),
            (Path, "read_bytes"),
            (Path, "write_text"),
            (Path, "write_bytes"),
            (Path, "iterdir"),
            (Path, "glob"),
            (Path, "rglob"),
            (Path, "exists"),
            (Path, "is_file"),
            (Path, "is_dir"),
        ]
        seen_tool_names = set()
        bash_smoke_action = None
        for registry_name, registration in TOOL_REGISTRY.items():
            if not isinstance(registry_name, str) or not registry_name:
                raise ValueError("tool registry names must be non-empty strings")
            if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", registry_name) is None:
                raise ValueError(f"tool '{registry_name}' name must match [A-Za-z0-9_-] and be at most 64 characters")
            schema = registration.schema
            if not isinstance(schema, dict) or schema.get("type") != "function" or not isinstance(schema.get("function"), dict):
                raise ValueError(f"tool '{registry_name}' schema must use OpenAI chat function format")
            function_payload = schema["function"]
            schema_name = function_payload.get("name")
            if schema_name != registry_name:
                raise ValueError(f"tool registry key '{registry_name}' does not match schema name '{schema_name}'")
            if schema_name in seen_tool_names:
                raise ValueError(f"duplicate tool schema name: {schema_name}")
            seen_tool_names.add(schema_name)
            if not isinstance(function_payload.get("description"), str) or not function_payload["description"].strip():
                raise ValueError(f"tool '{registry_name}' must have a non-empty description")
            parameters = function_payload.get("parameters")
            if not isinstance(parameters, dict) or parameters.get("type") != "object" or not isinstance(parameters.get("properties"), dict):
                raise ValueError(f"tool '{registry_name}' parameters must be an object JSON schema")
            try:
                Draft7Validator.check_schema(parameters)
            except SchemaError as error:
                raise ValueError(f"tool '{registry_name}' parameters are not a valid Draft 7 JSON Schema: {error.message}") from error
            if not callable(registration.build_command):
                raise ValueError(f"tool '{registry_name}' build_command must be callable")
            validate_candidate_callable(registration.build_command, f"tool '{registry_name}' builder", dangerous_builder_names, dangerous_module_roots)
            if not isinstance(registration.smoke_arguments, dict):
                raise ValueError(f"tool '{registry_name}' smoke_arguments must be a dict")
            json.dumps(registration.smoke_arguments, ensure_ascii=False)
            try:
                Draft7Validator(parameters).validate(registration.smoke_arguments)
            except ValidationError as error:
                raise ValueError(f"tool '{registry_name}' smoke_arguments do not satisfy its schema: {error.message}") from error
            original_operations = [(owner, name, getattr(owner, name)) for owner, name in protected_operations]
            builder_globals = getattr(registration.build_command, "__globals__", {})
            blocked_global_names = {
                "open",
                "subprocess",
                "socket",
                "requests",
                "httpx",
                "urllib",
                "urlopen",
                "Popen",
                "run",
                "call",
                "check_call",
                "check_output",
                "system",
                "popen",
                "getenv",
                "environ",
            }
            original_builder_globals = {name: builder_globals[name] for name in blocked_global_names if name in builder_globals}
            original_environment = validator_host_environment
            try:
                for owner, name, _ in original_operations:
                    setattr(owner, name, blocked_builder_side_effect)
                for name in original_builder_globals:
                    builder_globals[name] = blocked_builder_side_effect
                os.environ = BlockedEnvironment()
                action = guarded_candidate_utility(build_tool_action, registry_name, registration.smoke_arguments, f"validator-{registry_name}")
                repeated_action = guarded_candidate_utility(build_tool_action, registry_name, registration.smoke_arguments, f"validator-{registry_name}")
            finally:
                os.environ = original_environment
                for name, original in original_builder_globals.items():
                    builder_globals[name] = original
                for owner, name, original in original_operations:
                    setattr(owner, name, original)
            if not isinstance(action, dict) or not isinstance(action.get("command"), str) or not action["command"]:
                raise ValueError(f"tool '{registry_name}' did not build a non-empty E2B command action")
            if action != repeated_action:
                raise ValueError(f"tool '{registry_name}' builder must be deterministic for identical arguments")
            if isinstance(action["command"], str) and "\x00" in action["command"]:
                raise ValueError(f"tool '{registry_name}' command must not contain NUL bytes")
            if registry_name != "bash" and action.get("tool_name") != registry_name:
                raise ValueError(f"tool '{registry_name}' action did not preserve tool_name metadata")
            if action.get("tool_call_id") != f"validator-{registry_name}":
                raise ValueError(f"tool '{registry_name}' action did not preserve tool_call_id metadata")
            if registry_name != "bash" and action.get("tool_arguments") != registration.smoke_arguments:
                raise ValueError(f"tool '{registry_name}' action did not preserve tool_arguments metadata")
            if registry_name == "bash":
                bash_smoke_action = dict(action)

        expected_tool_schemas = [registration.schema for registration in TOOL_REGISTRY.values()]
        registry_identity_before = {name: (id(registration), id(registration.schema), id(registration.build_command), id(registration.smoke_arguments)) for name, registration in TOOL_REGISTRY.items()}
        tool_schemas = guarded_candidate_utility(get_tool_schemas)
        registry_identity_after = {name: (id(registration), id(registration.schema), id(registration.build_command), id(registration.smoke_arguments)) for name, registration in TOOL_REGISTRY.items()}
        if registry_identity_after != registry_identity_before:
            raise ValueError("get_tool_schemas() must not mutate TOOL_REGISTRY or replace registrations")
        if not isinstance(tool_schemas, list) or tool_schemas != expected_tool_schemas:
            raise ValueError("get_tool_schemas() must exactly return registration.schema values in TOOL_REGISTRY order")
        repeated_tool_schemas = guarded_candidate_utility(get_tool_schemas)
        repeated_registry_identity = {name: (id(registration), id(registration.schema), id(registration.build_command), id(registration.smoke_arguments)) for name, registration in TOOL_REGISTRY.items()}
        if repeated_tool_schemas != tool_schemas or repeated_registry_identity != registry_identity_before:
            raise ValueError("get_tool_schemas() must be deterministic and side-effect free")
        json.dumps(tool_schemas, ensure_ascii=False)

        bash_call = SimpleNamespace(id="validator-bash-parser", function=SimpleNamespace(name="bash", arguments=json.dumps(TOOL_REGISTRY["bash"].smoke_arguments)))
        parsed_bash_actions = guarded_candidate_utility(parse_toolcall_actions, [bash_call], format_error_template="{{ error }}")
        if bash_smoke_action is None:
            raise ValueError("TOOL_REGISTRY must register the bash tool")
        expected_bash_parser_action = {"command": bash_smoke_action["command"], "tool_call_id": "validator-bash-parser"}
        if parsed_bash_actions != [expected_bash_parser_action]:
            raise ValueError("bash parser action must use the validated bash builder output and preserve the Mini two-field interface")
        formatted_observations = guarded_candidate_utility(
            format_toolcall_observation_messages,
            actions=parsed_bash_actions,
            outputs=[{"output": "ok", "returncode": 0, "exception_info": ""}],
            observation_template=model_config.observation_template,
            template_vars={},
            multimodal_regex=model_config.multimodal_regex,
        )
        if not isinstance(formatted_observations, list) or len(formatted_observations) != 1 or formatted_observations[0].get("tool_call_id") != "validator-bash-parser":
            raise ValueError("observation formatter did not preserve tool call alignment")

        class DummyModel:
            def get_template_vars(self, **kwargs):
                return {**model_config.model_dump(), **kwargs}

            def query(self, *args, **kwargs):
                raise RuntimeError("validator dummy model must not be queried")

            def serialize(self):
                return {}

        class DummyEnvironment:
            def get_template_vars(self, **kwargs):
                return {**environment_config.model_dump(), **platform.uname()._asdict(), **os.environ, **kwargs}

            def execute(self, *args, **kwargs):
                raise RuntimeError("validator dummy environment must not execute commands")

            def serialize(self):
                return {}

        agent = DefaultAgent(DummyModel(), DummyEnvironment(), **resolved.get("agent", {}))
        if hasattr(agent, "extra_template_vars"):
            agent.extra_template_vars |= {"task": "validator task"}
        if hasattr(agent, "_render_template") and hasattr(agent, "config"):
            agent._render_template(agent.config.system_template)
            agent._render_template(agent.config.instance_template)
        Template(model_config.observation_template, undefined=StrictUndefined).render(output={"output": "ok", "returncode": 0, "exception_info": ""}, tool_name="bash", tool_arguments={"command": "true"})
        Template(model_config.format_error_template, undefined=StrictUndefined).render(error="validator error", finish_reason="tool_calls", has_tool_calls=True)

        def unknown_fields(config_payload, config_object, ignored=frozenset()):
            model_fields = set(type(config_object).model_fields)
            return sorted(set(config_payload) - model_fields - set(ignored))

        agent_unknown = unknown_fields(resolved.get("agent", {}), agent.config, {"mode", "confirm_exit"}) if hasattr(agent, "config") else []
        model_unknown = unknown_fields(resolved.get("model", {}), model_config, {"model_class"})
        environment_unknown = unknown_fields(resolved.get("environment", {}), environment_config)
        for section_name, names in (("agent", agent_unknown), ("model", model_unknown), ("environment", environment_unknown)):
            for field_name in names:
                issues.append({"severity": "WARNING", "category": "ineffective_config", "message": f"{section_name}.{field_name} is ignored by the evaluated runtime", "path": payload["candidate_config_path"]})
    except Exception as error:
        issues.append({"severity": "ERROR", "category": "runtime_smoke", "message": f"{type(error).__name__}: {error}", "path": payload["candidate_config_path"]})

print("__AHE_MINI_VALIDATOR_JSON__=" + json.dumps({"issues": issues}, ensure_ascii=False))
'''


def _sanitized_runtime_env(harbor_source_dir: Path, workspace: Path) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not any(token in key.upper() for token in _SENSITIVE_ENV_TOKENS)
    }
    python_paths = [str(workspace / "src"), str(harbor_source_dir / "src")]
    environment["PYTHONPATH"] = os.pathsep.join(python_paths)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["MSWEA_CONFIGURED"] = "true"
    return environment


def _validate_runtime(
    report: Report,
    workspace: Path,
    candidate_config_path: str,
    resolved: dict[str, Any],
    runtime_python: Path,
    harbor_source_dir: Path,
) -> None:
    if not runtime_python.is_file():
        report.add_infrastructure_error("runtime", f"runtime Python does not exist: {runtime_python}")
        return
    if not (harbor_source_dir / "src" / "harbor" / "agents" / "installed" / "mini_swe_agent.py").is_file():
        report.add_infrastructure_error("runtime", f"Harbor Mini host bridge source does not exist: {harbor_source_dir}")
        return
    payload = {
        "workspace": str(workspace),
        "candidate_config_path": candidate_config_path,
        "harbor_source_dir": str(harbor_source_dir),
        "resolved": resolved,
    }
    process = subprocess.Popen(
        [str(runtime_python), "-c", _RUNTIME_PROBE],
        cwd=workspace,
        env=_sanitized_runtime_env(harbor_source_dir, workspace),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(json.dumps(payload, ensure_ascii=False), timeout=45)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        report.add("ERROR", "runtime_smoke", "runtime import/schema smoke exceeded 45 seconds", candidate_config_path)
        return
    if process.returncode != 0:
        detail = (stderr or stdout).strip()[-2000:]
        report.add_infrastructure_error("runtime", f"runtime probe failed with exit code {process.returncode}: {detail}")
        return
    marker_lines = [line for line in stdout.splitlines() if line.startswith(_RUNTIME_RESULT_MARKER)]
    if not marker_lines:
        detail = stdout.strip()[-1000:]
        report.add_infrastructure_error("runtime", f"runtime probe produced no structured result: {detail}")
        return
    try:
        runtime_report = json.loads(marker_lines[-1][len(_RUNTIME_RESULT_MARKER):])
        runtime_issues = runtime_report.get("issues", [])
        if not isinstance(runtime_issues, list):
            raise ValueError("runtime issues is not a list")
        for raw_issue in runtime_issues:
            if not isinstance(raw_issue, Mapping):
                raise ValueError("runtime issue is not a mapping")
            report.add(
                str(raw_issue.get("severity", "ERROR")),
                str(raw_issue.get("category", "runtime_smoke")),
                str(raw_issue.get("message", "runtime validation failed")),
                str(raw_issue["path"]) if raw_issue.get("path") is not None else None,
            )
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        report.add_infrastructure_error("runtime", f"invalid structured runtime report: {error}")


def validate_workspace(
    workspace: Path,
    policy_path: Path,
    evaluation_lock_path: Path,
    runtime_python: Path,
    harbor_source_dir: Path,
    baseline_source: Path | None,
    run_runtime_check: bool = True,
) -> Report:
    workspace = workspace.expanduser().resolve()
    report = Report(workspace=str(workspace))
    try:
        if not workspace.is_dir() or not (workspace / ".git").exists():
            raise RuntimeError(f"workspace is not a Git repository: {workspace}")
        policy = _load_yaml_mapping(policy_path, "workspace policy")
        evaluation_lock = _load_yaml_mapping(evaluation_lock_path, "evaluation lock")
        _, candidate_payload, candidate_config_path = _validate_policy(report, workspace, policy, baseline_source)
    except (OSError, RuntimeError, subprocess.CalledProcessError, yaml.YAMLError) as error:
        report.add_infrastructure_error("validator", str(error))
        return report
    if candidate_payload is None or candidate_config_path is None:
        report.add_infrastructure_error("validator", "candidate configuration was not loaded")
        return report
    try:
        resolved = _resolved_config(candidate_payload, evaluation_lock)
    except RuntimeError as error:
        report.add_infrastructure_error("evaluation_lock", str(error))
        return report
    allowed_top_level = {"agent", "model", "environment"}
    for field_name in sorted(set(resolved) - allowed_top_level):
        report.add("WARNING", "ineffective_config", f"top-level field is ignored by the evaluated adapter: {field_name}", candidate_config_path)
    _validate_python_syntax(report, workspace)
    _validate_import_time_side_effects(report, workspace)
    _validate_local_imports(report, workspace)
    _validate_unwired_helpers(report, workspace)
    _validate_jinja_templates(report, resolved, candidate_config_path)
    if run_runtime_check:
        _validate_runtime(report, workspace, candidate_config_path, resolved, runtime_python, harbor_source_dir)
    else:
        report.add("WARNING", "runtime", "runtime import/schema smoke was explicitly skipped")
    return report


def _print_text_report(report: Report) -> None:
    print(f"Validating Mini workspace: {report.workspace}")
    print("=" * 72)
    if report.issues:
        print()
        for issue in report.issues:
            severity = issue.severity.ljust(7)
            category = issue.category.ljust(22)
            location = f" [{issue.path}]" if issue.path else ""
            print(f"[{severity}] {category} | {issue.message}{location}")
        print()
    else:
        print("\n  No issues found.\n")
    print("=" * 72)
    status = "VALID" if report.is_valid else "INVALID"
    print(f"Result: {status} ({report.error_count} errors, {report.warning_count} warnings)")


def _print_json_report(report: Report) -> None:
    payload = {
        "workspace": report.workspace,
        "valid": report.is_valid,
        "infrastructure_error": report.infrastructure_error,
        "error_count": report.error_count,
        "warning_count": report.warning_count,
        "issues": [asdict(issue) for issue in report.issues],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a Mini-SWE-agent candidate workspace and its evaluated runtime configuration")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--policy", required=True)
    parser.add_argument("--evaluation-lock", required=True)
    parser.add_argument("--runtime-python", default=sys.executable)
    parser.add_argument("--harbor-source-dir", required=True)
    parser.add_argument("--baseline-source")
    parser.add_argument("--no-runtime-check", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")
    arguments = parser.parse_args()
    runtime_python = Path(arguments.runtime_python).expanduser()
    baseline_source = Path(arguments.baseline_source).expanduser().resolve() if arguments.baseline_source else None
    report = validate_workspace(
        workspace=Path(arguments.workspace),
        policy_path=Path(arguments.policy).expanduser().resolve(),
        evaluation_lock_path=Path(arguments.evaluation_lock).expanduser().resolve(),
        runtime_python=runtime_python,
        harbor_source_dir=Path(arguments.harbor_source_dir).expanduser().resolve(),
        baseline_source=baseline_source,
        run_runtime_check=not arguments.no_runtime_check,
    )
    if arguments.json_output:
        _print_json_report(report)
    else:
        _print_text_report(report)
    if report.infrastructure_error:
        return 4
    return 0 if report.is_valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
