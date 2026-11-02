"""在 Harbor 0.8 中执行 AHE 当前候选 Mini-SWE-agent 的固定适配器。"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import yaml

from harbor.agents.installed.mini_swe_agent import HarborMiniEnvironment
from harbor.agents.installed.mini_swe_agent import MiniSweAgent
from harbor.agents.installed.mini_swe_agent import _ensure_local_mini_swe_agent_importable
from harbor.environments.base import BaseEnvironment

from .mini_swe_trace import write_openai_messages_view


def _load_mapping(path: Path, label: str) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"{label} 顶层必须是 YAML mapping: {path}")
    return payload


class CandidateMiniSweAgent(MiniSweAgent):
    """从本轮 workspace 读取 Mini 源码和 benchmark YAML，再交给 Harbor E2B 执行。"""

    def __init__(
        self,
        candidate_config_path: str | Path,
        evaluation_lock_path: str | Path,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        self.candidate_config_path = Path(candidate_config_path).resolve()
        self.evaluation_lock_path = Path(evaluation_lock_path).resolve()
        super().__init__(*args, **kwargs)
        source_root = Path(self.mini_swe_agent_src).resolve()
        try:
            self.candidate_config_path.relative_to(source_root)
        except ValueError as error:
            raise ValueError("candidate_config_path 必须位于 mini_swe_agent_src 内") from error
        if not self.candidate_config_path.is_file():
            raise FileNotFoundError(f"候选 Mini 配置不存在: {self.candidate_config_path}")
        if not self.evaluation_lock_path.is_file():
            raise FileNotFoundError(f"Mini evaluation lock 不存在: {self.evaluation_lock_path}")
        lock_payload = _load_mapping(self.evaluation_lock_path, "Mini evaluation lock")
        self.model_api_key_env = str(lock_payload.get("model_api_key_env", "DEEPSEEK_API_KEY"))

    @staticmethod
    def name() -> str:
        return "mini-swe-agent"

    def _resolved_config(self) -> dict[str, Any]:
        _ensure_local_mini_swe_agent_importable(self.mini_swe_agent_src)
        from minisweagent.utils.serialize import recursive_merge

        candidate_payload = _load_mapping(self.candidate_config_path, "候选 Mini 配置")
        lock_payload = _load_mapping(self.evaluation_lock_path, "Mini evaluation lock")
        runtime_payload = {
            "agent": {
                "mode": "yolo",
                "confirm_exit": False,
                "cost_limit": self.cost_limit,
                "step_limit": self.step_limit,
                "output_path": str(self.logs_dir / "mini-swe-agent.trajectory.json"),
            },
            "environment": {
                "timeout": self.command_timeout,
            },
            "model": {
                "model_name": self.model_name,
                "cost_tracking": "ignore_errors",
                "model_kwargs": ({"api_base": self.api_base} if self.api_base else {}),
            },
        }
        resolved = recursive_merge(candidate_payload, lock_payload, runtime_payload)
        resolved.pop("harbor_runtime", None)
        resolved.pop("model_api_key_env", None)
        return resolved

    def _write_config_audit(self, config: dict[str, Any]) -> None:
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        resolved_path = self.logs_dir / "ahe_candidate_config.resolved.yaml"
        resolved_path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=True), encoding="utf-8")
        metadata = {
            "candidate_config_path": str(self.candidate_config_path),
            "candidate_config_sha256": hashlib.sha256(self.candidate_config_path.read_bytes()).hexdigest(),
            "evaluation_lock_path": str(self.evaluation_lock_path),
            "evaluation_lock_sha256": hashlib.sha256(self.evaluation_lock_path.read_bytes()).hexdigest(),
        }
        (self.logs_dir / "ahe_candidate_config.metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _write_tool_audit(self) -> None:
        _ensure_local_mini_swe_agent_importable(self.mini_swe_agent_src)
        from minisweagent.models.host_guard import guarded_candidate_utility
        from minisweagent.models.utils.tool_registry import get_tool_schemas

        source_root = Path(self.mini_swe_agent_src).resolve()
        utility_root = source_root / "minisweagent" / "models" / "utils"
        fixed_provider_path = source_root / "minisweagent" / "models" / "litellm_model.py"
        source_hashes = {
            path.relative_to(source_root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(utility_root.rglob("*.py"))
            if path.is_file() and not path.is_symlink()
        }
        payload = {
            "schemas": guarded_candidate_utility(get_tool_schemas),
            "source_sha256": source_hashes,
            "fixed_provider_sha256": hashlib.sha256(fixed_provider_path.read_bytes()).hexdigest(),
        }
        (self.logs_dir / "ahe_candidate_tools.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _run_sync(
        self,
        instruction: str,
        environment: BaseEnvironment,
        loop: Any,
        trajectory_path: Path,
    ) -> None:
        _ensure_local_mini_swe_agent_importable(self.mini_swe_agent_src)
        from minisweagent.agents.default import DefaultAgent
        from minisweagent.models import get_model

        config = self._resolved_config()
        self._write_config_audit(config)
        self._write_tool_audit()
        model_key_env = self.model_api_key_env
        original_provider_key = os.environ.get(model_key_env)
        original_deepseek_key = os.environ.get("DEEPSEEK_API_KEY")
        if not os.environ.get(model_key_env) and os.environ.get("LLM_API_KEY"):
            os.environ[model_key_env] = os.environ["LLM_API_KEY"]
        if model_key_env != "DEEPSEEK_API_KEY" and not os.environ.get("DEEPSEEK_API_KEY") and os.environ.get("LLM_API_KEY"):
            os.environ["DEEPSEEK_API_KEY"] = os.environ["LLM_API_KEY"]
        try:
            model = get_model(config=config.get("model", {}))
            mini_env = HarborMiniEnvironment(
                environment,
                loop,
                config=config.get("environment", {}),
                mini_swe_agent_src=self.mini_swe_agent_src,
            )
            agent = DefaultAgent(model, mini_env, **config.get("agent", {}))
            agent.run(instruction)
        finally:
            if original_provider_key is None:
                os.environ.pop(model_key_env, None)
            else:
                os.environ[model_key_env] = original_provider_key
            if original_deepseek_key is None:
                os.environ.pop("DEEPSEEK_API_KEY", None)
            else:
                os.environ["DEEPSEEK_API_KEY"] = original_deepseek_key

    def populate_context_post_run(self, context: Any) -> None:
        super().populate_context_post_run(context)
        source_path = self.logs_dir / "mini-swe-agent.trajectory.json"
        if source_path.is_file():
            write_openai_messages_view(source_path, self.logs_dir / "ahe_openai_messages.json")
