# Triton-Ascend 单元测试用例报错定位与修复指导

## 概述

本文档记录了 Triton-Ascend 从 3.2.x 升级到 3.5.x 过程中，运行单元测试时遇到的各类用例报错的定位方法和修复策略。升级后，2090 个功能回归测试中 1983 个通过，107 个跳过。

---

## 1. 测试架构概览

### 1.1 测试目录结构

```
third_party/ascend/unittest/
├── pytest_ut/          # Python 功能回归测试（主要测试目录）
├── device_ut/          # 需要真机设备的测试
├── autotune_ut/        # 自动调优测试
└── Conversion/General/ # MLIR lit 测试（FileCheck 格式）
    ├── TritonToLinalg/
    ├── TritonToUnstructure/
    └── TritonAscendAllPass/
```

### 1.2 升级后测试结果

来自合并提交 `6744f5ff3c` 的测试报告：
```
All 2090 existing functional regression tests:
  1983 passed
  107 skipped
  197 warnings
  in 496.80s
```

---

## 2. 测试失败分类与诊断方法

### 2.1 失败类型分类

| 失败类型 | 典型错误信息 | 出现频率 | 诊断难度 |
|---------|-------------|---------|---------|
| **API 签名不匹配** | `TypeError: unexpected keyword argument` | ⭐⭐⭐⭐ | 低 |
| **Op 语义变更** | 输出值不正确/NaN | ⭐⭐⭐ | 中 |
| **LLVM/MLIR API 变更** | 编译错误，测试跳过 | ⭐⭐⭐ | 中 |
| **Pass 选项变更** | FileCheck 匹配失败 | ⭐⭐ | 低 |
| **IR 格式变更** | `hacc.target` 属性变化 | ⭐⭐ | 低 |
| **设备兼容性** | NPU 特定功能不支持 | ⭐⭐ | 中 |
| **测试用例膨胀** | CI 超时 | ⭐ | 低 |
| **重复测试代码** | 测试运行两次 | ⭐ | 低 |
| **负 memref offset / test_neg_index** | `expected offsets to be non-negative` | ⭐⭐ | 中 |

### 2.2 标准诊断流程

```bash
# Step 1: 运行单个失败的测试
pytest third_party/ascend/unittest/pytest_ut/test_xxx.py::test_yyy -v

# Step 2: 根据错误类型定位
# TypeError / AttributeError → API 变更
# AssertionError (值不匹配) → Op 语义变更
# 编译错误 → LLVM/MLIR 变更
# FileCheck 失败 → Pass 选项或 Op 名称变更

# Step 3: 对比上游变更
git log --oneline <upstream-3.2>..<upstream-3.5> -- <相关文件>

# Step 4: 搜索已有修复
git log --oneline 577a2d2b..783789c --grep="fix.*test\|test.*fix" -- <测试文件>
```

---

## 3. 典型失败案例详解

### 3.1 案例 1: `tl.load` 缺少 `other` 参数导致掩码区域值不正确

**错误表现:**
```
AssertionError: Tensor values not close
# masked load 返回的元素值不正确（垃圾值/NaN）
```

**根因分析:**
上游 3.5.x 移除了 Ascend 自定义的 `care_padding` 参数（提交 `0de0db7b1d`），该参数原本会自动为 `tl.load` 的掩码区域填充 `0.0`。移除后，使用 `mask=` 但不指定 `other=` 的 `tl.load` 调用会导致被掩码区域的元素值未定义。

**定位步骤:**
1. 查看失败的测试用例，找到使用 `tl.load(mask=...)` 的位置
2. 确认该调用没有指定 `other` 参数
3. 搜索相关 API 变更：`git log --grep="care_padding\|other"`

**修复方案:**
在所有带 `mask` 的 `tl.load` 调用中显式添加 `other=0.0`：

```python
# 修复前
tmp = tl.load(cache_ptr1 + offset, mask=(index < limit))

# 修复后
tmp = tl.load(cache_ptr1 + offset, mask=(index < limit), other=0.0)
```

**相关提交:**
- `a0097de5d6` — `[Test](fix) Add explicit other=0.0 to masked load`

---

### 3.2 案例 2: Pass 选项 `force-simt-template` 废弃导致 MLIR 测试失败

**错误表现:**
```
error: 'force-simt-template' is not a valid option for pass 'triton-to-linalg'
# MLIR lit 测试中的 RUN 行解析失败
```

**根因分析:**
上游 3.5.x 将多个 Pass 的 `force-simt-template` (bool) 选项改为 `compile-mode` (string) 选项。旧测试文件的 RUN 行仍使用旧选项名。

**修复方案:**

**Step 1:** 添加向后兼容的 Pass 选项定义
```cpp
// Passes.td 中添加 deprecated alias
Option<"forceSimtTemplate", "force-simt-template",
    "bool", /*default*/"false",
    "Deprecated alias for compile-mode=simt_template">,
```

**Step 2:** 添加兼容解析函数
```cpp
// Utils.h
inline CompileMode resolveCompileMode(llvm::StringRef mode,
                                      bool forceSimtTemplate) {
  return forceSimtTemplate ? CompileMode::SimtTemplate : parseCompileMode(mode);
}
```

**Step 3:** 更新 MLIR 测试文件的 RUN 行
```
# 修复前:
// RUN: triton-opt '--triton-to-unstructure=force-simt-template=True' ...

# 修复后:
// RUN: triton-opt '--triton-to-unstructure=compile-mode=simt_template' ...
```

**Step 4:** 更新 CHECK 行以匹配新的 Op 名称
```
# 修复前:
// CHECK: ascend.indirect_load

# 修复后:
// CHECK: ascend.unstructured_load
```

**相关提交:**
- `ac95260552` — `fix: add compatible option for passes and update mlir test cases`

---

### 3.3 案例 3: 自定义算子回退到社区版本导致测试失败

**错误表现:**
```
TypeError: cdiv() got an unexpected keyword argument
# 或
AssertionError: cdiv output mismatch (divergent implementations)
```

**根因分析:**
Ascend 3.2.x 对某些 Triton 标准算子有自定义实现（如 `cdiv`, `reduce`, `fast_math`），但上游 3.5.x 对这些算子的接口和实现进行了重构。Ascend 的自定义实现与新接口不兼容。

**诊断方法:**
```bash
# 搜索所有 Ascend 自定义的算子回退
git log --oneline --grep="revert\|Revert\|rollback\|Rollback"
git log --oneline --grep="align with upstream\|align.*upstream"
```

**修复策略:**

| 算子 | 修复方式 | 相关提交 |
|-----|---------|---------|
| `cdiv` | 删除 Ascend 自定义实现，恢复上游版本 | `3883ffaef2` |
| `reduce` | 回退 `builtins.tuple` → `tuple` | `a391e744cd` |
| `fast_math` | 默认值回退到 `False` | `b39d167dad` |
| `gluon.cdiv` | 恢复 Gluon 的 `cdiv` 导入路径 | `3a821cf4f5` |

---

### 3.4 案例 4: NPUIR 更新导致部分测试不支持

**错误表现:**
```
# 各种运行时错误：编译失败、输出不匹配、断言失败
```

**根因分析:**
AscendNPU-IR 子模块更新到新版本（如 `0501294d` → `8c903bbf`），底层编译器（bishengir-compile）的输出格式或支持的 IR 模式发生变化，导致部分测试场景暂时不支持。

**修复方案:**
暂时跳过受影响的测试，并标记 TODO：

```python
@pytest.mark.skip(
    reason="not supported after the NPUIR is updated in April, and will be fixed later"
)
def test_affected_case():
    ...
```

**受影响的测试类别:**
- `test_compile_hint.py` — 编译提示功能
- `test_discrete_mask_loadstore.py` — 离散掩码加载/存储
- `test_dot.py` — 点积运算（特定数据类型组合）
- `test_parallel.py` — 并行执行

**后续跟进:**
1. 在 NPUIR 适配完成后取消 skip
2. 如果需要，调整测试参数使其兼容新的 NPUIR

**相关提交:**
- `b77d79bd97` — `ci(update):Skip some test cases not supported after the NPUIR is updated`

---

### 3.5 案例 5: TestCase 参数膨胀导致 CI 超时

**错误表现:**
```
CI job timeout (exceeded 30 minutes)
# 或显式的 out-of-memory 错误
```

**根因分析:**
某些测试的参数空间太大（多个 dtype × 多个 shape × 多个配置），导致编译或运行时间过长。

**修复方案:**
大幅度精简参数组合：

```python
# 修复前: 7 种 dtype × 5 种 shape × 4 种 block_size
dtypes = ['float32', 'float16', 'bfloat16', 'int32', 'int64', 'int16', 'int8']
shapes = [(2, 4), (4, 8), (8, 16), (16, 32), (32, 64)]

# 修复后: 2 种 dtype × 2 种 shape
dtypes = ['float32', 'int32']
shapes = [(4, 8), (16, 32)]
```

**精简原则:**
1. 保留最有代表性的 dtype（float32, int32）
2. 保留边界 shape（最小和最大）
3. 如果某个 shape 组合在 CI 中已验证过，可以删除

**相关提交:**
- `96a96db4c8` — `[UT](test) reduce test cases for tensor_descriptor`
- `1d21085a19` — `[UT](fix) remove large case in test_load/load_store`

---

### 3.6 案例 6: 测试辅助函数缺失

**错误表现:**
```
ImportError: cannot import name 'generate_numpy' from 'test_common'
ModuleNotFoundError: No module named 'test_common'
```

**根因分析:**
测试基础设施在升级过程中重组，某些目录缺少 `test_common.py` 辅助文件。

**修复方案:**
为缺失目录创建 `test_common.py`：
```python
# third_party/ascend/unittest/device_ut/test_common.py
import torch
import numpy as np

def generate_numpy(shape, dtype):
    """Generate test data for a given shape and dtype."""
    if 'float' in str(dtype):
        return np.random.randn(*shape).astype(str(dtype))
    elif 'int' in str(dtype):
        return np.random.randint(-10, 10, shape).astype(str(dtype))
    ...
```

**相关提交:**
- `b89098c461` — `[UT](fix) add test_common for device_ut`

---

### 3.7 案例 7: 重复测试代码

**错误表现:**
```
# 同一个测试逻辑在两个文件中运行
# tests/test_01_vector_add.py 和 tutorials/01-vector-add.py
```

**根因分析:**
Tutorial 文件中内嵌了测试代码，同时又存在独立的 `pytest_ut/test_0*_*.py` 测试文件，导致测试重复运行和维护负担。

**修复方案:**
将测试逻辑统一到 tutorial 文件中，删除独立的测试文件：

```python
# tutorials/01-vector-add.py
if __name__ == "__main__":
    def test_vector_add():
        # ...测试逻辑...
        torch.testing.assert_close(output, expected)

    test_vector_add()
```

**相关提交:**
- `a6ee6dbd09` — `[Tutorials](fix) Remove duplicate test code from UT`（删除 1889 行重复代码）

---

### 3.8 案例 8: 负 base offset 连续 load（`test_neg_index`）在 LLVM 3.7+ 失败

**错误表现:**
```text
error: expected offsets to be non-negative, but got -6
Pipeline failed while executing
  [`TritonToLinalg` on 'builtin.module', `Canonicalizer` on 'builtin.module']
```
或 `pytest_ut/test_neg_index.py` 编译/数值失败。

典型 kernel：
```python
tmp = tl.load(in_ptr + ((-NEG_INDEX) + offset), mask=(offset >= NEG_INDEX), other=0.0)
# NEG=6, BLOCK=12 → 有效 lane 读 in[0..5] 写到 out[6..11]
```

**根因分析（勿判成 “verifier 变严”）:**

1. 旧 `NegOffsetElim` 把负 Attribute 改成 `%c-6 : index`（仍是负常量）。
2. LLVM 3.7+ 新增 `ReinterpretCastOpConstantFolder`（[#163505](https://github.com/llvm/llvm-project/pull/163505)），在 `TritonToLinalg` 内嵌 Canonicalizer 里把 `%c-6` 折回 `staticOffsets=[-6]`。
3. `ViewLikeInterface` 一直拒绝负 **static** offset（3.6/3.7 规则相同）。3.6 能过是因为没有 ConstantFolder。

**正确修复（pointer rebase）:**

文件：`third_party/ascend/lib/TritonToLinalg/BlockPtrAnalysis.cpp`
函数：`BlockDataParser::rewriteAddPtr`（IntToPtr→`pointer_cast` 之后、`createCastOp` 之前）

```text
linear = Σ getConstantIntValue(offsets[i])   # 有动态维则跳过
if linear 存在 && linear < 0:
    指针前进 linear 个元素（一次，不是按维循环）
    所有 offsets = 0
    再 createCastOp → reinterpret_cast offset:[0]
```

地址前进：

| 分支 | 条件 | 做法 |
|---|---|---|
| A | source 已是 `hivm.pointer_cast` | `addrs[0] + linear * elemBytes`（i64） |
| B | 普通 memref（函数参） | `extract_aligned_pointer_as_index` → 字节加减 → `index_cast` → i64 |

然后：`hivm.hir.pointer_cast(%addr) : memref<?xT>`（动态 `?` 基址；定长 tile 由后续 `reinterpret_cast sizes` 给出）。

等价：`base + (i + S)`（`S<0`）≡ `(base advanced by S elems) + i`。
有加有减但 **总和 ≥ 0**：**不要改**。

**禁止的错误修复:**

```cpp
// ❌ maxsi 夹紧 —— 能过 verifier，但语义错（NEG=6 时 out[6]=in[6] 而非 in[0]）
retOffset = maxsi(constant(-6), constant(0));  // → 0

// ❌ 仅 Attribute → %c-N（NegOffsetElim）—— 3.7 ConstantFolder 再折回负 static

// ❌ 负 reinterpret_cast / subview offset —— 同样撞 verifier
```

| | maxsi 夹 0 | rebase |
|---|---|---|
| verifier | 过 | 过 |
| `out[6]`（NEG=6） | `in[6]`（错） | `in[0]`（对） |

**回归测试:**

- `pytest_ut/test_neg_index.py::test_neg_index_load`
- `test_mixed_static_index_load`：`(8,2)` 和负 / `(4,10)` 和不负
- lit：`Conversion/General/TritonToLinalg/test_load_with_neg_index.mlir`（函数参 / int_to_ptr / zero-offset）

数值参考：`out[index:] = in[:n - index]`。

---

## 4. 升级后常见测试问题检查清单

### 4.1 API 变更检查

```bash
# 检查是否使用了已移除的 Ascend 私有参数
grep -r "care_padding" third_party/ascend/unittest/
grep -r "overflow_mode" third_party/ascend/unittest/

# 检查函数名变更
grep -r "generate_npu_wrapper_src" third_party/ascend/unittest/
grep -r "_builder" third_party/ascend/unittest/
```

### 4.2 Pass 选项检查

```bash
# 检查 MLIR 测试是否使用了旧选项名
grep -r "force-simt-template" third_party/ascend/unittest/Conversion/

# 期望的选项名
grep -r "compile-mode" third_party/ascend/unittest/Conversion/
```

### 4.3 Op 名称检查

```bash
# 检查是否使用了旧的 Op 名称
grep -r "indirect_load\|indirect_store" third_party/ascend/unittest/Conversion/

# 期望的新名称
grep -r "unstructured_load\|unstructured_store" third_party/ascend/unittest/Conversion/
```

### 4.4 设备特定测试检查

```bash
# 确认使用正确设备装饰器
grep -r "@simd_simt_910_95_only\|is_compile_on_910_95" \
    third_party/ascend/unittest/pytest_ut/
```

---

## 5. 添加新测试的最佳实践

### 5.1 测试文件结构

```
third_party/ascend/unittest/pytest_ut/
├── test_common.py           # 测试工具函数
├── conftest.py              # pytest 配置和 fixtures
├── test_<feature_name>.py   # 功能测试文件
└── ...
```

### 5.2 跳过测试的规范格式

```python
import pytest

# 临时跳过（需要后续修复）
@pytest.mark.skip(reason="not supported after NPUIR update, will fix later")

# 条件跳过（特定平台不支持）
@pytest.mark.skipif(not is_compile_on_910_95, reason="only support A5 platform")

# 预期失败（已知 bug）
@pytest.mark.xfail(reason="known issue: output precision mismatch")
```

### 5.3 测试参数化

```python
# 避免参数爆炸
# 好的做法：选择有代表性的参数
@pytest.mark.parametrize("dtype", ['float32', 'int32'])  # 2 个，不是 7 个
@pytest.mark.parametrize("shape", [(4, 8), (16, 32)])    # 2 个，不是 5 个
def test_my_op(dtype, shape):
    ...
```

---

## 6. 测试调试工具与技巧

### 6.1 启用 IR 转储

```python
# 在测试中启用 debug 模式
import os
os.environ['TRITON_DEBUG'] = '1'
# 这将输出完整的编译流程和 IR 文件
```

### 6.2 对比上游行为

```python
# 当不确定行为是否正确时，对比 CPU reference
import torch

def test_with_reference():
    # Ascend 结果
    ascend_result = run_on_ascend(kernel, inputs)
    # CPU reference
    cpu_result = run_cpu_reference(kernel, inputs)
    # 对比
    torch.testing.assert_close(ascend_result, cpu_result, rtol=1e-3, atol=1e-5)
```

### 6.3 使用 triton-mlir-opt 调试

```bash
# 导出 MLIR 中间表示
TRITON_DEBUG=1 pytest test_xxx.py -v

# 手动运行 MLIR Pass 管线
triton-opt kernel.ttir \
    --triton-to-structured \
    --discrete-mask-access-conversion=compile-mode=simd \
    --triton-to-unstructure=compile-mode=simd \
    --triton-to-linalg=compile-on-910-95=true

# 验证 BC 格式
triton-mlir-opt kernel.ttadapter.mlir --emit-bytecode -o kernel.mlirbc
```
