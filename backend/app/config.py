"""Environment and runtime configuration loader."""
from pathlib import Path
import os
from typing import Literal

from pydantic import BaseModel, Field
from app.adapters.demo_setup import DemoScenario

ExecutionMode = Literal["backend_stub", "mock_llm", "real_llm"]


def find_repo_root() -> Path:
    """Find repository root by looking for .git or GEMINI.md or docs/."""
    current = Path(__file__).resolve().parent
    for parent in [current] + list(current.parents):
        if (parent / ".git").exists() or (parent / "docs").exists():
            return parent
    return current.parent.parent


def load_env_file(filepath: Path) -> dict[str, str]:
    """Parse a simple .env file without external dependencies."""
    if not filepath.is_file():
        return {}
    values: dict[str, str] = {}
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip("'\"")
                values[k] = v
    return values


class Settings(BaseModel):
    llm_provider: Literal["anthropic", "openai"] = "anthropic"
    anthropic_api_key: str = Field(default="", repr=False)
    claude_model: str = "claude-haiku-5-5"
    openai_api_key: str = Field(default="", repr=False)
    openai_model: str = Field(default="gpt-6-luna")
    openai_reasoning_effort: str = Field(default="low")
    execution_mode: ExecutionMode = Field(default="backend_stub")
    demo_enabled: bool = False
    mock_scenario: DemoScenario = "normal"
    repo_root: Path = Field(default_factory=find_repo_root)

    @classmethod
    def load(cls) -> "Settings":
        root = find_repo_root()
        env_vars = load_env_file(root / ".env")

        # System environment variables take precedence over .env file
        api_key = os.environ.get("OPENAI_API_KEY", env_vars.get("OPENAI_API_KEY", ""))
        model = os.environ.get("OPENAI_MODEL", env_vars.get("OPENAI_MODEL", "gpt-6-luna"))
        reasoning = os.environ.get(
            "OPENAI_REASONING_EFFORT", env_vars.get("OPENAI_REASONING_EFFORT", "low")
        )
        mode = os.environ.get(
            "TOUCHBACK_EXECUTION_MODE", env_vars.get("TOUCHBACK_EXECUTION_MODE", "backend_stub")
        )
        if mode not in ("backend_stub", "mock_llm", "real_llm"):
            mode = "backend_stub"

        demo = os.environ.get("TOUCHBACK_DEMO_ENABLED", env_vars.get("TOUCHBACK_DEMO_ENABLED", "false")).lower()
        if demo not in ("true", "false", "1", "0"):
            raise ValueError("TOUCHBACK_DEMO_ENABLED must be true/false or 1/0")
        scenario = os.environ.get("TOUCHBACK_MOCK_SCENARIO", env_vars.get("TOUCHBACK_MOCK_SCENARIO", "normal"))
        return cls(
            llm_provider=os.environ.get("TOUCHBACK_LLM_PROVIDER", env_vars.get("TOUCHBACK_LLM_PROVIDER", "anthropic")),
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", env_vars.get("ANTHROPIC_API_KEY", "")),
            claude_model=os.environ.get("CLAUDE_MODEL", env_vars.get("CLAUDE_MODEL", "claude-haiku-5-5")),
            demo_enabled=demo in ("true", "1"),
            mock_scenario=scenario,
            openai_api_key=api_key,
            openai_model=model,
            openai_reasoning_effort=reasoning,
            execution_mode=mode,  # type: ignore[arg-type]
            repo_root=root,
        )

    @property
    def model_name(self) -> str:
        return self.claude_model if self.llm_provider == "anthropic" else self.openai_model

    def is_llm_configured(self) -> bool:
        key = self.anthropic_api_key if self.llm_provider == "anthropic" else self.openai_api_key
        return bool(key.strip())

    def is_openai_configured(self) -> bool:
        return bool(self.openai_api_key and self.openai_api_key.strip())


settings = Settings.load()
