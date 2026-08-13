"""External operator repository pluggable test cases.

Provides config loading and a runner for executing pytest suites
in external (non-vendored) operator repositories, with AI-driven
fix retry loops on failure.

Core entry points:
    - :func:`load_external_test_config` — parse a YAML config file
    - :func:`run_external_tests` — clone, install, test, fix (per repo)
    - :class:`ExternalTestConfig` / :class:`ExternalTestRepoConfig` — dataclasses
"""

from TA_main2main_workflow.external_test.config_loader import (
    ExternalTestConfig,
    ExternalTestRepoConfig,
    load_external_test_config,
)
from TA_main2main_workflow.external_test.runner import run_external_tests

__all__ = [
    "ExternalTestConfig",
    "ExternalTestRepoConfig",
    "load_external_test_config",
    "run_external_tests",
]
