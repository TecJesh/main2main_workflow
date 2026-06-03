# Adapt Guide — Triton-Ascend Upstream Sync

Use this guide during the merge conflict resolution and test fixing phases
of each main2main sync step. The goal is to successfully merge upstream Triton
changes into triton-ascend while preserving all Ascend-specific functionality.

---

## Re-orient (every step)

Re-read this file at the start of every step. For code-structure routing, use
`reference/code-structure-guide.md` when you need to map changed upstream
paths/symbols to likely triton-ascend locations.

---

## Understanding Triton-Ascend's Architecture

Triton-Ascend is a **fork** of upstream Triton that adds Ascend NPU backend support.

1. **Upstream code lives alongside Ascend code**: `python/triton/` is upstream
   code; `python/triton_ascend/` is Ascend-specific.

2. **Ascend backend in third_party**: `third_party/ascend/` contains the
   Ascend NPU backend implementation.

3. **Build system integration**: Ascend-specific CMake configurations are
   integrated into the upstream build system.

4. **Test infrastructure**: Ascend-specific tests live in
   `third_party/ascend/unittest/`.

---

## Conflict Resolution Strategy

### Priority Order

1. **Ascend-specific additions** (python/triton_ascend/, third_party/ascend/) —
   Always preserve.

2. **Ascend-modified upstream files** — Some files in python/triton/, lib/,
   or include/ contain Ascend-specific modifications. Identify by:
   - Imports of `triton_ascend`
   - References to `ascend` device type
   - `torch_npu` or `CANN` references

3. **Pure upstream files** — Accept upstream changes.

### Conflict Resolution Process

For each conflicted file:
1. Read the full conflict content from snapshot files in `{conflict_dir}/`
2. Identify which side is upstream (incoming) and which is current (triton-ascend)
3. Determine if the current side has Ascend-specific additions
4. When both sides modify the same region, integrate both
5. After resolving, verify no remaining conflict markers
6. Stage the resolved file: `git add <filepath>`

---

## Test Fixing Strategy

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| Undefined reference | Upstream added/removed a symbol | Update Ascend backend |
| CMake configuration error | Upstream changed CMake variables | Update Ascend CMakeLists.txt |
| Header not found | Upstream moved/renamed a header | Update include paths |
| ImportError | Upstream moved a module | Update imports |
| AttributeError | Upstream changed a class/function | Update Ascend references |
| TypeError | Upstream changed a signature | Update Ascend callers/overrides |

---

## Key Directories and Their Roles

| Directory | Role | Merge Strategy |
|-----------|------|---------------|
| `python/triton/` | Upstream Triton Python package | Accept upstream, preserve Ascend modifications |
| `python/triton_ascend/` | Ascend-specific Python code | Always preserve |
| `lib/` | C++ compiler and runtime | Accept upstream, preserve Ascend backend |
| `lib/Target/Ascend/` | Ascend C++ backend | Always preserve |
| `include/triton/` | C++ public headers | Accept upstream |
| `third_party/ascend/` | Ascend NPU backend + tests | Always preserve |
| `third_party/nvidia/` | NVIDIA GPU backend | Accept upstream |
| `CMakeLists.txt` | Build configuration | Accept upstream, preserve Ascend targets |
