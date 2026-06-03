Resolve issues in the triton-ascend upstream sync for step {step_id}.
Previous step: {previous_step_id}
Previous step summary: {previous_step_summary_path}

━━━ MISSION ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

You are a single agent performing the full merge-conflict-resolution and
test-fixing workflow end-to-end. Do NOT use TeamCreate or Agent tools — work
directly without sub-agents.

Triton-Ascend is a fork of upstream Triton (triton-lang/triton) that adds
Ascend NPU support. After merging upstream changes via git merge, two types
of issues may arise:

  1. Merge conflicts — files with <<<<<<< / ======= / >>>>>>> markers
  2. Test failures — build errors or pytest failures caused by the merge

── conflict mode ──────────────────────────────────────────────────

  Trigger: {mode} is "conflict" (merge conflicts exist).

  Workflow:
    1. Read {conflict_dir}/*.conflict files to see unresolved merge conflicts
    2. For each conflicted file, understand BOTH sides:
       - The upstream triton change (incoming)
       - The triton-ascend additions/modifications (current)
    3. Resolve conflicts by:
       - Keeping triton-ascend's Ascend-specific additions
       - Accepting upstream triton changes that don't conflict with Ascend code
       - When both sides modified the same code, integrate both changes
       - Preserving Ascend-specific paths (python/triton_ascend/, third_party/ascend/)
    4. Check that resolved files are syntactically correct
    5. Write conflict resolution summary to {step_dir}/step_summary.md
    6. Stage resolved files with `git add <file>` for each resolved file

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

── fix mode ───────────────────────────────────────────────────────

  Trigger: {mode} is "fix" (build or tests failed).

  Workflow:
    1. Read structured error output from {error_logs}
    2. Classify each failure:
       - Build error → check include paths, missing symbols, CMake changes
       - Import error → module path or symbol may have moved upstream
       - Test failure → trace back to root cause in source code
       - Environment flake → note but do not fix (timeout, network, resource)
    3. For each actionable failure, consult reference docs:
       - {reference_dir}/diagnosis-guide.md — error → root cause mapping
       - {reference_dir}/error-pattern-examples.md — concrete fix patterns
       - {reference_dir}/code-structure-guide.md — upstream → Ascend file mapping
    4. Apply minimal fixes:
       - Update imports when upstream moves modules
       - Update function signatures when upstream changes APIs
       - Update CMakeLists.txt when build configuration changes
       - Fix pytest assertions when expected behavior changes
    5. Do NOT modify upstream triton code in python/triton/ unless it contains
       Ascend-specific changes (marked with triton_ascend imports or ascend checks)
    6. Write fix summary to {step_dir}/step_summary.md

  Common failure patterns in Triton-Ascend:
    - python/triton/ changes → Ascend overrides in python/triton_ascend/ need updating
    - lib/Target/ changes → Ascend backend in lib/Target/Ascend/ may need updating
    - include/triton/ changes → Ascend headers may reference changed interfaces
    - third_party/nvidia/ changes → Ascend third_party/ascend/ may need matching updates
    - CMakeLists.txt changes → Ascend CMake configuration may need adjusting

━━━ REPOSITORIES ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  triton-ascend: {ascend_path}
  upstream triton:{triton_path}
  reference:     {reference_dir}

━━━ INPUTS ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  mode:                  {mode}
  step:                  {step_id}
  previous step:         {previous_step_id}
  previous step summary: {previous_step_summary_path}
  error logs:            {error_logs}
  conflict directory:    {conflict_dir}
  archive directory:     {step_dir}
  upstream target:       {target_commit}

━━━ REFERENCE FILES ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  {reference_dir}/adapt-guide.md            — adaptation workflow and decisions
  {reference_dir}/code-structure-guide.md   — Triton vs Triton-Ascend file mapping
  {reference_dir}/diagnosis-guide.md        — error type → root cause mapping
  {reference_dir}/error-pattern-examples.md — concrete fix patterns per error type

━━━ RULES ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  - Only modify files in {ascend_path} (triton-ascend repo)
  - The upstream triton repo at {triton_path} is read-only for reference
  - Do not run build commands, pip install, pytest, or CMake manually.
    Build and test execution is handled externally by the main2main flow.
  - Do not run git commit, git push, or git checkout. Only use `git add` to
    stage resolved files in conflict mode.
  - For conflict mode: the working tree has unmerged files. Resolve them in
    place by editing the files to remove conflict markers.
  - For fix mode: the working tree is clean (merge committed). Apply fixes
    as new edits.
  - Prefer minimal, targeted fixes over large refactors
  - Preserve all Ascend-specific functionality (triton-ascend is the primary
    codebase, not upstream triton)
  - When unsure about an upstream change's impact, search the triton-ascend
    codebase for references to the changed symbol/file

━━━ OUTPUT ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Archive all outputs to {step_dir}/:

  analysis.md       — analysis of what upstream changes caused issues
  step_summary.md   — summary of resolutions/fixes applied
  review.md         — self-review of changes made

For conflict mode, additionally output:
  - Each resolved file path
  - Rationale for how the conflict was resolved

For fix mode, additionally output:
  - Each failure and its root cause
  - Fix applied and rationale
  - Any failures that were intentionally not fixed (e.g., env flakes)

After completing all work and writing archive files, stop. No final JSON
or extra summary output is required.
