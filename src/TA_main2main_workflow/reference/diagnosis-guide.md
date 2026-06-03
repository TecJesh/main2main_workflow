# Diagnosis Guide — Triton-Ascend

Use this guide during fix mode. The goal is not to rerun validation locally; it
is to read the structured `error_logs` provided by the prompt, trace each
actionable failure back to the upstream change that caused it, and update
triton-ascend statically. Never modify the Triton repository; it is only an
upstream reference.

Runtime validation is external. The main2main flow runs pre_ci_check after each
AI attempt and runs build & test after AI analysis completes.

---

## Step 1: Read structured error_logs

Fix mode receives `error_logs` from the prompt. Each entry is a structured JSON
file path produced by the main2main flow. Start from these files; do not read raw
CI logs unless a structured summary explicitly points to a small relevant section.

Possible inputs:

1. `pre_ci_check.json`
   - Produced automatically after an AI attempt when static checks fail
   - Checks for: remaining conflict markers (`<<<<<<<` / `=======` / `>>>>>>>`),
     temporary/debug artifacts in the repo, Python syntax errors in modified files
   - Fix by reading the JSON and inspecting source; do not rerun pre_ci_check manually

2. `build_result.json`
   - Produced when the build step fails
   - Contains build step results and log file path
   - Check `build.log` for compilation errors, missing symbols, CMake errors

3. `test_result.json`
   - Produced when tests fail
   - Contains pytest exit code, passed/failed/error counts, and test log path
   - Check `test-logs/pytest.log` for specific test failure details

If a summary contains only environment flakes or missing local/runtime
dependencies, record that in `analysis.md` and `step_summary.md`; do not add code
workarounds.

---

## Step 2: Classify failures

For each structured error, decide whether it is actionable:

- Build/compilation errors → fix in triton-ascend source
- Test failures → FIRST check for NPU OOM (see below), then trace to root cause
- pre_ci static issues → fix statically in triton-ascend
- Environment flakes (including NPU OOM) → no code fix; record in the analysis
- NPU OOM → see `reference/npu-oom-handling.md` — rerun ALL tests until clear,
  do NOT modify source code for OOM

**NPU OOM check:** Before classifying any test failure as actionable, scan
`test-logs/pytest.log` for OOM keywords. If any test failed with NPU OOM,
classify ALL failures as "OOM — rerun needed". Do not attempt to fix individual
failures until OOM is eliminated. See `reference/npu-oom-handling.md` for the
full strategy.

Common code-bug mechanisms in Triton-Ascend:

- `ImportError` → upstream moved a module (e.g., `triton.runtime.autotuner` → new location)
- `AttributeError` → upstream renamed/removed a class attribute or function
- `TypeError` → upstream changed a function/method signature
- `NotImplementedError` → new abstract method on a base class that Ascend overrides
- CMake/build errors → upstream changed build configuration, CMake variables, or file locations
- Compilation errors in `lib/Target/Ascend/` → upstream changed C++ interfaces that Ascend backend implements
- Pytest assertion failures → expected behavior changed due to upstream modifications

Then look up the matching pattern in `reference/error-pattern-examples.md`.

---

## Step 3: Correlate with the upstream merge

For each actionable issue:

1. Extract a stable search term from the error message or traceback, such as a
   method name, import path, class name, or keyword argument.
2. Search the merged changes in the triton-ascend working tree (the merge has
   already been committed; the fix step runs on top of it).
3. Identify the upstream intent: rename, removal, new parameter, new abstract
   method, moved module, or changed return type.
4. Map the upstream change to the triton-ascend code that subclasses, overrides,
   calls, imports, or references the changed contract.
5. Decide on the minimal fix: update imports, update signatures, update CMake
   configuration, etc.

Do not infer fixes only from symptoms. Prefer root-cause fixes tied to the
upstream diff.

---

## Step 4: Apply fixes statically

Apply the smallest triton-ascend change that restores compatibility:

- Update imports when upstream moves modules
- Update function signatures when upstream changes APIs
- Update CMakeLists.txt when build configuration changes
- Implement new required interface methods with Ascend-appropriate behavior
- Preserve all Ascend-specific code paths (triton_ascend/, third_party/ascend/, lib/Target/Ascend/)
- Do not modify upstream triton code (python/triton/) unless it contains Ascend-specific changes

Do not run tests, build commands, or import triton/triton-ascend. Those checks
happen outside the AI step.

---

## Step 5: Write analysis, review, and summary

Write fix diagnosis into `{step_dir}/analysis.md`. For fix mode, include:

- Structured error source file(s)
- Classification: build error, test failure, pre_ci static issue, or non-actionable
  environment issue
- Root-cause upstream change and affected file/symbol
- Affected triton-ascend file(s)
- Fix plan and implemented fix

Write `{step_dir}/review.md` with static review results:
- Diff reviewed
- Function signatures checked
- Imports/config accesses checked
- Remaining risks or no known issues

Update `{step_dir}/step_summary.md` with the fix summary.

---

## Stop conditions controlled externally

The main2main flow controls retry limits and validation. Do not try to rerun CI
or override the retry policy manually.

During this AI step, stop after:
- Applying the static fix, or determining there is no actionable code fix
- Writing `analysis.md`, `review.md`, and `step_summary.md`

The next build/test round will be triggered by the main2main flow.
