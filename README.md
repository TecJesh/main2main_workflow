# TA Main2Main Upgrade Flow

Automate triton-ascend's main2main upgrade against upstream Triton.

Each time Triton's `main` advances, triton-ascend must catch up: merge
upstream changes, resolve conflicts, fix broken interfaces, build, and run
e2e tests. This project drives that whole loop:

- detect the commit gap between triton-ascend and upstream Triton
- create a work branch from the latest triton-ascend main
- merge the target upstream commit into the work branch
- run AI (opencode or claude) to resolve conflicts and fix build/test failures
- run pytest on Ascend NPU, retry on failure (up to 3×)
- when everything passes, optionally push a branch and open a PR

Full walkthrough lives in [`docs/guide.md`](docs/guide.md); this README only
covers how to install and run.

## Requirements

- Python 3.10–3.13
- [`opencode`](https://opencode.ai) or `claude` CLI on `$PATH` (used as the AI adapter)
- `git`, plus local checkouts of `triton` and `triton-ascend`
- For real e2e tests: a host with Ascend NPUs
- For automated PRs: [`gh`](https://cli.github.com/) logged in
- LLVM toolchain (for building triton-ascend C++ extensions)

## Install

```bash
pip install -e .
```

This registers the `ta-kickoff` and `ta-plot` console scripts.

## Run

```bash
ta-kickoff \
  --triton-ascend-path /path/to/triton-ascend \
  --triton-path        /path/to/triton \
  [--target-commit     <40-char SHA>]
```

- Both paths must be local git checkouts.
- `--target-commit` is optional; defaults to upstream triton `HEAD`.
- Each run wipes and recreates `workspace/`, so back it up if you need the
  artifacts from a previous run.

CLI flags can also be supplied via env vars: `TRITON_ASCEND_PATH`, `TRITON_PATH`,
`TRITON_TARGET_COMMIT` (CLI wins).

### Common variations

```bash
# Dry-run plumbing: skip both AI and NPU tests
SKIP_AI_ANALYSIS=true SKIP_E2E_TEST=true ta-kickoff \
  --triton-ascend-path /path/to/triton-ascend --triton-path /path/to/triton

# Target a specific upstream commit
ta-kickoff \
  --triton-ascend-path /path/to/triton-ascend \
  --triton-path /path/to/triton \
  --target-commit abc123def456

# Auto-push a branch and open a PR after a successful run
PUSH_TO_GITHUB=true GITHUB_REPO=triton-lang/triton-ascend \
ta-kickoff --triton-ascend-path ... --triton-path ...
```

### Environment variables

| Variable | Purpose | Default |
|---|---|---|
| `TRITON_ASCEND_PATH` | triton-ascend repo path | cwd |
| `TRITON_PATH` | upstream triton repo path | cwd |
| `TRITON_TARGET_COMMIT` | target triton commit SHA | triton `HEAD` |
| `AI_BACKEND` | AI adapter: `opencode` or `claude` | auto-detect |
| `SKIP_AI_ANALYSIS` | skip AI, only run deterministic steps | `false` |
| `SKIP_BUILD` | skip the build step | `false` |
| `SKIP_E2E_TEST` | skip pytest, treat as passed | `false` |
| `PUSH_TO_GITHUB` | open a PR after success | `false` |
| `GITHUB_REPO` | PR target, `owner/name` | — |
| `LLVM_INSTALL_PREFIX` | LLVM install path for building | — |
| `CONDA_ENV` | Conda environment name | `ta-upgrade` |
| `NUM_PROCS` | parallel pytest workers | `16` |
| `AUTO_STASH` | auto-stash before sync | `false` |

## Outputs

Everything lands under `workspace/` (recreated on every run):

```
workspace/
├── detect.json              # merge-base, target commit, changed files
├── merge_result.json        # merge status, conflict info
├── merge.log                # raw git merge output
├── build_result.json        # build step results
├── build.log                # raw build output
├── test_result.json         # pytest summary
├── test-logs/
│   ├── pytest.log
│   └── precommit.log
├── conflicts/               # conflict snapshots (if any)
├── fixes/                   # per-fix-attempt logs
│   └── fix-<N>/
├── step-0/
│   ├── step_summary.md      # AI-written summary
│   ├── step_target.patch    # cumulative diff
│   └── analysis.md          # fix diagnosis
├── final_summary.md         # final sync summary
├── final_target.patch       # cumulative patch
└── FAILURE.md               # failure report (if failed)
```

## Project layout

```
src/TA_main2main_workflow/
├── flow.py              # CrewAI Flow: nodes, routing, retry loop
├── main.py              # `ta-kickoff` / `ta-plot` CLI entrypoints
├── utils.py             # filename constants + git helpers + console output
├── agent/
│   ├── opencode_adapter.py   # spawns `opencode run`, parses JSONL events
│   └── prompt.md             # single-agent task prompt
├── reference/           # knowledge base the agent reads at runtime
│   ├── adapt-guide.md
│   ├── code-structure-guide.md
│   ├── diagnosis-guide.md
│   ├── error-pattern-examples.md
│   └── npu-oom-handling.md
└── scripts/             # deterministic helpers (no AI)
    ├── build_test.py
    ├── detect_commits.py
    ├── merge_upstream.py
    ├── pre_ci_check.py
    ├── push_to_github.py
    └── update_commit_reference.py
```

For a step-by-step explanation of every node and the per-step artifacts, see
[`docs/guide.md`](docs/guide.md). For conventions and gotchas that affect code
changes to this repo itself, see [`AGENTS.md`](AGENTS.md).
