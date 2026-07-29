Fix test failures in the triton-ascend upstream sync for step {step_id}.

---
## MISSION
---

You are a single agent. Do NOT use TeamCreate or Agent tools — work
directly without sub-agents.

Triton-Ascend is a fork of upstream Triton (triton-lang/triton) that adds
Ascend NPU support.

Your task is to fix pytest/unit-test failures caused by merging upstream
Triton changes. The working tree is clean (merge and build already committed).

Workflow:
  1. Read test failure logs from {error_logs} — these are file paths to
     pytest-junit.xml and other test output
  2. Classify each failure:
     - Import error → module path or symbol may have moved upstream
     - Test failure → trace back to root cause in source code
     - API signature mismatch → function/class interface changed upstream
     - Assertion failure → expected behavior changed
     - Environment flake → note but do not fix (timeout, network, resource)
  3. For each actionable failure, consult reference docs (see README.md index).
     Always-useful quick guides:
     - {reference_dir}/diagnosis-guide.md — error → root cause mapping
     - {reference_dir}/error-pattern-examples.md — concrete fix patterns
     - {reference_dir}/code-structure-guide.md — upstream → Ascend file mapping
     Then read the in-depth guide:
     - {reference_dir}/03-unit-test-failure-diagnosis-and-fixes.md
       (7 typical failure cases, API signature mismatches, pass-option
       deprecations, post-upgrade test checklist)
  4. Apply minimal fixes:
     - Update imports when upstream moves modules
     - Update function signatures when upstream changes APIs
     - Fix pytest assertions when expected behavior changes
  5. Do NOT modify upstream triton code in python/triton/ unless it contains
     Ascend-specific changes (marked with triton_ascend imports or ascend checks)
  6. Write fix summary to {step_dir}/step_summary.md
  7. Write a ONE-LINE commit message to {step_dir}/commit_message.txt
     - Format: "<type>: <brief description>"
     - Types: fix, test, compat
     - Example: "test: fix pytest assertion for renamed attribute getLhs→getA"
     - Keep under 72 characters, be specific about WHAT was fixed
     - This line will be used as the git commit subject

Common failure patterns in Triton-Ascend:
  - python/triton/ changes → Ascend overrides in python/triton_ascend/ need updating
  - include/triton/ changes → Ascend headers may reference changed interfaces
  - Upstream API deprecations → Ascend code using old APIs needs updating
  - Pytest assertion changes due to upstream behavior changes

---
## REPOSITORIES
---

  triton-ascend: {ascend_path}
  upstream triton:{triton_path}
  reference:     {reference_dir}

---
## INPUTS
---

  mode:                  test_fix
  step:                  {step_id}
  error logs:            {error_logs}
  archive directory:     {step_dir}
  upstream target:       {target_commit}

---
## REFERENCE FILES
---

  Start from the index, then open the doc that matches your task:
  {reference_dir}/README.md                 — index of ALL adaptation docs

  Quick guides:
  {reference_dir}/diagnosis-guide.md        — error type → root cause mapping
  {reference_dir}/error-pattern-examples.md — concrete fix patterns per error type
  {reference_dir}/code-structure-guide.md   — Triton vs Triton-Ascend file mapping

  In-depth guide:
  {reference_dir}/03-unit-test-failure-diagnosis-and-fixes.md
      — 7 typical unit-test failure cases, post-upgrade test checklist

---
## RULES
---

  - Only modify files in {ascend_path} (triton-ascend repo)
  - The upstream triton repo at {triton_path} is read-only for reference
  - Do not run build commands, pip install, pytest, or CMake manually.
    Build and test execution is handled externally by the main2main flow.
  - Do not run git commit, git push, or git checkout.
  - The working tree is clean. Apply fixes as new edits.
  - Prefer minimal, targeted fixes over large refactors
  - Preserve all Ascend-specific functionality (triton-ascend is the primary
    codebase, not upstream triton)
  - **NEVER modify code under `third_party/nvidia/` or `third_party/amd/`.**
    These directories contain vendor-specific code that is NOT part of the
    Ascend backend. Test failures in these paths must be treated as
    environment issues, not code bugs — do not touch them.
    Your fixes must be confined to Ascend-specific code paths:
      - `third_party/ascend/`     (Ascend backend implementation)
      - `python/triton_ascend/`   (Ascend Python bindings)
      - `lib/Target/Ascend/`      (Ascend LLVM backend)
      - `python/triton/`          (only if it contains Ascend conditionals)
      - Other project files (CMakeLists.txt, setup.py, etc.)
  - When unsure about an upstream change's impact, search the triton-ascend
    codebase for references to the changed symbol/file

---
## OUTPUT
---

  Archive all outputs to {step_dir}/:

    step_summary.md     — each failure and its root cause, fix applied and
                          rationale, any failures intentionally not fixed
    commit_message.txt  — ONE-LINE git commit subject (under 72 chars)

  After completing all work and writing archive files, stop.
