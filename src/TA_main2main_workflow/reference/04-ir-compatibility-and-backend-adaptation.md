# Triton-Ascend 升级后 IR 兼容性问题修复指导

## 概述

本文档记录了 Triton-Ascend 从 3.2.x 升级到 3.5.x 过程中，由于上游 Triton IR 格式变更、LLVM/MLIR 版本升级以及 AscendNPU-IR 子模块更新带来的 IR 兼容性问题及其修复方案。

核心技术挑战：TritonAscend（基于 LLVM 22）与 AscendNPU-IR（基于 BishengIR/LLVM）之间通过 MLIR 文本传递 IR 时的跨版本兼容性问题。

---

## 1. 跨版本 IR 兼容性问题的根源

### 1.1 问题背景

Triton-Ascend 的编译管线涉及两个不同 LLVM 版本的组件：

```
Triton-Ascend (LLVM 22)
    ↓ 生成 MLIR 文本
AscendNPU-IR / BishengIR (不同 LLVM 版本)
    ↓ 编译 MLIR → 二进制
NPU 硬件执行
```

当 Triton-Ascend 生成的 MLIR 文本包含新 LLVM 版本的 IR 特性时，旧版本的 BishengIR 可能无法正确解析。

### 1.2 解决方案：Bytecode (BC) 编译管线

引入 BC（MLIR Bytecode）作为中间格式，替代直接传递 MLIR 文本：

```
旧方案（3.2.x）:
  Linalg IR (文本) → LLIR (文本) → Binary

新方案（3.5.x, use_bytecode=True）:
  Linalg IR (文本) → [triton-mlir-opt] → MLIR Bytecode → [bishengir-opt] → LLIR (文本) → Binary
```

BC 格式是 LLVM/MLIR 的二进制序列化格式，具有更好的跨版本兼容性。

---

## 2. 新增工具与基础设施

### 2.1 triton-mlir-opt

**文件:** `bin/triton-mlir-opt.cpp`

**功能:** 将 Triton-Ascend 的 MLIR 文本转为 BC 格式。

```cpp
#include "./RegisterTritonDialects.h"
#include "bishengir/InitAllDialects.h"
#include "mlir/Tools/mlir-opt/MlirOptMain.h"

int main(int argc, char **argv) {
  mlir::DialectRegistry registry;
  mlir::registerAllDialects(registry);      // 标准 MLIR dialects
  bishengir::registerAllDialects(registry);  // BishengIR dialects
  return mlir::asMainReturnCode(
      mlir::MlirOptMain(argc, argv, "Triton-Ascend optimizer driver\n", registry));
}
```

### 2.2 BC 编译流程函数

```python
# third_party/ascend/backend/compiler.py

def linalg_to_bc_by_triton_mlir_opt(linalg: str, metadata, opt):
    """Linalg IR (文本) → MLIR Bytecode"""
    subprocess.check_call([
        triton_mlir_opt_path,
        ttadapter_path,
        "--emit-bytecode",
        "-o", bc_path,
    ])
    return bc_data  # bytes

def bc_to_linalg_by_bishengir_opt(bc_data: bytes, metadata, opt):
    """MLIR Bytecode → LLIR (文本)"""
    subprocess.check_call([
        bishengir_opt_path,
        bc_path,
        "-o", mlir_path,
    ])
    return linalg_text  # str
```

### 2.3 NPUOptions 配置

```python
@dataclass
class NPUOptions:
    # ...
    use_bytecode: bool = True  # 默认启用 BC 模式
    # True: Linalg IR → BC (triton-mlir-opt) → LLIR (bishengir-opt) → Binary
    # False: Linalg IR → LLIR → Binary (直接)
```

---

## 3. Op 名称与 IR 结构变更

### 3.1 indirect_load → unstructured_load / indirect_store → unstructured_store

**变更原因:** 上游 Triton 3.5 统一了非结构化内存访问的 Op 命名。

**IR 变更示例:**
```
# 旧 IR (3.2.x):
%result = ascend.indirect_load %ptr, %indices : <f32>, tensor<8x32xi64> -> tensor<8x32xf32>
ascend.indirect_store %ptr, %indices, %data : <f32>, tensor<8x32xi64>, tensor<8x32xf32>

# 新 IR (3.5.x):
%result = ascend.unstructured_load %ptr, %indices : <f32>, tensor<8x32xi64> 
          unstructured_dims = [0, 1] -> tensor<8x32xf32>
ascend.unstructured_store %ptr, %indices, %data : <f32>, tensor<8x32xi64>, tensor<8x32xf32>
          unstructured_dims = [0, 1]
```

**修复方式:**
1. C++ 代码中重命名 Op 类定义
2. MLIR lit 测试中更新 CHECK 行
3. Pass 管线中的引用更新

### 3.2 Pass 选项格式统一

**变更:** `force-simt-template` (bool) → `compile-mode` (string)

```
# 旧格式:
--triton-to-unstructure=compile-on-910-95=True force-simt-template=True

# 新格式:
--triton-to-unstructure=compile-on-910-95=True compile-mode=simt_template
```

**支持的 compile-mode 值:**
- `simd` — 默认，标准 SIMD 模式
- `simd_simt` — 混合 SIMD/SIMT 模式
- `simt_template` — SIMT 模板模式

### 3.3 DotScale Op 属性重命名

**IR 变更:**
```
# 旧 IR:
tt.dot_scaled %lhs, %lhs_scale, %rhs, %rhs_scale, %c

# 新 IR:
tt.dot_scaled %a, %a_scale, %b, %b_scale, %c
```

**C++ 适配器属性变更:**
```cpp
// 旧: adaptor.getLhs(), adaptor.getLhsScale(), adaptor.getRhs(), adaptor.getRhsScale()
// 新: adaptor.getA(), adaptor.getAScale(), adaptor.getB(), adaptor.getBScale()
```

---

## 4. AscendNPU-IR 子模块更新

### 4.1 子模块版本变更

| 阶段 | Commit | LLVM 版本 |
|------|--------|----------|
| 3.2.x | `0501294d3e` | LLVM 20 |
| 3.3.x | `f96183dede` | LLVM 21 |
| 3.5.x | `8c903bbfd4` | LLVM 22 |

### 4.2 子模块更新流程

```bash
# 1. 更新子模块
cd third_party/ascend/AscendNPU-IR
git fetch origin
git checkout <target-commit>
cd ../../..

# 2. 验证编译
python setup.py build 2>&1 | tee build.log

# 3. 运行测试
pytest third_party/ascend/unittest/pytest_ut/ -x

# 4. 处理测试失败
# 暂时不支持的测试标记 skip
# 需要适配的代码进行修改

# 5. 提交
git add third_party/ascend/AscendNPU-IR
git commit -m "feat: update AscendNPU-IR to <target-commit>"
```

### 4.3 NPUIR 更新后的典型问题

1. **编译器输出格式变化** — bishengir-compile 的输出格式改变，影响解析逻辑
2. **新增 Pass 或移除 Pass** — 编译管线需要调整
3. **Op 支持矩阵变化** — 部分 Op 组合不再支持
4. **性能变化** — 编译优化策略改变导致测试精度变化

---

## 5. TritonParse IR 兼容性

### 5.1 mix_mode 独立模块化

**问题:** TritonParse 工具生成的 kernel 复现脚本中，`mix_mode` 被编码到 kernel 名称中，导致：
1. 脚本路径包含空格时执行失败
2. import 路径包含空格
3. kernel_name 不正确，复现脚本无法导入真正的 kernel

**修复 (提交 `33eff2d37e`):**
将 `mix_mode` 作为独立参数传递，不再编码到 kernel 名称中：

```python
# compiler.py (旧)
load_binary(self.name, ...)
# 其中 self.name = kernel_name + "_" + mix_mode

# compiler.py (新)
load_binary(self.metadata.kernel_name, ..., self.metadata.mix_mode)
# kernel_name 和 mix_mode 分开传递

# driver.py (旧)
name, mix_mode = name.rsplit("_", 1)
# driver.py (新)
# 直接从 metadata 中获取 mix_mode
```

### 5.2 NPUOptions 字段优化

**变更 (提交 `2ddde63e97`):**

移除的字段：
- `num_buffers_warp_spec`
- `num_consumer_groups`
- `reg_dec_producer`
- `reg_inc_consumer`

新增字段：
- `ir_override: Optional[str] = None` — 允许用户覆盖 IR 阶段

### 5.3 ir_override 功能

允许在编译时跳过某些 IR 阶段，使用预先准备好的 IR 文件：

```python
@dataclass
class NPUOptions:
    ir_override: Optional[str] = None  # 覆盖 IR 文件的路径前缀
    
# 使用方式:
# 如果 ir_override = "/path/to/kernel"
# 则会查找:
#   /path/to/kernel.ttadapter.mlir  (Adapter IR)
#   /path/to/kernel.bcmlir          (BC MLIR)
#   /path/to/kernel.npubin          (NPU Binary)
```

---

## 6. LLVM Patch 机制与 IR 兼容性

### 6.1 fad3272 / f6ded0b patch

**位置:** `third_party/ascend/patch/llvm_patch_f6ded0b.patch`

**核心修改:** 为 MLIR 标准 Op 添加 `useCustomPropertiesEncoding = 1`：

```tablegen
// ArithOps.td
def Arith_TruncIOp : Arith_Op<"trunci", ...> {
  let useCustomPropertiesEncoding = 1;  // 新增
}

// ControlFlowOps.td
def CondBranchOp : ... {
  let useCustomPropertiesEncoding = 1;  // 新增
}

// FuncOps.td
def CallOp : Func_Op<"call", ...> {
  let useCustomPropertiesEncoding = 1;  // 新增
}
def FuncOp : Func_Op<"func", ...> {
  let useCustomPropertiesEncoding = 1;  // 新增
}

// LLVMIntrinsicOps.td
def LLVM_AssumeOp : ... {
  let useCustomPropertiesEncoding = 1;  // 新增
}

// LinalgStructuredOps.td
def MatmulOp : ... {
  let useCustomPropertiesEncoding = 1;  // 新增
}
def BatchMatmulOp : ... {
  let useCustomPropertiesEncoding = 1;  // 新增
}
```

**为什么需要:** BC 格式要求 Op 使用 `useCustomPropertiesEncoding` 来确保属性的序列化/反序列化与 BC 格式兼容。

### 6.2 LLVM 兼容宏体系

```cmake
# 层级继承关系:
LLVM_MAJOR_VERSION_22_COMPATIBLE=ON  →  同时启用:
  __LLVM_MAJOR_VERSION_22_COMPATIBLE__  (LLVM 22 特有变更)
  __LLVM_MAJOR_VERSION_21_COMPATIBLE__  (LLVM 21+ 变更，22 也需要)

LLVM_MAJOR_VERSION_21_COMPATIBLE=ON  →  启用:
  __LLVM_MAJOR_VERSION_21_COMPATIBLE__  (LLVM 21+ 变更)

LLVM_MAJOR_VERSION_20_COMPATIBLE=ON  →  启用:
  __LLVM_MAJOR_VERSION_20_COMPATIBLE__  (LLVM 20+ 变更)
```

**使用示例（AscendNPU-IR 中）:**
```cpp
// bishengir/tools/bishengir-target-spec-tblgen/TargetSpecGen.cpp

#if defined(__LLVM_MAJOR_VERSION_22_COMPATIBLE__)
  auto superClasses = derivedClassRecord->getDirectSuperClasses();
  const Record *superClass = superClasses.front().first;
#elif defined(__LLVM_MAJOR_VERSION_21_COMPATIBLE__)
  SmallVector<const Record *> superClasses;
  derivedClassRecord->getDirectSuperClasses(superClasses);
  const Record *superClass = superClasses.front();
#else
  SmallVector<Record *> superClasses;
  derivedClassRecord->getDirectSuperClasses(superClasses);
  Record *superClass = superClasses.front();
#endif
```

---

## 7. Ascend 特有 API 的移除与适配

### 7.1 已移除的 Ascend 私有参数

| 参数/功能 | 所在接口 | 移除原因 | 相关提交 |
|----------|---------|---------|---------|
| `care_padding` | `tl.load()` | 上游无此参数，维护成本高 | `0de0db7b1d` |
| `overflow_mode` | `tl.cast()`, `tensor.to()` | 上游不支持 saturate 模式 | `478f80cf45` |
| `other` 自动填充 | `_load_block_pointer` | 上游 load 语义变更 | `0e282ab3f5` |
| `fast_math=True` | `dot_scaled()` | Ascend NPU 不支持 | `b39d167dad` |
| fp8 builder 方法 | `ir.cc` | LLVM 22 API 变更 | `521e1328af` |

### 7.2 API 变更适配对照

```python
# ===== tl.load 变更 =====
# 3.2.x (Ascend 扩展):
tl.load(ptr, mask=mask, care_padding=True)  # care_padding 自动填充 0

# 3.5.x (标准):
tl.load(ptr, mask=mask, other=0.0)  # 显式指定 other

# ===== tl.cast 变更 =====
# 3.2.x (Ascend 扩展):
tl.cast(x, dtype, overflow_mode="saturate")

# 3.5.x (标准):
tl.cast(x, dtype)  # overflow_mode 移除

# ===== dot_scaled 变更 =====
# 3.2.x (Ascend 默认):
tl.dot_scaled(a, a_scale, b, b_scale, c, fast_math=False)

# 3.5.x (上游默认 fast_math=True, Ascend 回退为 False):
tl.dot_scaled(a, a_scale, b, b_scale, c, fast_math=False)
```

---

## 8. 上游 Triton IR 重大变更速查

### 8.1 [BC-breaking] 标记的变更

在升级过程中，需要特别关注上游标记为 `[BC-breaking]` 的变更：

| 上游 PR | 变更内容 | 对 Ascend 的影响 |
|---------|---------|-----------------|
| `expand_dims → reshape` | `ExpandDimsOp` 被 `ReshapeOp` 替代 | 所有使用 expand_dims 的 Pass 需要适配 |
| `ToMemrefOp → ToBufferOp` | Op 重命名 | 所有 bufferization 调用处需要更新 |
| `experimental descriptor APIs` | 移除 experimental 前缀 | 描述符 API 名称变更 |
| `global variables as constexpr` | `tl.constexpr` 要求 | kernel 全局变量的声明方式变更 |

### 8.2 IR Dialect 注册变更

```cpp
// 3.5.x 需要额外注册的 Dialect
// bin/RegisterTritonDialects.h
#include "triton/Dialect/TritonGPU/IR/Dialect.h"
#include "triton/Dialect/TritonNvidiaGPU/IR/Dialect.h"
#include "triton/Dialect/NVWS/IR/Dialect.h"

// Ascend 后端需要额外注册
#include "ascend/include/Dialect/TritonAscend/IR/TritonAscendDialect.h"
#include "bishengir/InitAllDialects.h"
```

---

## 9. IR 兼容性问题诊断流程

### 9.1 标准诊断步骤

```bash
# Step 1: 导出编译流程中的各阶段 IR
TRITON_DEBUG=1 python your_script.py 2>&1 | grep "IR dump"

# Step 2: 检查 MLIR 文本是否可以正确解析
triton-opt kernel.ttir --verify-diagnostics

# Step 3: 测试 BC 转换是否正常
triton-mlir-opt kernel.ttadapter.mlir --emit-bytecode -o kernel.mlirbc
bishengir-opt kernel.mlirbc -o kernel.mlir

# Step 4: 对比正常和异常的 IR
diff working_kernel.ttir broken_kernel.ttir
```

### 9.2 常见 IR 错误及解决

| 错误信息 | 原因 | 解决方案 |
|---------|------|---------|
| `error: unexpected op 'ascend.indirect_load'` | Op 名称变更 | 更新为新 Op 名称 |
| `error: 'force-simt-template' is not a valid option` | Pass 选项变更 | 使用 `compile-mode=simt_template` |
| `error: bytecode parse error` | BC 格式不兼容 | 检查 LLVM patch 是否应用 |
| `error: custom op not registered` | Dialect 未注册 | 检查 RegisterTritonDialects |
| `error: type mismatch in 'tt.dot_scaled'` | 属性名变更 | 适配 `getA()/getB()` |
| `hacc.target attribute missing` | 硬件目标信息缺失 | 确保编译流程中有 set_hacc_target |
| `undefined symbol: mlir::...` | 链接库缺失 | 检查 CMakeLists.txt 链接依赖 |

---

## 10. 预防性措施

### 10.1 升级前检查

```bash
# 1. 对比上游 IR 格式变更
git diff <upstream-old> <upstream-new> -- \
    "*.td" "*.h" "*.cpp" -- "**/IR/**"

# 2. 检查 AscendNPU-IR 兼容性
cd third_party/ascend/AscendNPU-IR
git log --oneline <old-commit>..<new-commit> | wc -l

# 3. 识别受影响的 Pass
grep -r "registerPass\|addPass" third_party/ascend/lib/ --include="*.cpp"
```

### 10.2 升级中监控

```bash
# 1. 编译验证
python setup.py build 2>&1 | tee build.log
grep -E "error:|warning:" build.log | sort -u

# 2. MLIR lit 测试
cd third_party/ascend/unittest/Conversion/
python -m lit . -v 2>&1 | tee lit.log

# 3. Python 测试
pytest third_party/ascend/unittest/pytest_ut/ -x --tb=short 2>&1 | tee pytest.log
```

### 10.3 BC 格式回退

如果 BC 格式出现问题，可以临时禁用：

```python
# third_party/ascend/backend/compiler.py
class NPUOptions:
    use_bytecode: bool = False  # 回退到直接文本传递模式
```
