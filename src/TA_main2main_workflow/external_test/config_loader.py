"""External test configuration loader and validation.

Parses ``external_test_config.yaml`` into typed dataclasses and validates
URL / path correctness before the runner consumes them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from TA_main2main_workflow.utils.logging import get_logger

log = get_logger(__name__)

# ── Default config shipped with the package ───────────────────────────────
_DEFAULT_CONFIG_DIR = Path(__file__).resolve().parent
_DEFAULT_CONFIG_PATH = _DEFAULT_CONFIG_DIR / "external_test_config.yaml"


@dataclass
class ExternalTestRepoConfig:
    """Configuration for a single external operator repository."""

    name: str  # display name
    url: str  # git clone URL
    branch: str = "main"  # branch / tag to checkout
    test_cases: list[str] = field(default_factory=list)  # test file relative paths
    install_cmd: str = ""  # optional dep install (empty = skip)


@dataclass
class ExternalTestConfig:
    """Top-level external test configuration."""

    enabled: bool = False  # master on/off switch
    repos: list[ExternalTestRepoConfig] = field(default_factory=list)
    test_procs: int = 8  # pytest -n <N>
    mode: str = "inline"  # inline | standalone | off
    max_retries: int = 5  # AI fix retries per repo
    timeout: int = 7200  # per-repo test timeout (seconds)


# ═══════════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════════


def load_external_test_config(path: str = "") -> ExternalTestConfig | None:
    """Load external test config from *path* (YAML).

    Resolution order:
        1. Explicit *path* argument
        2. ``TA_EXTERNAL_TEST_CONFIG`` environment variable
        3. Default ``external_test/external_test_config.yaml`` shipped with package

    Returns ``None`` when the config file does not exist (not an error — the
    caller treats missing config as "no external tests configured").
    """
    resolved = _resolve_config_path(path)
    if resolved is None:
        return None

    log.info(f"Loading external test config: {resolved}")
    try:
        raw = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        log.error(f"Failed to parse external test config: {exc}")
        return None

    cfg = _dict_to_config(raw)

    # Merge env-var overrides (env takes precedence over YAML)
    _apply_env_overrides(cfg)

    if not validate_config(cfg):
        return None

    log.key_value("External test enabled", str(cfg.enabled))
    log.key_value("External test mode", cfg.mode)
    log.key_value("External test repos", str(len(cfg.repos)))
    return cfg


def validate_config(cfg: ExternalTestConfig) -> bool:
    """Validate the loaded config. Returns True if usable."""
    if cfg.mode not in ("inline", "standalone", "off"):
        log.error(f"Invalid external test mode: {cfg.mode}")
        return False

    if cfg.test_procs < 1:
        log.error("test_procs must be >= 1")
        return False

    if cfg.max_retries < 0:
        log.error("max_retries must be >= 0")
        return False

    for repo in cfg.repos:
        if not repo.name:
            log.error("External test repo missing 'name'")
            return False
        if not repo.url or not (
            repo.url.startswith("http") or repo.url.startswith("git@")
        ):
            log.error(f"Invalid repo URL for '{repo.name}': {repo.url}")
            return False
        if not repo.test_cases:
            log.warning(f"External repo '{repo.name}' has no test_cases configured")

    return True


# ═══════════════════════════════════════════════════════════════════════════
# Internal helpers
# ═══════════════════════════════════════════════════════════════════════════


def _resolve_config_path(explicit: str) -> Path | None:
    """Determine which config file to read."""
    if explicit:
        p = Path(explicit)
        if p.exists():
            return p
        log.warning(f"External test config not found: {explicit}")
        return None

    env_path = os.getenv("TA_EXTERNAL_TEST_CONFIG", "")
    if env_path:
        p = Path(env_path)
        if p.exists():
            return p
        log.warning(f"TA_EXTERNAL_TEST_CONFIG points to missing file: {env_path}")

    if _DEFAULT_CONFIG_PATH.exists():
        return _DEFAULT_CONFIG_PATH

    return None


def _dict_to_config(raw: dict[str, Any]) -> ExternalTestConfig:
    """Convert raw YAML dict to ExternalTestConfig."""
    repos: list[ExternalTestRepoConfig] = []
    for item in raw.get("external_test_repos", []) or []:
        repos.append(
            ExternalTestRepoConfig(
                name=item.get("name", ""),
                url=item.get("url", ""),
                branch=item.get("branch", "main"),
                test_cases=item.get("test_cases", []),
                install_cmd=item.get("install_cmd", ""),
            )
        )

    return ExternalTestConfig(
        enabled=bool(raw.get("enabled", False)),
        repos=repos,
        test_procs=int(raw.get("test_procs", 8)),
        mode=str(raw.get("mode", "inline")),
        max_retries=int(raw.get("max_retries", 5)),
        timeout=int(raw.get("timeout", 7200)),
    )


def _apply_env_overrides(cfg: ExternalTestConfig) -> None:
    """Apply environment variable overrides on top of YAML config.

    Environment variables always take precedence over the YAML file.
    """
    # Master switch
    env_enabled = os.getenv("TA_EXTERNAL_TEST_ENABLED", "").lower()
    if env_enabled in ("true", "1", "yes"):
        cfg.enabled = True
    elif env_enabled in ("false", "0", "no"):
        cfg.enabled = False

    # Mode
    env_mode = os.getenv("TA_EXTERNAL_TEST_MODE", "").lower()
    if env_mode in ("inline", "standalone", "off"):
        cfg.mode = env_mode

    # Parallelism
    try:
        cfg.test_procs = int(os.getenv("TA_EXTERNAL_TEST_PROCS", str(cfg.test_procs)))
    except ValueError:
        pass

    # Max retries
    try:
        cfg.max_retries = int(
            os.getenv("TA_EXTERNAL_TEST_MAX_RETRIES", str(cfg.max_retries))
        )
    except ValueError:
        pass

    # Timeout
    try:
        cfg.timeout = int(os.getenv("TA_EXTERNAL_TEST_TIMEOUT", str(cfg.timeout)))
    except ValueError:
        pass
