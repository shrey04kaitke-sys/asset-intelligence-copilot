"""Runtime configuration. Secrets come only from the environment (never logged)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env reader; real environment variables win."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv(ROOT / ".env")


@dataclass
class Settings:
    dataset_dir: Path = field(default_factory=lambda: Path(os.environ.get("COPILOT_DATASET_DIR", ROOT / "dataset" / "sample-data")))
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("COPILOT_DATA_DIR", ROOT / "data")))
    today_override: str | None = field(default_factory=lambda: os.environ.get("COPILOT_TODAY") or None)
    provider: str = field(default_factory=lambda: os.environ.get("COPILOT_PROVIDER", "gemma_api"))
    model: str = field(default_factory=lambda: os.environ.get("COPILOT_MODEL", "gemma-4-31b-it"))
    ollama_base_url: str = field(default_factory=lambda: os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1"))
    max_tool_steps: int = 6
    llm_timeout_s: float = 90.0

    @property
    def source_db(self) -> Path:
        return Path(self.data_dir) / "source.db"

    @property
    def state_db(self) -> Path:
        return Path(self.data_dir) / "state.db"

    def api_key(self) -> str | None:
        """Read at call time so the key is never stored on a long-lived object."""
        return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or None
