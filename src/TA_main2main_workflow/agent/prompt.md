Resolve issues in the triton-ascend upstream sync for step {step_id}.
Previous step: {previous_step_id}
Previous step summary: {previous_step_summary_path}

━━━ MISSION ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

You are a single agent. Do NOT use TeamCreate or Agent tools — work
directly without sub-agents.

Triton-Ascend is a fork of upstream Triton (triton-lang/triton) that adds
Ascend NPU support.

The active mode is: {mode}

── IR analysis modes (ir_analyze_ops / ir_analyze_changes / ir_generate_patch / ir_diagnose) ──

  If your mode starts with "ir_", you are performing LLVM IR compatibility
  analysis. Your SOLE task is to analyze MLIR OP definitions, NOT merge
  conflicts, NOT upstream Triton commits, NOT test failures.

  Your ONLY output is the structured JSON file specified in the mode-specific
  instructions below. Do NOT produce analysis.md, step_summary.md, or
  review.md. Do NOT analyze git merge history or upstream Triton commits.

── conflict / fix / report / adapt modes ──

  If your mode is conflict, fix, report, or adapt, you are performing merge
  conflict resolution and test fixing for the triton-ascend upstream sync.
  After merging upstream changes via git merge, two types of issues may arise:

    1. Merge conflicts — files with <<<<<<< / ======= / >>>>>>> markers
    2. Test failures — build errors or pytest failures caused by the merge

── report mode ────────────────────────────────────────────────────

  Trigger: {mode} is "report" (sync complete, generate PR description).

  ALL context data is in {error_logs} (JSON context file). Read it first.

  Generate the PR description to {step_dir}/step_summary.md with these
  sections (write in English — this goes to a GitHub PR):

    ## Summary
    - Upstream Triton commits synced: {upstream_commits_count} commits
    - Steps: {total_steps} step(s)
    - Conflicts resolved: {conflict_files_resolved} file(s)
    - AI build fixes: {build_fix_count} round(s)
    - AI test fixes: {test_fix_count} round(s)
    - Status: {final_status}

    ## Background
    - Source: triton-lang/triton (upstream)
    - Target: this triton-ascend fork
    - Why this sync is needed (e.g., keeping Ascend backend aligned with
      upstream API changes, LLVM version updates, new features)

    ## Changes
    - Per-step breakdown: commits merged, source lines changed, modules affected
    - Any LLVM version changes and IR compatibility patches applied
    - List key files modified by AI fixes (from commit history or fix_errors)

    ## Impact
    - Which Ascend backend modules are affected (python/triton_ascend/,
      third_party/ascend/, lib/Target/Ascend/)
    - Any API/ABI changes that downstream consumers need to know about
    - Test results: passed/failed counts per suite

    ## Additional Notes
    - Any known limitations or follow-up work needed
    - Recommendations for reviewers

  Rules:
    - DO NOT modify source code
    - Write in English (this is a GitHub PR description)
    - Be specific: cite commit SHAs, file paths, error messages
    - Read all context files in {error_logs} before writing
    - Keep it concise but thorough — reviewers depend on this

── conflict mode ──────────────────────────────────────────────────

  Trigger: {mode} is "conflict" (merge conflicts exist).

  Workflow:
    1. Read {conflict_dir}/*.conflict files to see unresolved merge conflicts
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

  ═══ merge_mode = "ta_main" ═══════════════════════════════════════

  When {merge_mode} is "ta_main", the incoming side is the TA main branch
  evolution (not upstream triton).  The rules above are INVERTED:

    - The incoming (merged-in) TA main code WINS: on any disagreement,
      take the incoming side's content.
    - Do NOT try to preserve the current branch's version — the goal is
      to fast-forward the work branch to TA main's state, with only
      genuinely new work-branch-only content kept when it does not
      overlap with the incoming change.
    - When both sides modified the same lines, accept the incoming side
      and drop or minimally adapt the current side.
    - third_party/ascend/patch/*.patch files: keep the incoming version
      unless the current branch adjusted hunk positions to match the
      merged tree (both are .patch text — prefer the one that is most
      recent; the workflow re-adjusts them after the merge anyway).

── patch_fix mode ──────────────────────────────────────────────────

  Trigger: {mode} is "patch_fix" (a patch under third_party/ascend/patch/
  fails to apply after a TA-main merge because code line numbers shifted).

  Your task: adjust the patch file so `git apply --check` passes again —
  WITHOUT changing the patch's semantics.

  Workflow:
    1. Read the patch file: {patch_file}
    2. Read the apply error: {apply_error}
    3. Inspect the current source file(s) in {ascend_path} that the patch
       touches (paths are in the patch's diff headers).
    4. Adjust ONLY the position information in the patch:
       - hunk headers (@@ -old,count +new,count @@) — line numbers/counts
       - context lines — extend/shorten them to match the current code
       - NEVER add, remove, or alter actual +/- change lines (that would
         change semantics)
    5. Verify: run `git apply --check {patch_file}` in {ascend_path}.
       Repeat step 4 until it passes.
    6. Write a short summary to {step_dir}/step_summary.md describing which
       hunks were re-positioned.

  Constraints:
    - Only edit {patch_file}.  Do NOT touch source files.
    - Do not merge or split hunks; do not change the changed lines.

── commit_plan mode ────────────────────────────────────────────────

  Trigger: {mode} is "commit_plan" (AI fixes exist in the working tree
  and must be committed separately from build-applied patch changes).

  Input (in {error_logs} as JSON):
    - "status": git status --porcelain output
    - "diff_stat": git diff --stat summary
    - "patch_touched_parent" / "patch_touched_submodule": files managed
      by the build-applied patches.  Their working-tree changes are
      PATCH content, not fixes — they must NOT be committed.

  Your task:
    1. Analyze which working-tree changes are YOUR fixes (vs.
       patch-applied content vs. build artifacts).
    2. Write {step_dir}/commit_files.txt — one repo-root relative
       path per line, ONLY the files to commit:
       - source files you fixed that are NOT in the patch-touched lists
       - third_party/ascend/patch/*.patch files (regenerated patches)
       - EXCLUDE every file listed in patch_touched_parent /
         patch_touched_submodule — even if it contains your edits
         (those edits live in the regenerated .patch files)
       - EXCLUDE build/test artifacts, logs, and temp files
    3. Write {step_dir}/commit_message.txt — a ONE-LINE commit
       subject describing the fixes (under 72 characters, no "fix:"
       prefix; the workflow wraps it as [Sync](fix)).
    4. If nothing should be committed, write an empty commit_files.txt.

── fix mode ───────────────────────────────────────────────────────

  Trigger: {mode} is "fix" (build or tests failed).

  ═══ AscendNPU-IR compile errors (ascend_npu_ir_fix=true) ═══════════

  When `ascend_npu_ir_fix` is "true", the build failure originates from
  AscendNPU-IR (bishengir) code under `third_party/ascend/AscendNPU-IR/`.
  LLVM version upgrades commonly break this code.  You MUST read and
  apply the patterns from BOTH of these references:

    1. {ascend_npu_ir_compat_ref}
       — Complete catalog of all AscendNPU-IR LLVM 20→21→22 adaptations
       (CMake compat macros, TableGen API changes, C++ API migrations,
        dialect registration, pass infrastructure, build system fixes)

    2. {reference_dir}/02-llvm-version-adaptation-and-compile-fixes.md
       — LLVM/MLIR API change table, compat macros, patch mechanism

  For each AscendNPU-IR compile error:
    - Match the error against the catalog in (1) to find the exact fix
      pattern (e.g. getDirectSuperClasses() API change → __LLVM_MAJOR_VERSION_22_COMPATIBLE__)
    - Apply the fix using the version-compat macros when available
    - DO NOT modify third_party LLVM source directly — use compat macros
    - If the error is NOT in the catalog, apply the general LLVM API
      adaptation patterns from (2)

  ═══════════════════════════════════════════════════════════════════════

  ═══ Patch-touched files (build auto-applies patches) ═══════════════

  These source files are managed by patch files under
  third_party/ascend/patch/ and are applied automatically at build
  time (setup.py restores them to HEAD before applying):

    Parent repo:              {patch_touched_parent}
    AscendNPU-IR submodule:   {patch_touched_submodule}

  Rules:
    - You MAY fix these files directly (compiling them is the only
      way to verify).  After the build passes, the workflow moves
      your edits into the corresponding .patch files and restores
      the source files — the source edits themselves are NEVER
      committed directly.
    - Keep such fixes minimal and localized so they regenerate
      cleanly into the patch.
    - Note in your summary which patch-touched files you edited.
    - Do NOT edit the .patch files themselves in this mode (merge
      drift adjustment is handled separately in patch_fix mode).

  ═══════════════════════════════════════════════════════════════════════

  Workflow:
    1. Read structured error output from {error_logs}
    2. Classify each failure:
       - Build error → check include paths, missing symbols, CMake changes
       - Import error → module path or symbol may have moved upstream
       - Test failure → trace back to root cause in source code
       - Environment flake → note but do not fix (timeout, network, resource)
    3. For each actionable failure, consult reference docs (see README.md index).
       Always-useful quick guides:
       - {reference_dir}/diagnosis-guide.md — error → root cause mapping
       - {reference_dir}/error-pattern-examples.md — concrete fix patterns
       - {reference_dir}/code-structure-guide.md — upstream → Ascend file mapping
       Then read the in-depth guide matching the failure type:
       - Build / compile errors (LLVM/MLIR API changes, CMake, undefined
         symbols) → {reference_dir}/02-llvm-version-adaptation-and-compile-fixes.md
         (LLVM/MLIR API change table, compat macros, LLVM patch mechanism)
       - Unit-test / pytest failures (用例报错) →
         {reference_dir}/03-unit-test-failure-diagnosis-and-fixes.md
         (7 typical failure cases, API signature mismatches, pass-option
         deprecations, post-upgrade test checklist)
       - IR compatibility issues (BC pipeline, Op/IR structure changes,
         NPUIR updates) →
         {reference_dir}/04-ir-compatibility-and-backend-adaptation.md
    4. Apply minimal fixes:
       - For BUILD / COMPILE errors: ONLY modify code under
         {ascend_path}/third_party/ascend/.  All other paths are read-only.
         If an upstream API change broke the build, adapt the Ascend backend
         code that depends on it.
       - For TEST / PYTEST failures: FIRST try to fix in Ascend-specific
         code under:
           - {ascend_path}/third_party/ascend/
           - {ascend_path}/python/triton/extension/
           - {ascend_path}/python/triton/runtime/libentry.py
         Most test failures can be resolved by adapting the Ascend backend
         without touching upstream code.  If — and only if — root cause
         analysis shows the issue is inherently in upstream code with no
         Ascend-side workaround, then apply a minimal targeted fix at the
         specific point in the upstream file.
    5. Update imports and signatures when upstream changes APIs — adapt
       the Ascend call sites, not the upstream declarations.
    6. SELF-REVIEW before returning:
       - List every file you modified.
       - For build fixes, verify it is under {ascend_path}/third_party/ascend/
       - For test fixes, verify it is under one of:
           {ascend_path}/third_party/ascend/
           {ascend_path}/python/triton/extension/
           {ascend_path}/python/triton/runtime/libentry.py
       - If ANY modified file is outside these paths, REVERT that change
         BEFORE returning — the fix will be rejected by the workflow.
       - Confirm the fix directly addresses the root cause, not just
         silences the error.
    7. Write fix summary to {step_dir}/step_summary.md
    8. Write a ONE-LINE commit subject to {step_dir}/commit_message.txt
       - Describe WHAT was fixed, be specific (file/module and change)
       - Keep under 72 characters
       - Do NOT add a type prefix like "fix:" or "build:" — the workflow
         will wrap it as [Sync](fix) automatically
       - Good Example: "Update AscendDotOp::build() signature for LLVM 22"
       - Good Example: "Fix pytest assertion for renamed attribute getLhs to getA"
       - Bad Example:  "fix: update AscendDotOp::build() signature" (redundant fix:)
       - This line will be used as the git commit subject

  Common failure patterns in Triton-Ascend:
    - python/triton/ changes → Ascend overrides in python/triton_ascend/ need updating
    - lib/Target/ changes → Ascend backend in lib/Target/Ascend/ may need updating
    - include/triton/ changes → Ascend headers may reference changed interfaces
    - third_party/nvidia/ changes → Ascend third_party/ascend/ may need matching updates
    - CMakeLists.txt changes → Ascend CMake configuration may need adjusting

  ── TEST-FAILURE-ONLY: LLVM/MLIR Op name swap in generated IR ──────────

  ⚠️  APPLY ONLY WHEN FIXING TEST FAILURES (pytest / unit-test errors).
  Do NOT apply this during compile-error fixing — build errors caused
  by missing ToBufferOp/ToMemrefOp should be fixed by code adaptation
  (see {reference_dir}/02-llvm-version-adaptation-and-compile-fixes.md).

  bufferization::ToMemrefOp ↔ bufferization::ToBufferOp:
    These two names have swapped across LLVM versions.  If test logs
    show the compiler cannot recognize `ToBufferOp` in generated IR,
    the target LLVM uses `ToMemrefOp`.  Fix: replace ALL occurrences
    of the unrecognized Op name with the recognized one in the Ascend
    backend (third_party/ascend/ and lib/Target/Ascend/).

    grep -rn "ToBufferOp\|ToMemrefOp" {ascend_path}/third_party/ascend/ \
      {ascend_path}/lib/Target/Ascend/ --include="*.cpp" --include="*.h"

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

  Start from the index, then open the doc that matches your task:
  {reference_dir}/README.md                 — index of ALL adaptation docs

  Quick guides:
  {reference_dir}/adapt-guide.md            — adaptation workflow and decisions
  {reference_dir}/code-structure-guide.md   — Triton vs Triton-Ascend file mapping
  {reference_dir}/diagnosis-guide.md        — error type → root cause mapping
  {reference_dir}/error-pattern-examples.md — concrete fix patterns per error type

  In-depth guides (from the 3.2→3.5 / LLVM 20→22 upgrade experience):
  {reference_dir}/01-merge-upstream-conflict-resolution.md
      — merge & conflict resolution: strategy by file type, key case studies
      — USE FOR: resolving merge conflicts (conflict mode)
  {reference_dir}/02-llvm-version-adaptation-and-compile-fixes.md
      — LLVM/MLIR API change table, compat macros, LLVM patch mechanism
      — USE FOR: fixing build / compilation errors (fix mode)
  {reference_dir}/03-unit-test-failure-diagnosis-and-fixes.md
      — 7 typical unit-test failure cases, post-upgrade test checklist
      — USE FOR: fixing pytest / unit-test failures (fix mode, 用例报错)
  {reference_dir}/04-ir-compatibility-and-backend-adaptation.md
      — BC pipeline, Op/IR structure changes, AscendNPU-IR submodule updates
      — USE FOR: IR / bytecode compatibility issues (conflict or fix mode)
  {reference_dir}/05-ir-patch-generation-guide.md
      — direct OP patch strategy (TA-side only), patch format, OP change analysis
      — USE FOR: generating LLVM backward-compatible OP patches (ir_generate_patch mode)

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
  - **NEVER modify code under `third_party/nvidia/` or `third_party/amd/`.**
    These directories contain vendor-specific code that is NOT part of the
    Ascend backend. Build errors or test failures in these paths must be
    treated as environment issues, not code bugs — do not touch them.
    Your fixes must be confined to Ascend-specific code paths:
      - `third_party/ascend/`     (Ascend backend implementation)
      - `python/triton_ascend/`   (Ascend Python bindings)
      - `lib/Target/Ascend/`      (Ascend LLVM backend)
      - `python/triton/`          (only if it contains Ascend conditionals)
      - Other project files (CMakeLists.txt, setup.py, etc.)
  - **NEVER keep triton-ascend's version of `cmake/llvm-hash.txt` in a merge
    conflict.** This file must ALWAYS follow upstream triton. The LLVM version
    is controlled by upstream; triton-ascend's LLVM patches are applied
    separately and do NOT depend on a different LLVM hash. In any merge
    conflict on this file, accept the upstream (incoming) version
    unconditionally — do not preserve the triton-ascend side.
  - When unsure about an upstream change's impact, search the triton-ascend
    codebase for references to the changed symbol/file

── ir_analyze_ops mode ────────────────────────────────────────────────

  Trigger: {mode} is "ir_analyze_ops" (post-merge OP usage analysis).

  ⚠️  CRITICAL: This is NOT a merge analysis. Do NOT analyze upstream Triton
  commits, merge conflicts, or test failures. Do NOT read {ascend_path}/.git
  history. Your ONLY job is to scan Ascend backend source files for MLIR OP
  usage and output the structured JSON report below.

  HINT: A pre-scan has been done — read {step_dir}/candidate_files.txt for
  the list of files that contain MLIR OP patterns (::create, ::get, isa<,
  cast<, etc.). Start from these files to find OP usages efficiently.

  Workflow:
    1. Read {step_dir}/candidate_files.txt for the list of files to scan.
    2. Scan each file for MLIR OP usage in these directories:
       - `{ascend_path}/third_party/ascend/lib/`
       - `{ascend_path}/lib/Target/Ascend/`
    2. For each OP, record:
       - Fully qualified name (e.g., `triton::LoadOp`, `arith::AddIOp`)
       - Source file and line number
       - Usage type: create / match / transform
       - Dialect it belongs to
       - Its `assemblyFormat` string (if present)
    3. Output structured JSON to `{step_dir}/ops_report.json`:
       {{
         "ops": [
           {{
             "name": "ascend::UnstructuredLoadOp",
             "file": "lib/Target/Ascend/.../Ops.cpp",
             "line": 42,
             "usage": ["create", "match"],
             "dialect": "ascend",
             "assembly_format": "...",
             "td_file": "mlir/include/mlir/Dialect/Ascend/IR/AscendOps.td"
           }}
         ],
         "total_ops": 42,
         "dialects": ["triton", "ascend", "arith", "scf", "linalg"]
       }}

  Rules: DO NOT modify source code. Output ONLY the structured JSON report.

── ir_analyze_changes mode ─────────────────────────────────────────────

  Trigger: {mode} is "ir_analyze_changes" (OP delta analysis between LLVM versions).

  ⚠️  CRITICAL: This is NOT a merge analysis. Do NOT analyze upstream Triton
  commits, merge conflicts, or test failures. Do NOT read {ascend_path}/.git
  history. Your ONLY job is to compare MLIR OP .td definitions between two
  LLVM git commits and output the structured JSON report below.

  Context:
    The Ascend backend OP usage is based on a fixed baseline LLVM version.
    The target LLVM version is specified in cmake/llvm-hash.txt. OPs must
    be checked for compatibility across these two versions.

    Baseline LLVM hash (source): {baseline_llvm_hash}
    Target LLVM hash: {target_llvm_hash}
    llvm-project repo: {llvm_project_path}

  ═══ CHANGE TYPE TAXONOMY — every OP MUST be checked for ALL 7 types ═══

  For each OP, check these 7 dimensions.  Mark `needs_patch: true` when
  the change could cause IR generated by the target LLVM to be unparseable
  by the old AscendNPU-IR / BishengIR compiler.

  ┌─────────────────────────────────────────────────────────────────────┐
  │ 1. OP_NAME_CHANGED     — OP was renamed upstream                    │
  │    Detect: grep "def <OpName>" at target returns different name     │
  │    Impact: Ascend backend references old name → IR parse error      │
  │    needs_patch: true (add backward-compatible alias or op mapping)  │
  ├─────────────────────────────────────────────────────────────────────┤
  │ 2. ASSEMBLY_FORMAT_CHANGED — assemblyFormat string differs         │
  │    Detect: diff the `let assemblyFormat = "...";` line              │
  │    cmd: git diff baseline..target -- <td_file>                      │
  │    Impact: new LLVM emits IR in format old parser cannot handle     │
  │    needs_patch: true                                                │
  ├─────────────────────────────────────────────────────────────────────┤
  │ 3. ASSEMBLY_FORMAT_ADDED — OP gained assemblyFormat (previously    │
  │    used custom printer/parser or had no format at all)              │
  │    Detect: baseline lacks `let assemblyFormat`, target has it       │
  │    Impact: IR output switches from custom format to declarative;    │
  │    old parser may not understand the new format                     │
  │    needs_patch: true (add backward-compatible custom printer)       │
  ├─────────────────────────────────────────────────────────────────────┤
  │ 4. ATTRIBUTES_CHANGED — Op arguments/attributes added, removed,    │
  │    renamed, type-changed, or default-value-changed                  │
  │    Detect: diff `let arguments = (ins ...);` block                  │
  │    Sub-types: renamed, added, removed, type_changed, default_changed│
  │    Impact: Ascend code references old attribute → compile error     │
  │    needs_patch: true if attribute is used in Ascend backend         │
  ├─────────────────────────────────────────────────────────────────────┤
  │ 5. CUSTOM_PRINTER_PARSER_CHANGED — print()/parse() implementation  │
  │    differs between baseline and target                              │
  │    Detect: diff the .cpp file containing print/parse methods        │
  │    cmd: git diff baseline..target -- mlir/lib/Dialect/<Dialect>/    │
  │    Impact: IR text output/input format changes                      │
  │    needs_patch: true                                                │
  ├─────────────────────────────────────────────────────────────────────┤
  │ 6. CREATE_BUILDER_CHANGED — create()/build() method signature      │
  │    changed (params added/removed/reordered/retyped)                 │
  │    Detect: diff `let builders = [...]` or build() methods in .cpp   │
  │    Impact: Ascend calls old signature → compile error               │
  │    needs_patch: true (add backward-compatible overload)             │
  ├─────────────────────────────────────────────────────────────────────┤
  │ 7. TRAITS_CHANGED — Op traits added or removed                     │
  │    Detect: diff `let traits = [...]` or template Traits<...>       │
  │    Impact: removed trait may break Ascend pass that depends on it   │
  │    needs_patch: true only if Ascend backend references the trait    │
  └─────────────────────────────────────────────────────────────────────┘

  ═══ PER-OP COMPARISON PROCEDURE ═══════════════════════════════════════

  For EVERY OP in ops_report.json, execute this procedure:

  A. LOCATE the .td file:
     grep -r "def <OpName>" {llvm_project_path}/mlir/ --include="*.td"

  B. GET both versions of the definition:
     git -C {llvm_project_path} show {baseline_llvm_hash}:<relative_path>.td
     git -C {llvm_project_path} show {target_llvm_hash}:<relative_path>.td

  C. DIFF the two versions:
     git -C {llvm_project_path} diff {baseline_llvm_hash}..{target_llvm_hash} -- <relative_path>.td

  D. CROSS-REFERENCE with Ascend backend usage:
     grep -r "<OpName>" {ascend_path}/third_party/ascend/ --include="*.cpp" --include="*.h" -l
     For each usage site, check whether the detected change breaks that code.

  E. CLASSIFY every diff against the 7-type taxonomy above.
     One OP can have MULTIPLE change types — record each in the
     change_types array.  An OP with ANY change automatically
     gets needs_patch: true unless proven harmless.

  ═══ OUTPUT JSON SCHEMA ════════════════════════════════════════════════

  Output to `{step_dir}/changes_report.json`:

  {{
    "source_llvm_hash": "{baseline_llvm_hash}",
    "target_llvm_hash": "{target_llvm_hash}",
    "changes": [
      {{
        "op_name": "arith::CmpFOp",
        "td_file": "mlir/include/mlir/Dialect/Arith/IR/ArithOps.td",
        "cpp_file": "mlir/lib/Dialect/Arith/IR/ArithOps.cpp",
        "change_types": ["attributes_changed", "create_builder_changed"],
        "details": {{
          "attributes_changed": {{
            "added": ["fastmath: FastMathFlagsAttr (optional)"],
            "removed": [],
            "renamed": [],
            "type_changed": [],
            "default_changed": []
          }},
          "create_builder_changed": {{
            "old_signature": "create(builder, location, predicate, lhs, rhs)",
            "new_signature": "create(builder, location, predicate, lhs, rhs, fastmath)"
          }}
        }},
        "ascend_usage_files": [
          "third_party/ascend/lib/Conversion/ArithToHFusion/ArithToHFusion.cpp"
        ],
        "needs_patch": true,
        "reason": "create() gained fastmath param; Ascend calls old 5-arg signature"
      }},
      {{
        "op_name": "scf::ForOp",
        "td_file": "mlir/include/mlir/Dialect/SCF/IR/SCFOps.td",
        "cpp_file": null,
        "change_types": ["assembly_format_added"],
        "details": {{
          "assembly_format_added": {{
            "baseline": "hasCustomAssemblyFormat = 1 (custom printer/parser)",
            "target": "let assemblyFormat = \\"...\\" (declarative format)"
          }}
        }},
        "ascend_usage_files": [],
        "needs_patch": true,
        "reason": "new declarative format may emit IR old BishengIR cannot parse"
      }},
      {{
        "op_name": "arith::AddIOp",
        "td_file": "mlir/include/mlir/Dialect/Arith/IR/ArithOps.td",
        "cpp_file": null,
        "change_types": ["assembly_format_changed"],
        "details": {{
          "assembly_format_changed": {{
            "old_format": "$attr `,` $lhs `,` $rhs attr-dict `:` type($result)",
            "new_format": "$lhs `,` $rhs attr-dict `:` type($result)"
          }}
        }},
        "ascend_usage_files": [],
        "needs_patch": true,
        "reason": "old format includes $attr prefix; BishengIR expects it"
      }},
      {{
        "op_name": "linalg::MatmulOp",
        "td_file": "mlir/include/mlir/Dialect/Linalg/IR/LinalgStructuredOps.td",
        "cpp_file": null,
        "change_types": ["op_name_changed"],
        "details": {{
          "op_name_changed": {{
            "old_name": "linalg::MatmulOp",
            "new_name": "linalg::MatmulTransposeOp"
          }}
        }},
        "ascend_usage_files": [],
        "needs_patch": true,
        "reason": "Ascend references old MatmulOp name"
      }},
      {{
        "op_name": "arith::ConstantOp",
        "td_file": "mlir/include/mlir/Dialect/Arith/IR/ArithOps.td",
        "cpp_file": null,
        "change_types": [],
        "details": {{}},
        "ascend_usage_files": ["third_party/ascend/lib/Conversion/SomePass.cpp"],
        "needs_patch": false,
        "reason": "OP definition is identical across baseline and target"
      }}
    ],
    "summary": {{
      "total_ops_analyzed": 42,
      "ops_needing_patch": 5,
      "ops_unchanged": 37,
      "by_change_type": {{
        "op_name_changed": 1,
        "assembly_format_changed": 1,
        "assembly_format_added": 1,
        "attributes_changed": 1,
        "custom_printer_parser_changed": 0,
        "create_builder_changed": 1,
        "traits_changed": 0
      }}
    }}
  }}

  ═══ RULES ═══════════════════════════════════════════════════════════════

  - Check ALL 7 change types for EVERY OP — do not stop at the first hit.
  - Use `git show` / `git diff` in llvm-project — do NOT read the working
    tree directly (it may be at an arbitrary commit).
  - `needs_patch: false` ONLY when the OP definition is IDENTICAL across
    both LLVM versions for all 7 dimensions.
  - The "details" field MUST contain specific old-vs-new values for each
    detected change_type — file paths, old/new signatures, diffs.
  - Cross-reference with Ascend backend usage (grep in
    {ascend_path}/third_party/ascend/) — if Ascend never references the
    changed attribute/API, note it but still include the OP.
  - If an OP in ops_report.json no longer exists at the target hash,
    record it as op_name_changed with the old name and empty new_name.
  - An OP that was checked and found unchanged across all 7 types still
    goes in the output with change_types: [], needs_patch: false.

  Reference:
    {reference_dir}/02-llvm-version-adaptation-and-compile-fixes.md
    {reference_dir}/04-ir-compatibility-and-backend-adaptation.md
    {reference_dir}/05-ir-patch-generation-guide.md

  Rules: DO NOT modify source code. Output ONLY the structured JSON report.

── ir_generate_patch mode ──────────────────────────────────────────────

  Trigger: {mode} is "ir_generate_patch" (generate TA-side LLVM OP patches).

  ═══ PATCH FIX RETRY (patch_error_type present) ═════════════════════════

  When `patch_error_type` is "apply" or "build", a previously generated
  patch FAILED.  You are fixing it — NOT starting from scratch.

  patch_error_type: {patch_error_type}
  patch_error_msg:  {patch_error_msg}

  Fix strategy:
    - **apply failure**: the patch does not apply cleanly to the target
      LLVM commit.  Re-read the target files via git show, check line
      numbers and context, and regenerate the patch to match exactly.
    - **build failure**: the patch applied but LLVM compilation failed.
      Read the build error carefully — it tells you exactly which file
      and line has the problem.  Common causes:
        * Wrong API for the target LLVM version (check git show output)
        * Missing/extra parameters in create()/build() calls
        * Type mismatches in attribute definitions
        * Missing includes or forward declarations
      Fix the relevant section of the patch while keeping all OTHER
      sections intact — do NOT drop OPs that were correctly patched.


  ═══ PATCH SUPPLEMENT (adjust_mode=supplement) ═══════════════════════════

  When `adjust_mode` is "supplement", the existing patch was ALREADY
  applied and LLVM was built successfully, but TESTS ARE FAILING with
  IR compatibility errors.  The patch is incomplete — it is missing
  OP changes that the new LLVM version introduced.

  ⚠️  This is NOT a full generation.  You are SUPPLEMENTING an existing
  working patch with additional OP changes.  DO NOT start from scratch.

  supplement_iteration: {supplement_iteration}

  Workflow:
    1. READ the IR diagnosis ({previous_step_summary_path}) to
       understand which OPs failed and the specific error symptoms.

    2. READ the FOCUSED CHANGE ANALYSIS ({focused_changes_path}).
       This file was automatically generated by git-diffing the .td
       definitions of each affected OP between baseline LLVM
       ({baseline_llvm_hash}) and target LLVM ({target_llvm_hash}):
         - Each affected OP has its .td file path and the git diff
         - Use these diffs to understand exactly what changed upstream
         - The diff covers all 7 change types: OP name, assemblyFormat,
           attributes, custom printer/parser, create builder, traits

    3. READ the EXISTING PATCH ({ascend_patch_file}).
       The patch content snippet shows the first 5000 bytes:
         {patch_content_snippet}
       Read the full file if the snippet is truncated.

    4. For EACH affected OP in the focused analysis, determine:
       - Already in the existing patch? → the fix may be incorrect for
         the current LLVM version → UPDATE it based on the focused diff
       - NOT in the existing patch? → ADD a new section following the
         exact same patterns (see KNOWN IR PATCH PATTERNS below and
         the patch generation guide)

    5. SUPPLEMENT the patch — modify {ascend_patch_file} in-place:
       - KEEP all existing entries that are correct — do NOT drop them
       - ADD new entries for OPs that are missing
       - FIX existing entries that are incorrect (wrong API, params, etc.)
       - Use the focused diff to write precise, minimal changes
       - Follow the exact git format-patch style of the existing patch

    6. SELF-CHECK before returning:
       - Every affected OP in the focused analysis is addressed
       - The patch still applies cleanly at {target_llvm_hash}
       - No correct existing entries were removed

  ═══════════════════════════════════════════════════════════════════════

  Core strategy: patch TA-side LLVM so it generates IR compatible with the
  UNMODIFIED AscendNPU-IR. NPU-IR is NOT touched — we cannot patch or
  recompile it from the TA side.

  ⚠️  The patch MUST target LLVM at commit `{target_llvm_hash}`.
  The baseline LLVM is `{baseline_llvm_hash}` — changes_report.json
  describes what changed between these two LLVM versions.
  The patch will be applied to `{llvm_project_path}` checked out at
  `{target_llvm_hash}`, so all code modifications must be compatible
  with the target LLVM's API.

  Workflow (full generation — adjust_mode is NOT "supplement"):
    1. Read `{step_dir}/changes_report.json` for ALL OPs needing patches.
    2. Read the patch generation guide:
       `{reference_dir}/05-ir-patch-generation-guide.md`
       — core strategy, patch patterns, format requirements, validation steps.
    3. Read the patch template for concrete format examples:
       `{reference_dir}/ir_compatibility_patch_example.patch`
       This demonstrates the direct OP patching approach (NOT BC/bytecode).
    4. For each OP, view the TARGET version of its .td/.cpp file using:
         git -C {llvm_project_path} show {target_llvm_hash}:mlir/include/.../<file>.td
         git -C {llvm_project_path} show {target_llvm_hash}:mlir/lib/.../<file>.cpp
       Do NOT read the working tree directly — the checked-out commit may
       differ from `{target_llvm_hash}`.
    5. Generate a SINGLE complete `.patch` file that covers ALL OPs flagged
       with `needs_patch: true` in one unified patch. For each OP:
       - Apply the appropriate strategy by change type (per the guide):
         — OP renamed: add a backward-compatible alias (old name → new name)
         — assemblyFormat changed: modify to also accept/emit old format
         — create() params changed: add overload/defaults for old signature
         — Pass option renamed: add old option name as alias
         — attributes changed: add backward-compat getter/wrapper
         — custom printer/parser changed: preserve old output format
    6. Write the single patch directly to `{ascend_patch_file}` (modify
       the existing file in-place):
       - Follow `git format-patch` style with proper headers
       - Apply cleanly to `{llvm_project_path}` at `{target_llvm_hash}` as
         one atomic change
       - Cover every OP in changes_report — do NOT leave any out

  ═══ KNOWN IR PATCH PATTERNS ═══════════════════════════════════════════

  Apply these specific fixes when the corresponding OP appears in
  changes_report.json with needs_patch: true.

  ── empty-properties / assume-op rejection ─────────────────────────────

  Symptom: the old NPU-IR compiler rejects IR containing an OP followed
  by an empty inline property dict (e.g. the new LLVM prints the OP
  with a trailing empty dict while the old parser only expects the OP
  name without properties).  This commonly affects ops whose TableGen
  definition gained `useCustomPropertiesEncoding` or whose printer now
  emits an inline property dict.

  Fix: in the affected OP's custom printer (print() method in the
  corresponding .cpp file under mlir/lib/Dialect/), replace the
  printGenericOp block that emits inline attributes:

    // BEFORE (new LLVM — emits empty dict that old parser rejects):
    void <OpName>::print(OpAsmPrinter &p) {{
      p.printGenericOp(*this);    // ← emits attributes inline
    }}

    // AFTER (backward-compatible — filters empty properties):
    void <OpName>::print(OpAsmPrinter &p) {{
      SmallVector<NamedAttribute, 4> filtered;
      for (NamedAttribute attr : (*this)->getAttrs()) {{
        if (auto prop = dyn_cast<::mlir::Properties>(attr.getValue())) {{
          if (prop.isEmpty()) continue;  // skip empty properties
        }}
        filtered.push_back(attr);
      }}
      p << " ";
      p.printAttribute(DictionaryAttr::get(getContext(), filtered));
    }}

  If the OP uses `printGenericOp` directly (no custom printer), you
  must ADD a custom printer that filters out empty properties, AND
  set `let hasCustomAssemblyFormat = 1;` / remove `let assemblyFormat`
  in the .td file.

  ── assume-op specific fix ─────────────────────────────────────────────

  This fix applies ONLY to `llvm.assume` / `LLVM::AssumeOp`.  Do NOT
  apply it to any other OP — other ops have their own handling.

  If `LLVM::AssumeOp`'s custom printer has these three lines:
    os << " <";
    Impl::printAttribute(prop);
    os << '>';
  Simply comment them out (no replacement needed).  This suppresses
  the inline attribute printing that produces the empty dict the old
  parser cannot handle.

  ─────────────────────────────────────────────────────────────────────
  possible. Missing even one OP will cause the outer loop to retry
  (costly: LLVM rebuild takes ~2 hours). Review changes_report
  thoroughly before writing the patch — every `needs_patch: true` OP
  must have a corresponding fix in the patch.

  Reference:
    {reference_dir}/05-ir-patch-generation-guide.md
    {reference_dir}/04-ir-compatibility-and-backend-adaptation.md
    Template: {reference_dir}/ir_compatibility_patch_example.patch (single unified patch)

── ir_diagnose mode ────────────────────────────────────────────────────

  Trigger: {mode} is "ir_diagnose" (classify test failures as IR vs code).

  Workflow:
    1. Read test failure logs from {error_logs}.
    2. For each distinct failure, classify as:
       a. "ir_compatibility" — LLVM/MLIR version mismatch:
          - "unexpected op" / "custom op not registered" (OP renamed upstream)
          - Missing dialect registration
          - "attribute not found" for renamed properties
          - assemblyFormat parse error (new IR format, old parser)
          - triton-mlir-opt / bishengir-opt IR round-trip failures
       b. "code_adaptation" — upstream API/signature change:
          - Undefined symbols / missing includes
          - Function signature mismatch
          - Python ImportError / AttributeError / TypeError
          - pytest assertion changes due to behavior changes
       c. "environment" — non-code issue:
          - Timeout, OOM, resource exhaustion
          - Network failure, file not found (transient)
    3. Output to `{step_dir}/ir_diagnosis.json`:
       {{
         "failures": [
           {{
             "id": "test_load_other",
             "python_version": "3.10",
             "error_summary": "...",
             "classification": "ir_compatibility",
             "affected_op": "triton::LoadOp",
             "rationale": "..."
           }}
         ],
         "summary": {{
           "total_failures": 5,
           "ir_issues": 2,
           "code_issues": 2,
           "environment_issues": 1,
           "has_ir_issues": true
         }}
       }}

  Reference:
    {reference_dir}/03-unit-test-failure-diagnosis-and-fixes.md
    {reference_dir}/04-ir-compatibility-and-backend-adaptation.md
    {reference_dir}/diagnosis-guide.md
    {reference_dir}/error-pattern-examples.md

  Rules: DO NOT modify source code. Output ONLY the structured JSON diagnosis.

━━━ OUTPUT ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

── For ir_analyze_ops mode ──
  Output ONLY `{step_dir}/ops_report.json` — the structured JSON specified
  in the ir_analyze_ops section above. Do NOT write analysis.md,
  step_summary.md, or review.md. Do NOT analyze merge history.

── For ir_analyze_changes mode ──
  Output ONLY `{step_dir}/changes_report.json` — the structured JSON specified
  in the ir_analyze_changes section above. Do NOT write analysis.md,
  step_summary.md, or review.md. Do NOT analyze merge history.

── For ir_generate_patch mode ──
  Output ONLY by modifying `{ascend_patch_file}` in-place — the existing
  patch file. Do NOT write analysis.md, step_summary.md, or review.md.

── For ir_diagnose mode ──
  Output ONLY `{step_dir}/ir_diagnosis.json` — the structured JSON specified
  in the ir_diagnose section above. Do NOT write analysis.md, step_summary.md,
  or review.md.

── For conflict / fix / report / adapt modes ──
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
