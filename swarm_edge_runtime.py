"""Shared runtime configuration and filesystem paths for Swarm Edge.

The application historically resolved most state paths from the process working
directory.  This module centralizes those paths while retaining the existing
checkout-relative layout as the compatibility default.  Production deployments
must provide absolute path overrides so a service cannot silently create state
under an unexpected working directory.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, MutableMapping, Optional


PROJECT_ROOT = Path(__file__).resolve().parent

RUNTIME_ENV_FILE = "BGL_RUNTIME_ENV_FILE"
PRODUCTION_MODE = "BGL_PRODUCTION_MODE"
RUNTIME_MODE = "BGL_RUNTIME_MODE"

PATH_ENV_VARS = (
    "BGL_DB_PATH",
    "BGL_LOG_DIR",
    "BGL_SIGNALS_DIR",
    "BGL_REPORT_DIR",
    "BGL_EXPORT_DIR",
    "BGL_BACKUP_DIR",
    "BGL_RUNTIME_DIR",
    "BGL_WATCHLIST_PATH",
    RUNTIME_ENV_FILE,
)


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _is_production(environ: Mapping[str, str]) -> bool:
    return _truthy(environ.get(PRODUCTION_MODE)) or str(
        environ.get(RUNTIME_MODE, "")
    ).strip().lower() in {"production", "prod"}


def _strip_optional_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        if not separator:
            continue
        key = key.strip()
        if not key or not key.replace("_", "").isalnum() or key[0].isdigit():
            continue
        values[key] = _strip_optional_quotes(value)
    return values


def _candidate_env_file(environ: Mapping[str, str]) -> Optional[Path]:
    configured = environ.get(RUNTIME_ENV_FILE, "").strip()
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return path

    for candidate in (PROJECT_ROOT / ".env.runtime", PROJECT_ROOT / ".env"):
        if candidate.is_file():
            return candidate
    return None


def load_runtime_environment(
    environ: Optional[MutableMapping[str, str]] = None,
) -> Optional[Path]:
    """Load the runtime env file without overriding explicit environment values.

    ``.env.runtime`` is preferred over the legacy ``.env`` so direct execution
    and launchd execution use the same configuration.  Existing process
    environment values retain precedence over file values.
    """

    target: MutableMapping[str, str] = os.environ if environ is None else environ
    path = _candidate_env_file(target)
    if path is None:
        return None
    if not path.is_file():
        # An explicitly configured file may be provisioned after the process
        # environment is assembled (for example by launchd).  Keep direct
        # execution usable and let the caller's environment provide values.
        return path

    file_values = _read_env_file(path)
    for key, value in file_values.items():
        target.setdefault(key, value)
    target.setdefault(RUNTIME_ENV_FILE, str(path))
    return path


def _resolve_path(
    value: Optional[str],
    default: Path,
    *,
    variable: str,
    production: bool,
) -> Path:
    if value is None or not value.strip():
        return default
    raw = value.strip()
    path = Path(raw).expanduser()
    if production and not path.is_absolute():
        raise ValueError(
            f"{variable} must be an absolute path in production mode: {raw!r}"
        )
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


@dataclass(frozen=True)
class RuntimePaths:
    """All filesystem locations used by the runtime and operational tools."""

    root: Path
    db_path: Path
    log_dir: Path
    signals_dir: Path
    report_dir: Path
    export_dir: Path
    backup_dir: Path
    runtime_dir: Path
    watchlist_path: Path
    runtime_env_file: Path

    @property
    def data_dir(self) -> Path:
        return self.export_dir

    @property
    def migrations_dir(self) -> Path:
        return self.root / "migrations"


def get_runtime_paths(
    environ: Optional[MutableMapping[str, str]] = None,
    *,
    load_env: bool = True,
) -> RuntimePaths:
    """Resolve runtime paths from environment with compatibility defaults.

    Precedence is: explicit process environment, runtime env file, then the
    historical checkout-relative defaults.  Relative overrides remain supported
    for compatibility; production mode rejects them.
    """

    target: MutableMapping[str, str] = os.environ if environ is None else environ
    env_file = load_runtime_environment(target) if load_env else _candidate_env_file(target)
    production = _is_production(target)

    default_env = env_file or (PROJECT_ROOT / ".env.runtime")
    return RuntimePaths(
        root=PROJECT_ROOT,
        db_path=_resolve_path(
            target.get("BGL_DB_PATH"),
            PROJECT_ROOT / "memory" / "runs.sqlite",
            variable="BGL_DB_PATH",
            production=production,
        ),
        log_dir=_resolve_path(
            target.get("BGL_LOG_DIR"),
            PROJECT_ROOT / "logs",
            variable="BGL_LOG_DIR",
            production=production,
        ),
        signals_dir=_resolve_path(
            target.get("BGL_SIGNALS_DIR"),
            PROJECT_ROOT / "signals",
            variable="BGL_SIGNALS_DIR",
            production=production,
        ),
        report_dir=_resolve_path(
            target.get("BGL_REPORT_DIR"),
            PROJECT_ROOT / "reports",
            variable="BGL_REPORT_DIR",
            production=production,
        ),
        export_dir=_resolve_path(
            target.get("BGL_EXPORT_DIR"),
            PROJECT_ROOT / "data",
            variable="BGL_EXPORT_DIR",
            production=production,
        ),
        backup_dir=_resolve_path(
            target.get("BGL_BACKUP_DIR"),
            PROJECT_ROOT / "backups",
            variable="BGL_BACKUP_DIR",
            production=production,
        ),
        runtime_dir=_resolve_path(
            target.get("BGL_RUNTIME_DIR"),
            PROJECT_ROOT / "runtime",
            variable="BGL_RUNTIME_DIR",
            production=production,
        ),
        watchlist_path=_resolve_path(
            target.get("BGL_WATCHLIST_PATH"),
            PROJECT_ROOT / "markets" / "polymarket_watchlist.json",
            variable="BGL_WATCHLIST_PATH",
            production=production,
        ),
        runtime_env_file=_resolve_path(
            target.get(RUNTIME_ENV_FILE),
            default_env,
            variable=RUNTIME_ENV_FILE,
            production=production,
        ),
    )


# Import-time aliases preserve the existing module-level constants used by
# callers and tests while all defaults now come from this resolver.
RUNTIME_PATHS = get_runtime_paths()
