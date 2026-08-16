"""djtool.toml configuration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from djtool.errors import ConfigError
from djtool.state import project_dir

try:
    import tomllib

    HAVE_TOML = True
except ImportError:  # pragma: no cover - Python < 3.11
    HAVE_TOML = False
CONFIG_FILE_NAME = "djtool.toml"
def load_config() -> dict[str, Any]:
    """Load <project>/djtool.toml. Returns {} when the file is absent."""
    p = config_path()
    if not p.exists():
        return {}
    if not HAVE_TOML:
        raise ConfigError("djtool.toml needs Python 3.11+ (tomllib) to parse")
    try:
        return tomllib.loads(p.read_text())
    except tomllib.TOMLDecodeError as e:  # type: ignore[attr-defined]
        raise ConfigError(f"invalid {CONFIG_FILE_NAME}: {e}") from None


def config_path() -> Path:
    return project_dir() / CONFIG_FILE_NAME


@dataclass
class SyncConfig:
    remote: str
    remote_dj: str
    local_mixxx: str | None = None
    remote_mixxx: str | None = None


def load_sync_config(root: Path) -> tuple[SyncConfig | None, str]:
    cfg = load_config()
    section = cfg.get("sync") or {}
    missing = [k for k in ("remote", "remote_dj") if not section.get(k)]
    if missing:
        return None, f"[sync] in {CONFIG_FILE_NAME} ({config_path()}) is missing: {', '.join(missing)}"
    return SyncConfig(
        remote=section["remote"],
        remote_dj=section["remote_dj"],
        local_mixxx=section.get("local_mixxx") or None,
        remote_mixxx=section.get("remote_mixxx") or None,
    ), ""

