# TA Main2Main Workflow

Automate triton-ascend's upstream sync against Triton main branch.

Each time Triton's `main` advances, triton-ascend must catch up: merge
upstream changes, resolve conflicts, fix broken interfaces, rebuild LLVM (if
needed), build triton-ascend, and run tests. This project drives that whole
loop with AI-assisted fix/retry.

## Pipeline

```
prepare  →  detect  →  plan  →  per-step loop:
                                  merge → [resolve] → build ⇄ fix → test ⇄ fix → commit
                               →  finalize → [push PR]
```

| Phase | Description |
|-------|-------------|
| prepare | Clone triton-ascend, configure `origin` / `triton-upstream` remotes, fetch, checkout base branch |
| detect | Find merge-base between ascend HEAD and upstream target, list commits to merge |
| plan | Group commits into steps by line budget, handle LLVM-hash changes as solo steps |
| merge | `git merge --no-ff` the step's end commit into the current branch |
| resolve | AI resolves merge conflicts (skipped if no conflicts) |
| build | Rebuild LLVM (with AI fix if needed), then build triton-ascend. Retries on failure |
| test | Run pytest on Ascend NPU. Retries with AI fix on failure |
| commit | `git add -A && git commit -s` with structured message |
| finalize | Generate cumulative patch and summary report |
| push PR | Push branch and create GitHub PR (opt-in) |

## Requirements

- Python 3.10+
- `opencode` or `claude` CLI on `$PATH` (AI adapter)
- `git`, `cmake`, `ninja`, `clang`/`clang++` (for LLVM build)
- For tests: a host with Ascend NPUs
- For auto PR: `gh` CLI logged in

## Install

```bash
pip install -e .
```

Registers the `ta-kickoff` console script.

## Quick Start

```bash
# Auto-clone triton-ascend to workspace/, sync to a specific upstream commit
ta-kickoff --target-commit 99f44dd5a90c9ae30daa974704fcea0bcc4f5ba1

# Use existing local repo
ta-kickoff --triton-ascend-path /path/to/triton-ascend --target-commit abc123

# Dry-run: skip AI and tests (verify merge + build only)
SKIP_AI_ANALYSIS=true SKIP_BUILD=false SKIP_E2E_TEST=true ta-kickoff --target-commit abc123
```

## CLI Arguments

| Argument | Env Variable | Default | Description |
|----------|-------------|---------|-------------|
| `--triton-ascend-path` | `TRITON_ASCEND_PATH` | — | Local path to triton-ascend repo (auto-clone if not set) |
| `--triton-path` | `TRITON_PATH` | — | Local triton repo (offline mode, not used in remote mode) |
| `--target-commit` | `TRITON_TARGET_COMMIT` | triton-upstream/main HEAD | Upstream commit SHA to sync to |
| `--llvm-prefix` | `LLVM_INSTALL_PREFIX` | `workspace/llvm-install` | LLVM install prefix path |
| `--build-procs` | `BUILD_PROCS` | 32 | Parallel workers for ninja / cmake build |
| `--test-procs` | `TEST_PROCS` | 8 | Parallel pytest workers (`-n`) |

## Environment Variables

### Repository

| Variable | Default | Description |
|----------|---------|-------------|
| `TRITON_ASCEND_PATH` | — | Local triton-ascend path; if empty, auto-clone from URL |
| `TRITON_ASCEND_URL` | `https://github.com/triton-lang/triton-ascend.git` | Clone URL for triton-ascend |
| `TRITON_PATH` | — | Local triton repo (offline mode) |
| `TRITON_UPSTREAM_URL` | `https://github.com/triton-lang/triton.git` | Upstream Triton remote URL |
| `TRITON_TARGET_COMMIT` | — | Target upstream commit SHA |
| `TA_BASE_BRANCH` | `upstream_sync` | Base branch in triton-ascend to sync from |
| `LLVM_REPO_URL` | `https://github.com/llvm/llvm-project.git` | LLVM clone URL |
| `LLVM_INSTALL_PREFIX` | `workspace/llvm-install` | LLVM install prefix |

### AI Backend

| Variable | Default | Description |
|----------|---------|-------------|
| `AI_BACKEND` | `auto` | AI adapter: `opencode`, `claude`, or `auto` (detect) |
| `TA_AI_TIMEOUT_MINUTES` | 30 | AI call timeout in minutes |
| `TA_AI_STALE_SECONDS` | 1200 | AI stale timeout (seconds) |
| `TA_AI_MAX_STALE_RETRIES` | 3 | Max AI stale retries |

### Build / Test

| Variable | Default | Description |
|----------|---------|-------------|
| `BUILD_PROCS` | 32 | Parallel build workers (ninja `-j`, cmake, setup.py) |
| `TEST_PROCS` | 8 | Parallel pytest workers (`-n`) |

### Retry / Budget

| Variable | Default | Description |
|----------|---------|-------------|
| `TA_MAX_RETRIES` | 10 | Max retry attempts per step (build + test) |
| `TA_LINE_BUDGET` | 1000 | Max source lines per merge step |
| `TA_PROGRESSIVE_MERGE` | `true` | Enable progressive step merge |
| `TA_IR_MAX_ITERATIONS` | 3 | Max IR patch iterations |

### Skip Flags

| Variable | Default | Description |
|----------|---------|-------------|
| `TA_RESUME` | `false` | Skip steps whose output files already exist |
| `SKIP_AI_ANALYSIS` | `false` | Skip all AI calls (conflict resolution, fix) |
| `SKIP_BUILD` | `false` | Skip triton-ascend build |
| `SKIP_E2E_TEST` | `false` | Skip pytest, treat as passed |
| `SKIP_LLVM_REBUILD` | `false` | Skip LLVM rebuild |
| `SKIP_IR_PATCH` | `false` | Skip IR patch generation |
| `SKIP_BASELINE_LLVM` | `false` | Skip baseline LLVM build |

### Git / PR

| Variable | Default | Description |
|----------|---------|-------------|
| `PUSH_TO_GITHUB` | `false` | Push branch and create PR after success |
| `GITHUB_REPO` | `triton-lang/triton-ascend` | PR target `owner/name` |
| `TA_MAIN2MAIN_WORKSPACE` | `./workspace` | Override workspace directory |

## Workspace Layout

```
workspace/
├── triton-ascend/            # auto-cloned (if no local path given)
├── llvm-project/             # auto-cloned LLVM
├── llvm-build/               # LLVM build directory (outside llvm-project)
├── llvm-install/             # LLVM install prefix
├── detect.json               # merge-base, target commit, changed files
├── steps.json                # step plan
├── steps/
│   └── step-N/
│       ├── merge_result.json
│       ├── build_result.json
│       ├── build.log / build.err
│       ├── llvm-cmake.log / llvm-cmake.err
│       ├── llvm-ninja.log / llvm-ninja.err
│       ├── upstream.patch
│       ├── changed_files.txt
│       └── commits.txt
├── test-logs/
│   └── pytest-junit.xml
├── final_summary.md
└── final_target.patch
```

## Project Layout

```
src/TA_main2main_workflow/
├── flow.py                  # Pipeline orchestrator
├── main.py                  # `ta-kickoff` CLI entrypoint
├── pipeline/
│   ├── prepare.py           # Phase 0: workspace setup
│   ├── detect.py            # Phase 1: detect upstream commits
│   ├── plan.py              # Phase 2: plan merge steps
│   ├── merge.py             # Phase 3: git merge
│   ├── resolve.py           # Phase 3: AI conflict resolution
│   ├── build.py             # Phase 3: LLVM + triton-ascend build
│   ├── test.py              # Phase 3: pytest
│   ├── fix.py               # Phase 3: AI fix
│   ├── commit.py            # Phase 3: commit progress
│   ├── finalize.py          # Phase 4: summary + patch
│   ├── pre_ci.py            # pre-commit checks
│   └── push_pr.py           # push + create PR
├── utils/
│   ├── config.py            # TAConfig dataclass
│   ├── context.py           # WorkflowContext dataclass
│   ├── git.py               # run_git with built-in retry
│   ├── logging.py           # TALogger
│   ├── tracker.py           # timed() context manager
│   ├── errors.py            # Exception types
│   └── submodule.py         # AscendNPU-IR submodule helpers
├── agent/
│   └── opencode_adapter.py  # AI adapter (opencode / claude)
└── reference/               # AI knowledge base
```
