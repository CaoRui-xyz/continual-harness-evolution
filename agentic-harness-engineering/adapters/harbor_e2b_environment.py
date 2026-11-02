"""为 AHE 修复 Harbor 0.8 与 E2B SDK 的固定基础设施兼容性。"""

from __future__ import annotations

import hashlib
import re
import shutil
import tempfile
from pathlib import Path

from e2b import AsyncSandbox, AsyncTemplate, Template
from e2b.api.client.api.templates import get_templates_aliases_alias, get_templates_template_id
from e2b.api.client_async import get_api_client
from e2b.connection_config import ConnectionConfig
from tenacity import retry, stop_after_attempt, wait_exponential

from harbor.environments.e2b import E2BEnvironment
from harbor.models.task.config import EnvironmentConfig
from harbor.models.trial.paths import TrialPaths


_DOCKER_FROM_PATTERN = re.compile(r"^\s*FROM(?:\s|$)", re.IGNORECASE)
_DOCKER_HEREDOC_START_PATTERN = re.compile(
    r"^\s*RUN\s+<<(?P<quote>['\"]?)(?P<delimiter>[A-Za-z_][A-Za-z0-9_]*)"
    r"(?P=quote)\s+cat\s+>\s+(?P<target>/[^\s]+)\s*(?:\r?\n)?$",
    re.IGNORECASE,
)


def _prepare_e2b_dockerfile_context(environment_dir: Path, temporary_root: Path) -> tuple[Path, Path] | None:
    dockerfile_path = environment_dir / "Dockerfile"
    dockerfile_text = dockerfile_path.read_text(encoding="utf-8")
    if not re.search(r"^\s*RUN\s+<<", dockerfile_text, flags=re.IGNORECASE | re.MULTILINE):
        return None
    source_lines = dockerfile_text.splitlines(keepends=True)
    normalized_lines: list[str] = []
    extracted_payloads: list[tuple[str, bytes, str]] = []
    real_from_lines: list[int] = []
    line_index = 0
    while line_index < len(source_lines):
        current_line = source_lines[line_index]
        heredoc_match = _DOCKER_HEREDOC_START_PATTERN.match(current_line)
        if heredoc_match is None:
            if re.match(r"^\s*RUN\s+<<", current_line, flags=re.IGNORECASE):
                raise ValueError(
                    f"E2B兼容层不支持Dockerfile第{line_index + 1}行的heredoc形式: {current_line.strip()}"
                )
            if _DOCKER_FROM_PATTERN.match(current_line):
                real_from_lines.append(line_index + 1)
            normalized_lines.append(current_line)
            line_index += 1
            continue
        delimiter = heredoc_match.group("delimiter")
        target_path = heredoc_match.group("target")
        terminator_index = line_index + 1
        while terminator_index < len(source_lines):
            if source_lines[terminator_index].rstrip("\r\n") == delimiter:
                break
            terminator_index += 1
        if terminator_index >= len(source_lines):
            raise ValueError(f"Dockerfile第{line_index + 1}行的heredoc缺少结束标记{delimiter}")
        payload_bytes = "".join(source_lines[line_index + 1 : terminator_index]).encode("utf-8")
        payload_hash = hashlib.sha256(payload_bytes).hexdigest()[:16]
        payload_relative_path = f"ahe_e2b_heredoc_payloads/{len(extracted_payloads):02d}-{payload_hash}.bin"
        extracted_payloads.append((payload_relative_path, payload_bytes, target_path))
        normalized_lines.append(f"COPY --chown=root:root {payload_relative_path} {target_path}\n")
        line_index = terminator_index + 1
    if len(real_from_lines) != 1:
        raise ValueError(
            f"E2B仅支持单stage Dockerfile；heredoc外检测到FROM行: {real_from_lines}"
        )
    swt_bootstrap_command = 'RUN git config user.name "Anonymous"\n'
    swt_final_workdir = "WORKDIR /testbed\n"
    if extracted_payloads and swt_bootstrap_command in normalized_lines:
        if normalized_lines[-1].rstrip("\r\n") != swt_final_workdir.rstrip("\r\n"):
            raise ValueError("含SWT heredoc bootstrap的Dockerfile必须以WORKDIR /testbed结束")
        bootstrap_index = normalized_lines.index(swt_bootstrap_command)
        normalized_lines.insert(bootstrap_index, swt_final_workdir)
    normalized_context = temporary_root / "context"
    shutil.copytree(environment_dir, normalized_context, symlinks=True)
    payload_root = normalized_context / "ahe_e2b_heredoc_payloads"
    if payload_root.exists():
        raise ValueError(f"Docker build context已存在保留目录: {payload_root}")
    payload_root.mkdir(parents=True)
    for relative_path, payload_bytes, _target_path in extracted_payloads:
        payload_path = normalized_context / relative_path
        payload_path.write_bytes(payload_bytes)
        payload_path.chmod(0o644)
    normalized_dockerfile = normalized_context / "Dockerfile"
    normalized_dockerfile.write_text("".join(normalized_lines), encoding="utf-8")
    return normalized_context, normalized_dockerfile


class AheE2BEnvironment(E2BEnvironment):
    """限制 sandbox timeout，并无损适配 E2B SDK 不支持的写文件 heredoc。"""

    def __init__(
        self,
        environment_dir: Path,
        environment_name: str,
        session_id: str,
        trial_paths: TrialPaths,
        task_env_config: EnvironmentConfig,
        sandbox_timeout_seconds: int = 3600,
        *args,
        **kwargs,
    ) -> None:
        self.sandbox_timeout_seconds = int(sandbox_timeout_seconds)
        if not 1 <= self.sandbox_timeout_seconds <= 3600:
            raise ValueError("AHE E2B sandbox_timeout_seconds 必须位于 1 到 3600 秒之间")
        super().__init__(
            environment_dir=environment_dir,
            environment_name=environment_name,
            session_id=session_id,
            trial_paths=trial_paths,
            task_env_config=task_env_config,
            *args,
            **kwargs,
        )

    @retry(
        stop=stop_after_attempt(2),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    async def _create_template(self) -> None:
        if self.task_env_config.docker_image:
            template = Template().from_image(image=self.task_env_config.docker_image)
            await self._build_e2b_template(template)
            return
        with tempfile.TemporaryDirectory(prefix="ahe-e2b-dockerfile-") as temporary_dir_text:
            temporary_root = Path(temporary_dir_text)
            prepared_context = _prepare_e2b_dockerfile_context(self.environment_dir, temporary_root)
            if prepared_context is None:
                context_path = self.environment_dir
                dockerfile_path = self._environment_definition_path
            else:
                context_path, dockerfile_path = prepared_context
            template = Template(file_context_path=str(context_path)).from_dockerfile(
                dockerfile_content_or_path=str(dockerfile_path)
            )
            await self._build_e2b_template(template)

    async def _build_e2b_template(self, template) -> None:
        cpus = self._effective_cpus
        memory_mb = self._effective_memory_mb
        if cpus is not None and memory_mb is not None:
            await AsyncTemplate.build(template=template, alias=self._template_name, cpu_count=cpus, memory_mb=memory_mb)
        elif cpus is not None:
            await AsyncTemplate.build(template=template, alias=self._template_name, cpu_count=cpus)
        elif memory_mb is not None:
            await AsyncTemplate.build(template=template, alias=self._template_name, memory_mb=memory_mb)
        else:
            await AsyncTemplate.build(template=template, alias=self._template_name)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=4),
        reraise=True,
    )
    async def _does_template_exist(self) -> bool:
        api_client = get_api_client(ConnectionConfig())
        alias_response = await get_templates_aliases_alias.asyncio_detailed(
            alias=self._template_name,
            client=api_client,
        )
        if alias_response.status_code == 404:
            return False
        if alias_response.status_code == 403:
            return True
        if alias_response.status_code >= 300 or alias_response.parsed is None:
            raise RuntimeError(f"E2B template alias查询失败: {self._template_name}, HTTP {alias_response.status_code}")
        template_response = await get_templates_template_id.asyncio_detailed(
            template_id=alias_response.parsed.template_id,
            client=api_client,
        )
        if template_response.status_code >= 300 or template_response.parsed is None:
            raise RuntimeError(f"E2B template构建状态查询失败: {self._template_name}, HTTP {template_response.status_code}")
        return any(build.status.value == "ready" for build in template_response.parsed.builds)

    @retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    async def _create_sandbox(self) -> None:
        metadata = {
            "environment_name": self.environment_name,
            "session_id": self.session_id,
        }
        self._sandbox = await AsyncSandbox.create(
            template=self._template_name,
            metadata=metadata,
            timeout=self.sandbox_timeout_seconds,
            allow_internet_access=self.task_env_config.allow_internet,
        )
