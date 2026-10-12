"""Environment and runtime configuration loader."""
from pathlib import Path
import os
from typing import Literal

from pydantic import BaseModel, Field

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
    openai_api_key: str = Field(default="")
    openai_model: str = Field(default="gpt-6-luna")
    openai_reasoning_effort: str = Field(default="low")
    execution_mode: ExecutionMode = Field(default="backend_stub")
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

        return cls(
            openai_api_key=api_key,
            openai_model=model,
            openai_reasoning_effort=reasoning,
            execution_mode=mode,  # type: ignore[arg-type]
            repo_root=root,
        )

    def is_openai_configured(self) -> bool:
        return bool(self.openai_api_key and self.openai_api_key.strip())


settings = Settings.load()
