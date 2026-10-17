"""Runtime configuration. Secrets come only from the environment (never logged)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")


def _decode_env_bytes(raw: bytes) -> str:
    """Decode a .env file saved by common Windows tools (UTF-8, UTF-8 with BOM, or UTF-16 from PowerShell redirects)."""
    if raw.startswith(_UTF16_BOMS):
        return raw.decode("utf-16", errors="replace")
    if b"\x00" in raw[:200]:                       # UTF-16 without a BOM
        return raw.decode("utf-16-le", errors="replace")
    return raw.decode("utf-8-sig", errors="replace")


def parse_dotenv(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.lower().startswith("export "):
            line = line[7:].lstrip()
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        elif " #" in v:
            v = v.split(" #", 1)[0].rstrip()
        out[k.strip()] = v
    return out


def _load_dotenv(path: Path) -> None:
    """Minimal .env reader; real environment variables win and empty values never override anything."""
    if not path.is_file():
        return
    for k, v in parse_dotenv(_decode_env_bytes(path.read_bytes())).items():
        if v and not os.environ.get(k):
            os.environ[k] = v


_load_dotenv(ROOT / ".env")


@dataclass
class Settings:
    dataset_dir: Path = field(default_factory=lambda: Path(os.environ.get("COPILOT_DATASET_DIR", ROOT / "dataset" / "sample-data")))
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("COPILOT_DATA_DIR", ROOT / "data")))
    today_override: str | None = field(default_factory=lambda: os.environ.get("COPILOT_TODAY") or None)
    provider: str = field(default_factory=lambda: os.environ.get("COPILOT_PROVIDER", "gemma_api"))
    model: str = field(default_factory=lambda: os.environ.get("COPILOT_MODEL", "gemma-4-26b-a4b-it"))
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
        for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
            v = (os.environ.get(name) or "").strip().strip("\"'").strip()
            if v:
                return v
        return None
