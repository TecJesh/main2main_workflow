# Common Error Patterns Reference — Triton-Ascend

These are the most frequently seen failure patterns when upstream Triton evolves.
Use this reference when diagnosing build/test failures or applying fixes.

---

## Import / Module Path Change

**Error:** `ImportError: cannot import name 'X' from 'triton.old.path'` or
`ModuleNotFoundError: No module named 'triton.old.path'`

**Cause:** Upstream Triton moved/renamed a module or removed a symbol.

**Fix:** Update imports to match the new upstream path:
```python
# Old import
from triton.old.path import SomeClass

# New import (after merge)
from triton.new.path import SomeClass
```

Check for all references to the old import path in triton-ascend code
(`python/triton_ascend/`, `lib/Target/Ascend/`, `third_party/ascend/`).

---

## Function/Method Signature Change

**Error:** `TypeError: function_name() got an unexpected keyword argument 'X'` or
`TypeError: function_name() missing 1 required positional argument: 'X'`

**Cause:** Upstream changed a function signature — parameter added, removed, or renamed.

**Fix:** Compare the upstream signature change with the triton-ascend call site or
override. Update the call site or override to match the new signature:
```python
# If upstream added a new parameter with a default
def ascend_override(self, existing_param, new_param=None):
    ...
```

---

## Attribute/Class Change

**Error:** `AttributeError: 'SomeClass' object has no attribute 'X'`

**Cause:** Upstream renamed/removed a class attribute, method, or property.

**Fix:** Update triton-ascend references to use the new name/location. If the
attribute was moved to a different class, update the reference chain.

---

## New Abstract Method

**Error:** `TypeError: Can't instantiate abstract class AscendBackend with abstract method X`

**Cause:** Upstream added a new abstract method to a base class or interface that
the Ascend backend implements.

**Fix:** Implement the new method in the Ascend backend class:
```cpp
// In lib/Target/Ascend/ or python/triton_ascend/
ReturnType newMethod(ParamType param) override {
    // Ascend-appropriate implementation
}
```

---

## CMake / Build Configuration Change

**Error:** CMake configuration error, undefined reference, missing target

**Cause:** Upstream changed CMakeLists.txt — new targets, renamed variables,
moved source files, or changed compiler requirements.

**Fix:** Update triton-ascend's CMake configuration to match:
- Add new source files to Ascend build targets
- Update variable names if upstream renamed them
- Add new required CMake dependencies

---

## C++ Interface Change

**Error:** Compilation error in `lib/Target/Ascend/` or `lib/Conversion/` —
undefined reference, virtual function override mismatch, type mismatch

**Cause:** Upstream changed a C++ interface that the Ascend backend implements
or references.

**Fix:** Update the Ascend C++ implementation to match the new interface:
- Update function signatures to match new virtual methods
- Update type references if upstream changed types
- Add/remove includes if headers were reorganized

---

## Backend Registration Change

**Error:** Ascend backend not found, device initialization failure, or
backend-specific pass registration error

**Cause:** Upstream changed how backends are registered or discovered.

**Fix:** Update the Ascend backend registration code (typically in
`python/triton_ascend/__init__.py` or C++ backend registration in
`lib/Target/Ascend/`) to match the new registration pattern.

---

## Test Assertion Change

**Error:** pytest assertion failure in `third_party/ascend/unittest/pytest_ut/`

**Cause:** Upstream changed behavior that existing Ascend tests assert on, or
changed test infrastructure (conftest, fixtures, markers).

**Fix:** Update the test assertion or test setup to match the new expected
behavior. If the upstream change intentionally modified the behavior, update
the test. If the test failure reveals a real bug in Ascend code, fix the source.

---

## Pre-CI Check Failure

**Error source:** `{step_dir}/pre_ci_check.json`

**Cause:** Static check failure detected after an AI attempt:
- Remaining merge conflict markers
- Temporary/debug artifacts in repo
- Python syntax errors in modified files

**Fix:** Read the structured JSON and inspect the affected source files. Apply a
static code fix, then update `analysis.md` and `step_summary.md`. Do not rerun
pre_ci_check manually.

---

## Environment Flakes (NO FIX NEEDED)

These are transient infrastructure issues — note them in the report but require
no code changes:

- `TimeoutError` — network/resource timeout
- `ConnectionResetError` — transient network failure
- `torch.cuda.OutOfMemoryError` — resource exhaustion (on GPU CI)
- **NPU Out of Memory** — `npu.OutOfMemoryError`, `ACL_ERROR_FAILURE` memory,
  `device memory allocation failed`, etc. → see [`npu-oom-handling.md`](npu-oom-handling.md)
  for the full rerun-until-clear strategy
- NPU device not available — CI environment issue
- `FileNotFoundError` for runtime dependencies — environment setup issue
- Disk full, stale file handles, filelock contention

---

## Local Environment Missing Dependencies (NO FIX NEEDED)

**Error:** `ModuleNotFoundError: No module named 'triton'`, missing `triton_ascend`,
missing NPU/CANN/runtime libraries, or device discovery failures

**Cause:** The AI adaptation environment may contain source code only. Runtime
imports and device checks are not meaningful during the AI step.

**Fix:** Do not add dependency hacks, fallback imports, or code workarounds for
local environment failures. Use static source inspection only.
