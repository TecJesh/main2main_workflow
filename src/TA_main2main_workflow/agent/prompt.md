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

  Ascend Backend Adaptation tracking — in conflict, fix, setup_sync, and
  patch_fix modes, whenever you adapt Ascend-side code or patches to
  follow upstream changes (API/signature updates, attribute or op
  renames, deleted frontend ops, LLVM version changes, setup.py syncs,
  patch hunk adjustments), record EACH adaptation point in
  {step_dir}/step_summary.md under the heading "Ascend Backend
  Adaptation", one line per entry, near the TOP of the file:

    ## Ascend Backend Adaptation
    - <upstream change: commit SHA or file/API> → <ascend-side change: files + what was adapted>

  The report mode collects these entries into the final PR description.

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

    ## Ascend Backend Adaptation
    - Collect every "Ascend Backend Adaptation" entry from the step
      summaries and group them by adaptation type (upstream API /
      signature changes, attribute / op renames, deleted frontend ops,
      LLVM version changes, setup.py syncs, patch adjustments).
    - For each entry list: the upstream change that triggered it
      (commit SHA or file/API), the Ascend-side files changed, and the
      adaptation made.
    - Also scan the sync commits for [Sync](fix) commits and changes
      under third_party/ascend/, lib/Target/Ascend/,
      python/triton_ascend/, and third_party/ascend/patch/*.patch for
      adaptation points the step summaries may have missed.
    - Adaptation points that still require human work (ops temporarily
      preserved via the TA source patch) are detailed in "Manual
      Adaptation Required" — mention them here briefly and point to
      that section for details.
    - Omit this section only when the sync required no Ascend-side
      adaptation.

    ## Impact
    - Which Ascend backend modules are affected (python/triton_ascend/,
      third_party/ascend/, lib/Target/Ascend/)
    - Any API/ABI changes that downstream consumers need to know about
    - Test results: passed/failed counts per suite

    ## Manual Adaptation Required
    - Collect every "Manual adaptation required" entry from the step
      summaries (deleted frontend op strategy): for each entry list
      the op, the upstream commit that removed it, the files restored
      into the TA source patches, and the skipped tests.
    - State clearly that these are intentional TEMPORARY
      preservations: the Ascend backend keeps the op via the TA source
      patch, and a human must adapt/remove them later — do NOT claim
      they are resolved.

    ## Known Issues / Skipped Tests
    - Collect every "Known Issues / Skipped Tests" entry from
      the step summaries (tests skipped at the FINAL fix retry): list
      every skipped test with its node id, file path, and skip reason.
    - State clearly that these tests were still FAILING after the
      maximum number of fix retries and were skipped at the final
      retry — they are KNOWN ISSUES pending manual follow-up, they do
      NOT pass.  Say "all tests pass with N case(s) skipped (known
      issues below)"; do NOT claim a fully green suite.
    - Omit this section when no step summary contains such entries.

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
    6. Write conflict resolution summary to {step_dir}/step_summary.md —
       include "Ascend Backend Adaptation" entries (shared rule above)
       for every conflict where Ascend code was adapted to upstream changes
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

── deleted frontend op strategy ──────────────────────────────────────

  Applies in conflict and fix modes.  Trigger: the upstream code merged
  this step DELETED a frontend TritonOp from its .td definition file
  (include/triton/Dialect/Triton/IR/*.td, together with its C++ side
  under lib/Dialect/Triton/IR/ when needed) that the Ascend backend
  (third_party/ascend/, lib/Target/Ascend/) still references —
  typically surfaced as build errors (unknown op, missing
  symbol/registration) or test failures.

  ⚠️  DO NOT follow the deletion.  The Ascend backend code for the op
  is KEPT, and the op itself is preserved via the TA source patch
  scheme.  These op changes are temporary preservation — they must be
  listed in the PR description and will be adapted MANUALLY by the
  team later.  Full details:
  {reference_dir}/06-deleted-frontend-op-strategy.md

  Rules (in order):

    1. KEEP the Ascend backend code for the op
       (third_party/ascend/, lib/Target/Ascend/).  Adapt it only when
       OTHER upstream API changes force a minimal compile fix — never
       delete the op handling.

    2. RESTORE the pre-merge op definition into the merged .td file
       (and its C++ implementation when the build needs it).  In fix
       mode the step's merge commit is HEAD and its first parent is
       the pre-merge tree — recover the deleted op from there:
         git -C {ascend_path} show HEAD^1:<path>
       (the reverse diff `git -C {ascend_path} diff HEAD HEAD^1 --
       <path>` shows exactly what was deleted).  If the op was already
       gone before this step, use `git -C {ascend_path} log` to find
       the last commit that still had it.  Re-apply the op
       definition/registration to the merged file, keeping the
       surrounding merged code intact.

    3. RECORD every restored file in {step_dir}/op_restore_files.txt —
       one repo-root relative path per line (e.g.
       include/triton/Dialect/Triton/IR/TritonOps.td,
       lib/Dialect/Triton/IR/Ops.cpp), ONLY files where you restored
       deleted frontend op code.  The workflow regenerates those edits
       into the TA source patch
       (third_party/ascend/patch/triton-ascend-*.patch), restores the
       sources to HEAD, and the build re-applies the patch — the
       restored op therefore survives every build.  Do NOT edit the
       .patch files yourself in this mode.

    4. SKIP the affected tests instead of deleting them: add
         @pytest.mark.skip(
             reason="upstream removed <op>; Ascend backend keeps it "
                    "via TA source patch — manual adaptation required")
       to each test exercising the deleted op (tests under
       third_party/ascend/unittest/ only).  The reason MUST name the
       op and state that manual adaptation is pending.

    5. DOCUMENT in {step_dir}/step_summary.md under a
       "Manual adaptation required" heading: each restored op, the
       upstream commit that deleted it, the restored files, and the
       skipped tests.  The report mode collects these entries into the
       PR description.

  Conflict-mode note: when a conflict is caused by an upstream op
  deletion, keep the Ascend backend side of the conflict and accept
  the upstream deletion in the frontend files — do NOT re-add the op
  during conflict resolution.  The fix loop then applies the strategy
  above when the backend fails to build against the missing op.

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
       hunks were re-positioned, under the "Ascend Backend Adaptation"
       heading (shared rule above).

  Constraints:
    - Only edit {patch_file}.  Do NOT touch source files.
    - Do not merge or split hunks; do not change the changed lines.

── setup_sync mode ───────────────────────────────────────────────

  Trigger: {mode} is "setup_sync" (this merge step changed setup.py —
  upstream modifications must be ported into setup_ascend.py).

  Context: triton-ascend builds with `python setup_ascend.py install`.
  setup_ascend.py is the Ascend fork of setup.py — it starts from
  upstream setup.py and adds Ascend-specific build logic.  When the
  merged upstream code changes setup.py, setup_ascend.py must carry
  the same changes WITHOUT losing its Ascend-specific parts.

  Your task:
    1. Read {step_dir}/setup_diff.txt — the upstream diff of setup.py
       for this step.
    2. Read {ascend_path}/setup_ascend.py.
    3. Port EVERY upstream change into setup_ascend.py:
       - new/changed/removed dependencies, version strings, build
         flags, CMake args, package data, entry points, install
         requirements — anything setup.py gained, changed, or lost
       - adapt each change to setup_ascend.py's structure while
         preserving ALL Ascend-specific logic (Ascend backend wiring,
         LLVM paths, patch application, wheel name, etc.)
    4. NEVER edit setup.py itself — upstream owns it.
    5. Verify: re-read both files and confirm setup_ascend.py now
       reflects every upstream setup.py change.
    6. Write a short summary to {step_dir}/step_summary.md and list the
       files you modified in {step_dir}/modified_files.txt.  In the
       summary, add an "Ascend Backend Adaptation" entry (shared rule
       above): which upstream setup.py changes were ported into
       setup_ascend.py.

  Constraints:
    - Only edit {ascend_path}/setup_ascend.py.
    - If you determine NO sync is needed, make no edits and say so in
      the summary.

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
       - third_party/ascend/patch/npuir_adapter_to_llvm_23.patch
         (regenerated by the workflow from your AscendNPU-IR fixes)
       - any other regenerated third_party/ascend/patch/*.patch file
         (e.g. triton-ascend-*.patch updated by the workflow from your
         restored frontend op code) — NEVER llvm_patch_*.patch
       - EXCLUDE every file listed in patch_touched_parent /
         patch_touched_submodule — patch-applied content and
         submodule source edits must never be committed (the
         submodule edits live in the regenerated npuir patch; restored
         frontend op edits live in the regenerated TA source patch)
       - EXCLUDE build/test artifacts, logs, and temp files
    3. Write {step_dir}/commit_message.txt — a ONE-LINE commit
       subject describing the fixes (under 72 characters, no "fix:"
       prefix; the workflow wraps it as [Sync](fix)).
    4. If nothing should be committed, write an empty commit_files.txt.

── pipeline_test_analyze mode ─────────────────────────────────────

  Trigger: {mode} is "pipeline_test_analyze" (the full pipeline test
  script finished; your job is to analyze the raw pytest logs).

  Context (in {error_logs} as JSON):
    - "log_files": list of raw log file paths, grouped per test repo
      (they live under {test_workspace}/test_log/<repo>_test_log/)
    - "hints": deterministic pre-parsed counts / failure lines
      (best-effort — verify and correct them from the logs)

  Your task:
    1. Read the log files (grep/tail — do NOT dump huge logs).
    2. For EACH test repo produce: total / passed / failed / errors /
       skipped counts (from pytest summary lines like
       "1234 passed, 5 failed, 2 errors, 3 skipped in 123.45s") and
       the list of FAILING cases ("FAILED path::test_id" or
       "ERROR path::test_id" short-summary lines plus their tracebacks).
    3. For each failing case classify the problem into EXACTLY one of:
       - "operator"       — 算子问题: wrong kernel/operator semantics —
         numerical/assertion mismatches, wrong results, bugs in
         operator lowering/codegen (lib/Dialect/, lib/Conversion/,
         lib/Target/Ascend/, include/triton .td ops)
       - "triton_ascend"  — Triton—Ascend问题: Ascend adaptation-layer
         problems — compile failures, unsupported features, runtime /
         binding issues in third_party/ascend/,
         python/triton_ascend/, python/triton/extension
       - "npu_ir"         — NPU-IR问题: errors mentioning AscendNPU-IR /
         bishengir / npuir / MLIR-NPU IR failures
       Default to "triton_ascend" when evidence is weak.
    4. Write {step_dir}/analysis.json EXACTLY matching this schema:

{{"repos": [{{"repo": "<repo name>",
              "total": <int>, "passed": <int>, "failed": <int>,
              "errors": <int>, "skipped": <int>,
              "cases": [{{"case": "tests/...::test_id[param]",
                          "error": "<error message excerpt>",
                          "category": "operator|triton_ascend|npu_ir",
                          "log_file": "<relative log path>"}}]}}]}}

  Rules:
    - total = passed + failed + errors + skipped (when derivable)
    - One "cases" entry per failing case; "error" is a SHORT excerpt
      (≤300 chars) of the failure/traceback
    - DO NOT modify any source code; your ONLY output is analysis.json

── pipeline_test_fix mode ─────────────────────────────────────────

  Trigger: {mode} is "pipeline_test_fix" (one failing pipeline-test
  case must be fixed in the triton-ascend repo).

  Context (in {error_logs} as JSON):
    - "case":        the failing test case id
    - "category":    problem classification (see below)
    - "error":       error excerpt from the analysis
    - "log_file":    raw log path under {test_workspace}/test_log/
    - "rerun_log":   path where the precise rerun output will land
    - "repo_dir":    the external test repo directory (read-only —
                     fixes go into triton-ascend, NOT the test repo)
    - "attempt" / "max_retries": fix attempt position

  Problem categories (from the analysis):
    - operator       (算子问题): fix the operator implementation in
                     triton-ascend — allowed paths:
                       {ascend_path}/lib/Dialect/
                       {ascend_path}/lib/Conversion/
                       {ascend_path}/lib/Target/Ascend/
                       {ascend_path}/include/triton/Dialect/Triton/IR/
                       {ascend_path}/third_party/ascend/
                       {ascend_path}/python/triton_ascend/
                     Directly modify the operator code; no special
                     patch handling needed.
    - triton_ascend  (Triton—Ascend问题): fix the Ascend adaptation
                     layer — allowed paths:
                       {ascend_path}/third_party/ascend/
                       {ascend_path}/python/triton/extension/
                       {ascend_path}/python/triton/runtime/libentry.py
                       {ascend_path}/setup_ascend.py
                       {ascend_path}/python/triton_ascend/
                     The workflow rebuilds triton-ascend after your fix.
    - npu_ir         (NPU-IR问题): fix code under
                       {ascend_path}/third_party/ascend/AscendNPU-IR/
                     (the AscendNPU-IR submodule).  The workflow moves
                     your edits into
                     third_party/ascend/patch/npuir_adapter_to_llvm_23.patch
                     and restores the sources before rebuilding (the
                     build applies the patch itself) — do NOT edit
                     .patch files, and note in your summary which
                     AscendNPU-IR files you edited.  When
                     ascend_npu_ir_fix is "true", read
                     {ascend_npu_ir_compat_ref} for the LLVM-version
                     adaptation catalog.

  Workflow:
    1. Read the error excerpt + relevant log sections (grep around the
       failing case in "log_file").
    2. Consult reference docs as in fix mode:
       {reference_dir}/03-unit-test-failure-diagnosis-and-fixes.md
       and {reference_dir}/error-pattern-examples.md.
    3. Apply a MINIMAL root-cause fix in triton-ascend, only in the
       allowed paths for the category above.  Any file outside them
       will be REVERTED by the workflow and the attempt rejected.
    4. Write {step_dir}/modified_files.txt — one repo-root relative
       path per line (e.g. lib/Conversion/.../X.cpp).  List ONLY files
       YOU changed.  For AscendNPU-IR edits (npu_ir category) list
       each file as third_party/ascend/AscendNPU-IR/<submodule-relative
       path>.
    5. Write {step_dir}/rerun_command.txt — ONE bash command line that
       re-runs EXACTLY this single failing case inside "repo_dir",
       e.g.:
         cd <repo_dir> && pytest -sv -n 16 tests/fla/test_xxx.py::test_xxx[param] > <rerun_log> 2>&1
       Use the pytest node id from the case; keep the -n worker count
       from the original log when visible; target ONLY this case.
    6. Write {step_dir}/commit_message.txt — ONE-LINE subject
       (≤72 chars, no "fix:" prefix) describing the case fixed, e.g.
       "Fix Q3TritonKernel test_cross_entropy_bwd bf16 numerical mismatch"
    7. Write {step_dir}/step_summary.md — short fix note: WHICH case
       was fixed, the root cause, and WHY this change fixes it.  The
       report mode collects these into the PR description.
    8. SELF-REVIEW: verify every modified file is within the category's
       allowed paths, then return.

── pipeline_test_report mode ──────────────────────────────────────

  Trigger: {mode} is "pipeline_test_report" (pipeline test finished;
  generate the PR description).

  Context (in {error_logs} as JSON):
    - "fix_summaries": accumulated per-case fix notes (WHICH case was
      fixed + WHY) from the fix attempts
    - "remaining_failures": cases still failing after the final round
      (Known Issues)
    - "overview": final per-repo pass/fail counts

  Your task:
    Write {step_dir}/step_summary.md as the PR description (English —
    it goes to a GitHub PR) with these sections:

      ## Summary
      - Pipeline test run against triton-ascend: result summary
      - Total fixed cases: N; still failing: M

      ## Fixed Cases
      - For EVERY fixed case: the test case id (repo + node id), the
        problem category (算子问题 / Triton—Ascend问题 / NPU-IR问题),
        and WHY the fix was made (root cause → change) — collect this
        from the fix_summaries entries verbatim where possible.

      ## Known Issues
      - List every remaining failing case with its error and category.
        State clearly these are still FAILING and need manual follow-up.

      ## Test Reports
      - Point to the Excel reports under {test_workspace}/analysis/
        (错误信息总览表.xlsx, 错误信息汇总表.xlsx).

  Rules:
    - DO NOT modify source code
    - Write in English
    - Cite exact case ids; keep it reviewer-ready

── fix mode ───────────────────────────────────────────────────────

  Trigger: {mode} is "fix" (build or tests failed).

  ══════ FINAL RETRY (attempt {attempt}/{max_retries}): SKIP FAILING TESTS ══════

  This block applies ONLY when {attempt} equals {max_retries} (the very
  last fix retry — if the tests do not pass now, the whole sync fails)
  AND the failures are TEST failures (pytest/UT).  Do NOT try to fix
  the failing tests any further.  Instead:

    1. Read the failing test names from the failure logs in {error_logs}
       (the JUnit XML files list each failing testcase).
    2. For EVERY still-failing test case, add
         @pytest.mark.skip(reason="<specific root-cause summary>")
       to the failing test function.  The reason must be specific (the
       error message / root cause, e.g. "AssertionError: expected 4,
       got 2 after upstream sync") and must state that this is a
       last-retry skip pending manual follow-up, e.g.
         @pytest.mark.skip(
             reason="fails after upstream sync (AssertionError: "
                    "expected 4, got 2) — skipped at final retry, "
                    "manual follow-up required")
       Skip markers go into test files under
       {ascend_path}/third_party/ascend/unittest/ ONLY (same path rule
       as the deleted-frontend-op strategy).  Keep existing decorators
       and the test body unchanged.
    3. Build errors are NOT covered by this rule — if the failure is a
       compile/build error, keep fixing it normally.
    4. In {step_dir}/step_summary.md, add a
       "Known Issues / Skipped Tests" heading listing EVERY
       skipped test: node id, file path, and the skip reason.  The
       report mode copies these entries into the PR description so
       reviewers know exactly which tests do not pass.
    5. Then continue with the normal fix-mode workflow below (file
       lists, commit message, summary).

  When {attempt} is below {max_retries}, ignore this block and fix the
  failures normally.

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

  ═══ AscendNPU-IR patch scheme (build auto-applies patches) ═════════

  The project keeps intrusive modifications in patch files under
  third_party/ascend/patch/, applied automatically at build time
  (setup.py restores the target files to HEAD before applying):

    Parent repo:              {patch_touched_parent}
    AscendNPU-IR submodule:   {patch_touched_submodule}

  Rules:
    - Fixes in ANY file under third_party/ascend/AscendNPU-IR/ (the
      npuir patch currently touches: {patch_touched_submodule}) stay
      in the source ONLY as an intermediate step: before the next
      build the workflow moves your edits into
      third_party/ascend/patch/npuir_adapter_to_llvm_23.patch (new
      files get a new section appended) and restores the source
      files; the build itself applies the updated patch.  The patch
      update is committed only after the build/tests pass.  Your
      earlier npuir fixes accumulate through the patch — on a later
      attempt they are already applied to the tree, so do NOT redo
      them.  Such source edits are NEVER committed directly.
    - Keep such fixes minimal and localized so they regenerate
      cleanly into the patch; note in your summary which
      AscendNPU-IR files you edited.
    - Fixes in all OTHER allowed paths (Ascend backend code etc.)
      are committed normally — no patch involvement.
    - Do NOT edit the .patch files themselves in this mode (merge
      drift adjustment is handled separately in patch_fix mode).

  ═══════════════════════════════════════════════════════════════════════

  Workflow:
    1. Read structured error output from {error_logs}
    2. Classify each failure:
       - Build error → check include paths, missing symbols, CMake changes
       - Import error → module path or symbol may have moved upstream
       - Test failure → trace back to root cause in source code
       - Upstream deleted a frontend TritonOp from its .td file that
         the backend still uses (unknown op / missing symbol traced to
         include/triton/Dialect/Triton/IR/*.td) →
         apply the "deleted frontend op strategy" section above
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
         For runtime / kernel-launch / binding / cache failures the fix
         usually belongs in python/triton/extension/ (Ascend C extension
         bindings) or python/triton/runtime/libentry.py — check those
         before patching unittest-side code.
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
       - For test skips (deleted frontend op strategy or FINAL-RETRY
         skip), verify the test file is under
         {ascend_path}/third_party/ascend/unittest/
       - For op restorations (deleted frontend op strategy), verify the
         file is listed in {step_dir}/op_restore_files.txt AND is under
         one of:
           {ascend_path}/include/triton/Dialect/Triton/IR/
           {ascend_path}/lib/Dialect/Triton/IR/
       - If ANY modified file is outside these paths, REVERT that change
         BEFORE returning — the fix will be rejected by the workflow.
       - Confirm the fix directly addresses the root cause, not just
         silences the error.
    7. Write the list of files you modified to
       {step_dir}/modified_files.txt — one repo-root relative path per
       line (e.g. python/triton/extension/build.py).  The workflow uses
       this list to validate that your changes stay within the allowed
       paths.  List ONLY the files YOU changed — do NOT include
       pre-existing working-tree changes such as the build-applied
       patch content.
       When you applied the deleted frontend op strategy, ALSO write
       {step_dir}/op_restore_files.txt — one repo-root relative path
       per line, exactly the subset of modified_files.txt where you
       restored deleted frontend op code.  The workflow regenerates
       those files into the TA source patches (they are never
       committed directly).
    8. Write fix summary to {step_dir}/step_summary.md — include
       "Ascend Backend Adaptation" entries (shared rule above) for
       every upstream change you adapted the Ascend backend to
    9. Write a ONE-LINE commit subject to {step_dir}/commit_message.txt
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

━━━ OPTIONAL MCP: triton-report ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  A "triton-report" MCP service MAY be connected to this session (it is
  configured in GitHub Actions runs; local runs may not have it).  It
  provides a knowledge base of daily upstream triton commit analyses
  (intent / risk / ascend impact), architecture knowledge, cross-project
  triton ↔ triton-ascend mappings, the TA patch catalog, adaptation
  status, and adaptation lessons recorded from previous main2main runs.

  Its tools appear as mcp__triton-report__<name>.  They are OPTIONAL —
  you DECIDE whether to call them based on whether they would help the
  current task.  If the tools are unavailable, ignore this section and
  proceed normally — never fail because of them.

  High-value uses:
    - Before diagnosing a build/test failure, query recorded adaptation
      lessons (get_adaptation_lessons) with symptom keywords — a
      matching lesson can fix the failure in one pass.
    - For the upstream commits merged in this step: use
      get_commit_impact_batch / get_ascend_impact_summary /
      get_commit_diff to understand intent and ascend impact before
      adapting code.
    - When the problem involves patched modules: get_cross_project_mapping
      and get_patch_catalog.
    - Architecture questions: get_architecture_overview,
      get_interface_surface, get_key_abstractions.
    - Adaptation status: get_pending_adaptations, get_adaptation_guide.

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
  {reference_dir}/06-deleted-frontend-op-strategy.md
      — upstream deleted a TritonOp from its .td file: keep backend,
        restore the pre-merge op into the TA source patch, skip tests
        with reasons, PR "manual adaptation" list
      — USE FOR: deleted frontend TritonOp in .td files (conflict or fix mode)

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
      - Other project files (CMakeLists.txt, setup.py, setup_ascend.py, etc.)
  - **NEVER keep triton-ascend's version of the LLVM pin file
    (`cmake/llvm-hash.txt`, or `cmake/llvm-info.json` in newer upstream code)
    in a merge conflict.** It must ALWAYS follow upstream triton. The LLVM version
    is controlled by upstream; triton-ascend's LLVM patches are applied
    separately and do NOT depend on a different LLVM hash. In any merge
    conflict on this file, accept the upstream (incoming) version
    unconditionally — do not preserve the triton-ascend side.
  - When unsure about an upstream change's impact, search the triton-ascend
    codebase for references to the changed symbol/file
  - setup_ascend.py is the Ascend build entry (`python setup_ascend.py
    install`) — the TA-side fork of setup.py. It MAY be modified when
    fixing build errors, but it must carry the upstream setup.py changes
    merged in this step (the workflow syncs it automatically; preserve
    all Ascend-specific logic it contains).

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
    The target LLVM version is specified in cmake/llvm-hash.txt (or the
    `llvm_hash` field of cmake/llvm-info.json in newer upstream code). OPs must
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
    - **filecheck failure**: the patch applied and LLVM + Triton-Ascend
      built successfully, but the LLVM20Compat FileCheck guard failed —
      the printed IR text does not match the LLVM 20 baseline.  Read
      the failing test file(s) and FileCheck error output to find which
      OP prints LLVM 23-only fields/syntax, then fix the corresponding
      section of the patch.

      ⚠️  FOLLOW THE KNOWLEDGE BASE — do NOT rewrite the patch
      arbitrarily.  Before editing, read and follow the established
      LLVM patch adjustment strategy:
        - {reference_dir}/05-ir-patch-generation-guide.md — §2.2 the
          canonical per-change-type patch strategies (compat alias for
          renamed ops, dual-format assemblyFormat, alias getters,
          overloads with defaults, pass-option aliases), §3 the OP
          definition check list, §4 patch organization rules
          (git format-patch style, only mlir/ .td/.cpp files)
        - {reference_dir}/02-llvm-version-adaptation-and-compile-fixes.md
          — §7.2 the LLVM patch mechanism and how existing patches are
          structured; check for conflicts with the AscendNPU-IR-side
          patches
        - {ascend_path}/third_party/ascend/unittest/Conversion/LLVM20Compat/README.md
          — the 21-op compatibility table: expected LLVM 20 text vs
          actual LLVM 23 print vs the intended approach — match it

      Apply the strategy that fits the failing OP (print elision,
      backward-compatible printer/parser, restoring removed ops such as
      ToMemrefOp, etc.), mirror the existing patch's style, and make
      MINIMAL targeted edits.  Keep all OTHER sections intact — do NOT
      drop or reformat OPs that were correctly patched.  Do not invent
      new mechanisms when the guide already defines one.


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
  - {step_dir}/op_restore_files.txt — ONLY when the deleted frontend
    op strategy was applied: one repo-root relative path per line for
    every file where deleted frontend op code was restored.  The
    workflow regenerates these into the TA source patches; they are
    never committed directly.

After completing all work and writing archive files, stop. No final JSON
or extra summary output is required.
