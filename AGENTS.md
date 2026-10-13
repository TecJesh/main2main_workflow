# AGENTS.md

CrewAI Flow that automates triton-ascend's main2main upstream sync against upstream Triton. Drives an external AI adapter (opencode or claude) as a subprocess; everything else is deterministic Python.

## Run

Install once: `pip install -e .` (registers `ta-kickoff`, `ta-pipeline-test` and `ta-plot` console scripts).

Real entrypoint is `TA_Main2MainFlow` in `src/TA_main2main_workflow/flow.py`. Use:

```bash
ta-kickoff --triton-ascend-path <path> --triton-path <path> [--target-commit SHA]
```

Both repos must be real git checkouts. triton HEAD is the implicit target unless `--target-commit` is given.

### Pipeline test (`ta-pipeline-test`)

A separate full-test-pipeline flow, independent of the main2main sync:

```bash
ta-pipeline-test --triton-ascend-path <path>
```

`TA_PIPELINE_TEST_SCRIPT` (required) points at a human-configured shell
script that clones external test repos (e.g. Q3TritonKernel) into
`~/test_workspace` (`TA_TEST_WORKSPACE`) and runs pytest, logging to
`~/test_workspace/test_log/<repo>_test_log/`. The flow first creates
the work branch (`fix/pipeline-test-<ts>`, base
`{TA_WORK_BRANCH_BASE}/{TA_BASE_BRANCH}`) and **compiles + installs
triton-ascend via `build_and_fix_loop` BEFORE running any test**
(`SKIP_BUILD=true` skips) — the test script runs against the installed
package. Then it: collects logs → AI analyzes (`pipeline_test_analyze`
mode) → writes two Excel reports (`~/test_workspace/analysis/错误信息总览表.xlsx`
+ `错误信息汇总表.xlsx`) → fixes failing cases one by one
(`pipeline_test_fix` mode, category: operator / triton_ascend / npu_ir;
rebuild + AI-generated precise rerun command; **one commit per fixed
case**) → reruns the full script until green (bounded by
`TA_PIPELINE_TEST_ROUNDS` + one final verification run) → PR via
`pipeline_test_report` mode only when code changed. Tests pass as-is →
no PR, empty work branch deleted, exit 0. Rounds exhausted with
failures → PR lists them as Known Issues, exit 1. See `docs/guide.md`
for the full spec and the env-var table.

The flow lives in `src/TA_main2main_workflow/pipeline_test/` (flow /
logs / script_runner / analyze / fix_loop) with the CLI in
`pipeline_test_main.py`. It reuses the main flow's machinery and config
semantics where they apply: work branch base
`{TA_WORK_BRANCH_BASE}/{TA_BASE_BRANCH}` (via
`merge._create_work_branch`), PR base `TA_PR_BASE_BRANCH`, plus
`build_triton`, `commit_fixes`, `regen_ai_fixes`, `parse_patches`,
`validate_fix`-style validation, and `push_and_create_pr` (new optional
`title` param — backward compatible). Pipeline-test specifics: PR title
identity is `[PipelineTest](fix) ...` (NOT the main flow's PR_AUTHOR
"Sync"), plus `TA_PIPELINE_TEST_SCRIPT`, `TA_TEST_WORKSPACE`,
`TA_PIPELINE_TEST_MAX_RETRIES`, `TA_PIPELINE_TEST_ROUNDS`,
`TA_PIPELINE_TEST_TIMEOUT`, `TA_PIPELINE_TEST_PR_TITLE`.

## Layout (only the non-obvious bits)

- `src/TA_main2main_workflow/flow.py` — the Flow; node order: `initialize → detect_commits → execute_sync → push_to_github / handle_failure`. `execute_sync` is a single `@router` node that internally calls `_do_merge → _do_resolve_conflicts → _do_build_and_fix_loop → _do_finalize`. Routing uses string signals defined in `utils.py` (`UpgradeCompleted`, `UpgradeFailed`, `HasNewCommits`, `HasNoNewCommits`) — match them exactly.
- `src/TA_main2main_workflow/scripts/` — deterministic helpers (`detect_commits`, `merge_upstream`, `build_test`, `pre_ci_check`, `push_to_github`, `update_commit_reference`). Import them, don't shell out.
- `src/TA_main2main_workflow/agent/opencode_adapter.py` — spawns `opencode run --format json --dangerously-skip-permissions`, streams JSONL, 30 min total / 5 min stale timeouts, up to 3 stale retries with a continue-prompt.
- `src/TA_main2main_workflow/agent/prompt.md` — single-agent prompt (do NOT use TeamCreate/Agent sub-tools); formatted with `str.format_map`, so any literal `{}` in this file must be escaped as `{{ }}`.
- `src/TA_main2main_workflow/reference/` — adapt/diagnosis/error-pattern guides consumed by the agent. Update these when new error patterns appear; they are the durable knowledge base.
- `docs/guide.md` — long-form spec. When in doubt about behavior, trust `flow.py` + `utils.py` over any other doc.

## workspace/ is volatile

`initialize` **deletes and recreates** `workspace/` on every run. Never put anything there you want to keep. All step artifacts (`workspace/merge_result.json`, `workspace/build_result.json`, `workspace/test_result.json`, `workspace/detect.json`, `workspace/conflicts/`, `workspace/fixes/`, `workspace/step-0/step_summary.md`, `workspace/step-0/step_target.patch`, `workspace/final_summary.md`, `workspace/final_target.patch`, `workspace/test-logs/`) live under it. Filenames are centralised as constants in `utils.py` — reuse them, don't hardcode strings.

## State & path constants

- `WORKSPACE_DIR = <repo>/workspace` (computed from `__file__`, not cwd).
- Path resolution priority in `initialize`: CLI arg → env var (`TRITON_ASCEND_PATH`, `TRITON_PATH`, `TRITON_TARGET_COMMIT`) → cwd.
- `initialize` records `original_branch` and `ascend_head`; `_do_finalize` restores `original_branch` (`git checkout`).
- Work branch is always created from the latest `main` of `triton-lang/triton-ascend` (fetched fresh via `_ensure_upstream_ascend_remote`).

## Retry & test loop semantics

`_do_build_and_fix_loop`: build → test first. Pass → `_commit_fixes` then return. Fail → `_do_ai_fix` (opencode in fix mode) → build → test. At `retry_count >= max_retries` the entire flow short-circuits to `UpgradeFailed`.

Inside `_do_ai_fix`, opencode is called once per fix attempt. The AI modifies source files directly; after build + test both pass, `_commit_fixes` commits with a descriptive message derived from the AI-generated `step_summary.md`.

## Git safety — no test artifacts in commits

All `git add` calls use `git add -u` (tracked-only) to avoid staging test artifacts, cache files, `__pycache__/`, or other transient files created during build/test. New untracked files created during the flow will NOT be committed.

## Env flags worth knowing

| Var | Effect |
|---|---|
| `SKIP_AI_ANALYSIS=true` | Bypass AI entirely; only deterministic ops run. Useful for debugging the Flow plumbing. |
| `SKIP_BUILD=true` | Skip the build step (treat as passed). |
| `SKIP_E2E_TEST=true` | `_do_test` returns None (treat as passed). |
| `SKIP_LLVM20COMPAT=true` | Skip the LLVM20Compat FileCheck guard (hard gate). |
| `TA_LLVM20COMPAT_MAX_RETRIES` | Guard fix-loop retries: adjust LLVM patch → rebuild LLVM → rebuild TA (default 3, each costs an LLVM rebuild). |
| `PUSH_TO_GITHUB=true` + `GITHUB_REPO=owner/name` | Enables `push_to_github`; requires `gh` logged in. |
| `AI_BACKEND=opencode\|claude` | Force AI backend; default auto-detects. |
| `LLVM_INSTALL_PREFIX` | Path to LLVM for building triton-ascend. |
| `CONDA_ENV` | Conda env name (default: `ta-upgrade`). |
| `NUM_PROCS` | Number of parallel pytest workers (default: 16). |
| `AUTO_STASH=true` | Auto-stash uncommitted changes before creating work branch. |

## Conventions

- Python 3.10–3.13. Uses `pip install -e .`; dependencies in `pyproject.toml`.
- No lint/typecheck/test commands are wired up. Verify changes by `SKIP_E2E_TEST=true SKIP_AI_ANALYSIS=true ta-kickoff ...` with a small synthetic commit range.
- All adapter outputs that need persistence go through `utils.py` filename constants; introducing a new artifact means adding a constant there first.

## Don'ts

- Don't keep `workspace/` paths between runs; they vanish on `initialize`.
- Don't add `{var}` placeholders to `prompt.md` that aren't passed into `run_opencode_adapter`'s inputs dict, or `format_map` will KeyError.
- Don't commit anything under `workspace/`, `output/`, `__pycache__/`, or `.env`.
- Don't use `git add -A` — use `git add -u` to avoid staging test artifacts.
- Don't modify version.txt or add version tracking files; `_do_finalize` no longer writes them.
