# NPU Out-of-Memory Error Handling — Triton-Ascend

## Summary

NPU out-of-memory (OOM) errors in pytest runs are **transient resource allocation
failures**, not code bugs. They happen when parallel test workers allocate more
NPU memory than available at a given moment. Rerunning tends to resolve them
because memory allocation patterns differ across runs.

**Core rule:** If any test fails with NPU OOM, rerun the **entire** test suite
until OOM disappears, then address remaining failures.

---

## Recognition

Look for these patterns in pytest output, build logs, or error traces:

| Pattern | Example |
|---------|---------|
| `ascend` OOM | `torch_npu.npu.OutOfMemoryError`, `ACL_ERROR_FAILURE` with "out of memory" |
| `CANN` OOM | `runtime error: device memory allocation failed` |
| `NPU` OOM | `npu memory allocation failed`, `Cannot allocate memory on device` |
| Driver-level | `drvDeviceAlloc failed`, `hccs memory error` |
| Python wrapper | `RuntimeError: NPU out of memory`, `torch.OutOfMemoryError` on NPU device |

**Key:** Any error containing "memory" or "OOM" tied to an NPU device, CANN
runtime, or ascend backend is a candidate for this pattern.

---

## Strategy: Rerun-All-Until-Clear

### Step 1: Confirm it's OOM

Scan the test output (`test-logs/pytest.log`) for OOM keywords. If at least one
failure is NPU OOM, proceed to Step 2.

### Step 2: Rerun the full test suite

Do **NOT** rerun only failed tests — memory pressure depends on the full set of
concurrently-running test cases. Rerun **all** tests:

```bash
cd <triton-ascend>
pytest -n 16 third_party/ascend/unittest/pytest_ut/
```

If your environment uses a different number of workers (`-n`), keep the same
value as the original run.

### Step 3: Check results — loop if needed

```
┌─────────────────────────────────────────────────────┐
│                                                     │
│  Run FULL test suite                                │
│       │                                             │
│       ▼                                             │
│  Any NPU OOM errors?  ── Yes ──► Rerun FULL suite  │
│       │                                     │       │
│       No                                    │       │
│       ▼                                     │       │
│  Other failures remain?  ── Yes ──► Fix them        │
│       │                                             │
│       No                                            │
│       ▼                                             │
│  Done — tests pass                                  │
│                                                     │
└─────────────────────────────────────────────────────┘
```

- Keep rerunning until **zero** NPU OOM errors appear in the output.
- If OOM persists beyond 5 consecutive runs, try reducing parallelism:
  ```bash
  pytest -n 8 third_party/ascend/unittest/pytest_ut/
  ```
  Then retry with full `-n 16`.
- Only after OOM is gone, classify and fix remaining failures.

### Step 4: Classify remaining failures

Once OOM is eliminated, remaining failures fall into normal categories:

- ImportError / AttributeError / TypeError → upstream API change
- Assertion failure → behavior change, fix or update test
- Compilation / link error → build configuration mismatch
- Other → see `error-pattern-examples.md`

---

## What NOT to do

| Don't | Why |
|-------|-----|
| Skip OOM-failing tests | They may pass on rerun; skipping loses coverage |
| Reduce batch sizes in test code | Masks the issue; the real fix may be unrelated |
| Add `torch.npu.empty_cache()` calls | Doesn't fix the root cause; OOM is transient |
| Reduce `-n` permanently | Fewer workers = slower CI; only use temporarily |
| Mark tests as `xfail` for OOM | OOM is environment-level, not test-level |

---

## Reporting

When OOM was encountered and resolved by rerunning, note it in `analysis.md`:

```markdown
## NPU OOM — Resolved by Rerun

- **Occurrences**: 3 OOM failures in initial run
- **Reruns needed**: 2 full-suite reruns
- **Final result**: All tests pass, no OOM
- **Conclusion**: Transient memory allocation failure, no code change required
```

---

## How this fits into the main2main flow

The main2main flow's `_do_build_and_fix_loop` runs build → test → AI fix in a
loop. When test failures include NPU OOM:

1. The AI fix step should detect OOM in `test-logs/pytest.log`
2. Instead of generating code changes, the AI should signal "rerun tests"
3. The flow reruns the full test suite (same `-n` value)
4. Repeat until OOM is gone, then continue with normal fix logic for any
   remaining failures

If you are the AI agent reading this during a fix step: **do NOT modify any
source code for OOM failures**. Write `analysis.md` stating OOM was detected
and that tests should be rerun. The main2main flow will handle the rerun.
