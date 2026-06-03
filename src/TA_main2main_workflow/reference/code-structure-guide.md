# Code Structure Guide — Triton-Ascend

Use this guide as a routing reference when mapping upstream Triton changes
to likely triton-ascend files.

---

## Triton Key Areas

1. **Triton Language Frontend** (`python/triton/`)
   - Core Triton APIs: `triton.jit`, `triton.autotune`, `triton.heuristics`
   - Changes here affect all backends including Ascend

2. **Triton Compiler** (`lib/`)
   - Core compilation pipeline: `lib/Conversion/`, `lib/Dialect/`, `lib/Analysis/`
   - Target-specific backends: `lib/Target/`
   - Ascend backend: `lib/Target/Ascend/`

3. **C++ Public Headers** (`include/triton/`)
   - Dialect definitions, conversion passes, target interfaces

4. **Third-Party Backends** (`third_party/`)
   - NVIDIA backend: `third_party/nvidia/`
   - AMD backend: `third_party/amd/`
   - **Ascend backend**: `third_party/ascend/`

5. **Build System** (`CMakeLists.txt`, `cmake/`, `setup.py`)

6. **Testing** (`python/test/`, `test/`, `unittest/`)

---

## Triton-Ascend Key File Locations

| Component | Path |
|-----------|------|
| Ascend backend registration | `python/triton_ascend/__init__.py` |
| Ascend runtime | `python/triton_ascend/runtime.py` |
| Ascend device utils | `python/triton_ascend/utils.py` |
| Ascend target implementation | `lib/Target/Ascend/` |
| Ascend NPU backend (C++) | `third_party/ascend/` |
| Ascend kernel library | `third_party/ascend/kernels/` |
| Ascend unit tests (pytest) | `third_party/ascend/unittest/pytest_ut/` |
| Top-level CMake | `CMakeLists.txt` |
| Version tracking | `version.txt` |

---

## File Mapping Table

| Upstream Triton path | Triton-Ascend path(s) | What to check |
|:---|:---|:---|
| `python/triton/__init__.py` | `python/triton_ascend/__init__.py` | Backend registration |
| `python/triton/runtime/` | `python/triton_ascend/runtime.py` | Device driver, memory |
| `python/triton/compiler/` | `python/triton_ascend/` | Compilation flags |
| `python/triton/language/` | `python/triton_ascend/`, `third_party/ascend/kernels/` | Language builtins |
| `lib/Target/NVPTX/` | `lib/Target/Ascend/` | Code generation patterns |
| `lib/Conversion/` | `lib/Conversion/`, `lib/Target/Ascend/` | Dialect conversion |
| `lib/Dialect/` | `lib/Target/Ascend/` | Triton dialect ops |
| `include/triton/Target/` | `lib/Target/Ascend/` | Target interface |
| `include/triton/Dialect/` | `lib/Conversion/`, `lib/Target/Ascend/` | Dialect definitions |
| `third_party/nvidia/` | `third_party/ascend/` | Backend structure |
| `CMakeLists.txt` | `CMakeLists.txt`, `cmake/` | Build targets |
| `setup.py` | `setup.py` | Python package config |
