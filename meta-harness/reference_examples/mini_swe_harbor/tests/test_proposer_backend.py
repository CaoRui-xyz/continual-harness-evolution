from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import proposer_backend


def test_skill_directory_loads_its_skill_markdown(tmp_path: Path) -> None:
    skill = tmp_path / "meta-harness-mini" / "SKILL.md"
    skill.parent.mkdir()
    skill.write_text("follow this exact proposal protocol", encoding="utf-8")

    loaded = proposer_backend._load_skills([str(skill.parent)])

    assert "## Skill: SKILL" in loaded
    assert "follow this exact proposal protocol" in loaded


def test_missing_proposer_key_fails_before_provider_call(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    result = proposer_backend.run("write pending_eval.json", cwd=str(tmp_path))
    assert result.exit_code == 2
    assert "DEEPSEEK_API_KEY" in result.stderr


def test_litellm_backend_executes_bash_and_finishes(monkeypatch, tmp_path: Path) -> None:
    class FakeLiteLLM:
        def __init__(self) -> None:
            self.calls = 0
            self.requests = []

        def completion(self, **kwargs):
            self.requests.append(kwargs)
            self.calls += 1
            if self.calls == 1:
                call = SimpleNamespace(
                    id="call-1",
                    function=SimpleNamespace(
                        name="bash",
                        arguments='{"command":"printf done > marker.txt"}',
                    ),
                )
                message = SimpleNamespace(role="assistant", content=None, tool_calls=[call])
            else:
                message = SimpleNamespace(role="assistant", content="candidate complete", tool_calls=[])
            return SimpleNamespace(
                choices=[SimpleNamespace(message=message)],
                usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
            )

    fake = FakeLiteLLM()
    monkeypatch.setitem(sys.modules, "litellm", fake)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("mandatory skill instruction", encoding="utf-8")

    result = proposer_backend.run(
        "implement one candidate",
        cwd=str(tmp_path),
        skills=[str(skill_dir)],
        max_steps=2,
    )

    assert result.exit_code == 0
    assert result.tool_calls == 1
    assert result.token_usage == {"input_tokens": 20, "output_tokens": 10}
    assert (tmp_path / "marker.txt").read_text() == "done"
    assert "mandatory skill instruction" in fake.requests[0]["messages"][0]["content"]
