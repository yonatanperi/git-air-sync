"""Persistent settings and sync state.

This file is less "configuration" than a small state database: it records, per
project, the last commit that crossed the air gap. Losing or corrupting it means a
full resync, so writes are atomic and a re-read/merge happens immediately before every
save to keep two concurrent runs from clobbering each other.

``AIR_SYNC_CONFIG`` overrides the location. That is a supported feature, not a test
hook — it is what lets you drive both machine roles from one box.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

CONFIG_VERSION = 1
DEFAULT_MAX_PAYLOAD_MB = 25
# Files that are never part of the sync contract: stripped from the patch series
# at export time, and kept-local if one still shows up conflicted at import time
# (see core/patterns.py and core/sync.py's _apply_with_auto_resolve).
DEFAULT_EXCLUDE_PATTERNS = ["CLAUDE.md", ".claude/**"]


def config_path() -> Path:
    override = os.environ.get("AIR_SYNC_CONFIG")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".air_sync_config.json"


def resolve_path(value: str | os.PathLike[str] | None) -> Path | None:
    if value is None:
        return None
    return Path(str(value)).expanduser()


@dataclass
class ProjectState:
    path: str | None = None
    last_synced_commit: str | None = None
    last_synced_branch: str | None = None
    last_sync_at: str | None = None
    last_payload_sha256: str | None = None
    # This machine's own branch HEAD immediately after the last successful import
    # (`git am`-applied, so it never matches `last_synced_commit`, which is the
    # exporting machine's hash). Used to detect local commits made since then —
    # see `status` in main.py.
    last_import_head: str | None = None
    pending_conflict: dict[str, Any] | None = None
    # Additions on top of Config.exclude_patterns, for this project only.
    exclude_patterns: list[str] = field(default_factory=list)


@dataclass
class Config:
    version: int = CONFIG_VERSION
    machine_role: str | None = None  # "A" (export) | "B" (import)
    projects_root: str | None = None
    default_project: str | None = None
    drop_folder: str | None = None
    export_output_dir: str | None = None
    max_payload_mb: int = DEFAULT_MAX_PAYLOAD_MB
    export_refs: str = "branch"  # "branch" | "all"
    exclude_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE_PATTERNS))
    projects: dict[str, ProjectState] = field(default_factory=dict)
    _unknown: dict[str, Any] = field(default_factory=dict, repr=False)

    # ------------------------------------------------------------------ accessors

    def effective_exclude_patterns(self, project_name: str) -> list[str]:
        """This project's exclude patterns: the global list plus its own
        additions, deduped. Never overrides — a project can only add patterns,
        not un-exclude a globally excluded one."""
        from .core import patterns

        state = self.projects.get(project_name)
        extra = state.exclude_patterns if state else []
        return patterns.dedupe([*self.exclude_patterns, *extra])

    @property
    def role_label(self) -> str:
        if self.machine_role == "A":
            return "COMPUTER A · EXPORT"
        if self.machine_role == "B":
            return "COMPUTER B · IMPORT"
        return "UNCONFIGURED"

    def project(self, name: str) -> ProjectState:
        return self.projects.setdefault(name, ProjectState())

    def project_path(self, name: str) -> Path | None:
        state = self.projects.get(name)
        if state and state.path:
            return resolve_path(state.path)
        root = resolve_path(self.projects_root)
        if root:
            candidate = root / name
            if candidate.exists():
                return candidate
        return None

    def output_dir(self) -> Path:
        target = self.export_output_dir or self.drop_folder
        return resolve_path(target) or (Path.home() / "AirSyncOut")

    def inbox_dir(self) -> Path:
        target = self.drop_folder or self.export_output_dir
        return resolve_path(target) or (Path.home() / "AirSyncDrop")


# ------------------------------------------------------------------------- loading


def exists() -> bool:
    return config_path().is_file()


def load() -> Config:
    """Never raises on a missing file; returns defaults instead."""
    path = config_path()
    if not path.is_file():
        return Config()

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(
            f"{path} could not be read ({exc}).\n"
            "Fix or delete the file — deleting it loses your recorded sync positions, "
            "which means the next export will be a full resync."
        ) from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"{path} does not contain a JSON object.")

    return _from_dict(migrate(raw))


class ConfigError(Exception):
    """The config file exists but is unusable."""


def migrate(raw: dict[str, Any]) -> dict[str, Any]:
    """Bring an older config forward. Currently only v1 exists."""
    raw.setdefault("version", CONFIG_VERSION)
    return raw


_KNOWN = {f for f in Config.__dataclass_fields__ if not f.startswith("_")}


def _from_dict(raw: dict[str, Any]) -> Config:
    projects: dict[str, ProjectState] = {}
    for name, value in (raw.get("projects") or {}).items():
        if isinstance(value, str):
            # Tolerate the simplest possible shape: {"alpha": "<sha>"}
            projects[name] = ProjectState(last_synced_commit=value)
        elif isinstance(value, dict):
            fields = {
                k: v for k, v in value.items() if k in ProjectState.__dataclass_fields__
            }
            projects[name] = ProjectState(**fields)

    known = {k: v for k, v in raw.items() if k in _KNOWN and k != "projects"}
    unknown = {k: v for k, v in raw.items() if k not in _KNOWN}
    return Config(**known, projects=projects, _unknown=unknown)


def _to_dict(cfg: Config) -> dict[str, Any]:
    data = {k: v for k, v in asdict(cfg).items() if not k.startswith("_")}
    data["projects"] = {
        name: {k: v for k, v in asdict(state).items() if v is not None}
        for name, state in cfg.projects.items()
    }
    data.update(cfg._unknown)  # preserve keys written by a newer version
    return data


# ------------------------------------------------------------------------- saving


def save(cfg: Config) -> None:
    """Atomic write, 0600.

    Re-reads the on-disk file first and merges in any *other* projects it has gained,
    so two runs touching different projects can't erase each other's state.
    """
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.is_file():
        try:
            on_disk = _from_dict(migrate(json.loads(path.read_text(encoding="utf-8"))))
            for name, state in on_disk.projects.items():
                cfg.projects.setdefault(name, state)
        except (OSError, json.JSONDecodeError, TypeError):
            pass  # a corrupt file shouldn't block writing a good one

    payload = json.dumps(_to_dict(cfg), indent=2, sort_keys=True) + "\n"

    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".air_sync_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)  # contains filesystem paths to every project
        os.replace(tmp_name, path)
    except OSError:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def update_project(cfg: Config, name: str, **fields: Any) -> Config:
    state = cfg.project(name)
    for key, value in fields.items():
        if key not in ProjectState.__dataclass_fields__:
            raise KeyError(f"unknown project field: {key}")
        setattr(state, key, value)
    save(cfg)
    return cfg


# --------------------------------------------------------------------- validation


def validate_projects_root(value: str) -> tuple[bool, str]:
    path = resolve_path(value)
    if path is None or not str(path).strip():
        return False, "Path is empty."
    if not path.exists():
        return False, f"{path} does not exist."
    if not path.is_dir():
        return False, f"{path} is not a directory."
    if not os.access(path, os.R_OK):
        return False, f"{path} is not readable."
    return True, ""


def validate_writable_dir(value: str) -> tuple[bool, str]:
    path = resolve_path(value)
    if path is None or not str(path).strip():
        return False, "Path is empty."
    if path.exists():
        if not path.is_dir():
            return False, f"{path} is not a directory."
        if not os.access(path, os.W_OK):
            return False, f"{path} is not writable."
        return True, ""
    parent = path.parent
    if not parent.exists():
        return False, f"{parent} does not exist."
    if not os.access(parent, os.W_OK):
        return False, f"{parent} is not writable, so {path.name} cannot be created."
    return True, ""
