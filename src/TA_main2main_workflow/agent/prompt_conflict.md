Resolve merge conflicts in the triton-ascend upstream sync for step {step_id}.

---
## MISSION
---

You are a single agent. Do NOT use TeamCreate or Agent tools — work
directly without sub-agents.

Triton-Ascend is a fork of upstream Triton (triton-lang/triton) that adds
Ascend NPU support.

Your task is to resolve merge conflicts caused by merging upstream Triton
changes into triton-ascend. Files with <<<<<<< / ======= / >>>>>>> markers
need to be resolved.

Workflow:
  1. Read conflict files listed in {error_logs} to see unresolved merge conflicts
  2. For each conflicted file, understand BOTH sides:
     - The upstream triton change (incoming)
     - The triton-ascend additions/modifications (current)
  3. Consult the in-depth reference docs (see README.md index) for the
     conflict-resolution strategy BEFORE editing:
     - {reference_dir}/01-merge-upstream-conflict-resolution.md — conflict
       resolution strategy by file type, key case studies (Python frontend
       refactor, BC pipeline, DotScale attribute rename), standard merge flow
     - {reference_dir}/04-ir-compatibility-and-backend-adaptation.md — when a
       conflict involves IR/bytecode compatibility (BC pipeline, Op renames
       like indirect→unstructured, AscendNPU-IR submodule updates)
     - {reference_dir}/code-structure-guide.md — upstream → Ascend file mapping
  4. Resolve conflicts by:
     - Keeping triton-ascend's Ascend-specific additions
     - Accepting upstream triton changes that don't conflict with Ascend code
     - When both sides modified the same code, integrate both changes
     - Preserving Ascend-specific paths (python/triton_ascend/, third_party/ascend/)
  5. Check that resolved files are syntactically correct
  6. Write conflict resolution summary to {step_dir}/step_summary.md
  7. Stage resolved files with `git add <file>` for each resolved file

Key principles for conflict resolution:
  - Ascend-specific code (imports of triton_ascend, ascend device checks,
    CANN/torch-npu references) must be preserved
  - Upstream triton API changes should be accepted, but Ascend overrides
    must be updated to match new signatures
  - python/triton/ files are upstream code; changes here should follow
    upstream unless they contain Ascend-specific modifications
  - third_party/ascend/ files are entirely Ascend-specific; never overwrite
    these with upstream changes
  - lib/ and include/ changes should accept upstream C++ changes while
    preserving Ascend backend registration code

---
## REPOSITORIES
---

  triton-ascend: {ascend_path}
  upstream triton:{triton_path}
  reference:     {reference_dir}

---
## INPUTS
---

  mode:                  conflict
  step:                  {step_id}
  error logs:            {error_logs}
  conflict directory:    {conflict_dir}
  archive directory:     {step_dir}
  upstream target:       {target_commit}

---
## REFERENCE FILES
---

  Start from the index, then open the doc that matches your task:
  {reference_dir}/README.md                 — index of ALL adaptation docs

  {reference_dir}/01-merge-upstream-conflict-resolution.md
      — merge & conflict resolution: strategy by file type, key case studies
  {reference_dir}/04-ir-compatibility-and-backend-adaptation.md
      — BC pipeline, Op/IR structure changes, AscendNPU-IR submodule updates
  {reference_dir}/code-structure-guide.md
      — Triton vs Triton-Ascend file mapping

---
## RULES
---

  - Only modify files in {ascend_path} (triton-ascend repo)
  - The upstream triton repo at {triton_path} is read-only for reference
  - Do not run build commands, pip install, pytest, or CMake manually.
    Build and test execution is handled externally by the main2main flow.
  - Do not run git commit, git push, or git checkout. Only use `git add` to
    stage resolved files.
  - The working tree has unmerged files. Resolve them in place by editing
    the files to remove conflict markers.
  - Prefer minimal, targeted fixes over large refactors
  - Preserve all Ascend-specific functionality (triton-ascend is the primary
    codebase, not upstream triton)
  - **NEVER modify code under `third_party/nvidia/` or `third_party/amd/`.**
    These directories contain vendor-specific code that is NOT part of the
    Ascend backend.
  - **NEVER keep triton-ascend's version of `cmake/llvm-hash.txt` in a merge
    conflict.** This file must ALWAYS follow upstream triton. In any merge
    conflict on this file, accept the upstream (incoming) version
    unconditionally.
  - When unsure about an upstream change's impact, search the triton-ascend
    codebase for references to the changed symbol/file

---
## OUTPUT
---

  Archive all outputs to {step_dir}/:

    step_summary.md   — summary of resolutions applied, each resolved file
                        path and rationale for how the conflict was resolved

  After completing all work and writing archive files, stop.
