# Triton-Ascend LLVM 版本升级适配与编译报错修复指导

## 概述

本文档记录了 Triton-Ascend 从 3.2.x（LLVM 20）升级到 3.5.x（LLVM 22）过程中，Ascend 后端代码和 AscendNPU-IR 适配 LLVM/MLIR API 变更的完整指南。

核心适配提交: `7222a734c6`（get→cast, is→isa）、`887cafd2e6`（match→matchAndRewrite, ToMemref→ToBuffer）、`f4d31cdf69`（getStridesAndOffset API）、`27c12c87a5`（ExtractSlice/InsertSlice 参数变更）。

---

## 1. LLVM/MLIR API 变更总览

### 1.1 关键 API 变更对照表

| 变更类别 | LLVM 20 (3.2.x) | LLVM 22 (3.5.x) | 影响范围 |
|---------|----------------|-----------------|---------|
| OpFoldResult 类型转换 | `ofr.get<Value>()` | `cast<Value>(ofr)` | 所有使用 OpFoldResult 的代码 |
| OpFoldResult 类型判断 | `ofr.is<Value>()` | `isa<Value>(ofr)` | 所有使用 OpFoldResult 的代码 |
| 自由函数替代成员函数 | `getStridesAndOffset(type)` | `type.getStridesAndOffset()` | MemRefType 相关代码 |
| Op 重命名 | `bufferization::ToMemrefOp` | `bufferization::ToBufferOp` | 所有 bufferization 使用处 |
| 模式重写方法名 | `match()` | `matchAndRewrite()` | 所有 Pattern 实现 |
| 零值判断 | `isZeroIndex()` | `isZeroInteger()` | 索引值判断 |
| Dialect 操作创建 | `create<Op>(..., extraArg)` | `create<Op>(..., extraArg, ...)` | 某些 Op 的 create 参数变化 |
| ExtractSlice/InsertSlice | 仅动态参数 | 动态 + 静态参数各一套 | IR 构建器调用 |
| 属性名变更 | `getLhs()/getRhs()` | `getA()/getB()` | DotScale 算子 |

---

## 2. OpFoldResult 类型转换 API 迁移

### 2.1 问题描述

LLVM 22 中移除了 `OpFoldResult` 的 `get<T>()` 和 `is<T>()` 方法，改用标准的 `cast<T>()` 和 `isa<T>()` 函数。

### 2.2 变更模式

```cpp
// ===== 模式 1: 获取 Value 类型 =====
// 旧代码 (LLVM 20)
operands.push_back(offset.get<Value>());
assert(isa<IndexType>(dataOffset.get<Value>().getType()));

// 新代码 (LLVM 22)
operands.push_back(cast<Value>(offset));
assert(isa<IndexType>(cast<Value>(dataOffset).getType()));

// ===== 模式 2: 获取 Attribute 类型 =====
// 旧代码 (LLVM 20)
auto constOffset = offset.get<Attribute>();
auto trueScalar = dyn_cast<IntegerAttr>(trueState.scalar.get<Attribute>());

// 新代码 (LLVM 22)
auto constOffset = cast<Attribute>(offset);
auto trueScalar = dyn_cast<IntegerAttr>(cast<Attribute>(trueState.scalar));

// ===== 模式 3: 类型判断 + 取值 =====
// 旧代码 (LLVM 20)
if (o.is<Value>()) {
    auto oVal = o.get<Value>();
}

// 新代码 (LLVM 22)
if (isa<Value>(o)) {
    auto oVal = cast<Value>(o);
}

// ===== 模式 4: template get 形式 =====
// 旧代码 (LLVM 20)
offsetVal = offset.template get<Value>();

// 新代码 (LLVM 22)
offsetVal = cast<Value>(offset);
```

### 2.3 受影响的文件列表

```bash
third_party/ascend/lib/TritonToLinalg/BlockPtrAnalysis.cpp
third_party/ascend/lib/TritonToLinalg/LoadStoreConverter.cpp
third_party/ascend/lib/TritonToLinalg/MaskAnalysis.cpp
third_party/ascend/lib/TritonToLinalg/TritonOpConverter.cpp
third_party/ascend/lib/TritonToUnstructure/BubbleUpOperation.cpp
third_party/ascend/lib/Utils/InterleaveOptimization.cpp
```

### 2.4 批量替换脚本

```bash
#!/bin/bash
# 批量替换 OpFoldResult API 调用
# 注意: 替换后需手动检查语义正确性

FILES=$(grep -rl "\.get<Value>\|\.get<Attribute>\|\.is<Value>" \
    third_party/ascend/lib/ --include="*.cpp" --include="*.h")

for f in $FILES; do
    # 替换 .get<Value>() → cast<Value>(...) 
    # 替换 .get<Attribute>() → cast<Attribute>(...)
    # 替换 .is<Value>() → isa<Value>(...)
    echo "Processing: $f"
done

echo "请手动检查每个替换的正确性！"
```

---

## 3. MemRefType::getStridesAndOffset API 迁移

### 3.1 问题描述

LLVM 22 将自由函数 `mlir::getStridesAndOffset(MemRefType)` 改为 `MemRefType` 的成员方法。

### 3.2 变更示例

```cpp
// 旧代码 (LLVM 20)
#include "mlir/IR/BuiltinTypes.h"
auto [ptrStrides, ptrOffsets] = getStridesAndOffset(memRefType);

// 新代码 (LLVM 22)
auto [ptrStrides, ptrOffsets] = memRefType.getStridesAndOffset();
```

### 3.3 受影响的文件

```cpp
// third_party/ascend/lib/TritonToLinalg/LoadStoreConverter.cpp
// 旧代码:
auto [ptrStrides, ptrOffsets] = getStridesAndOffset(memRefType);
auto [srcStrides, srcOffset] = getStridesAndOffset(dstSubViewType);

// 新代码:
auto [ptrStrides, ptrOffsets] = memRefType.getStridesAndOffset();
auto [srcStrides, srcOffset] = dstSubViewType.getStridesAndOffset();

// third_party/ascend/lib/Utils/Utils.cpp
// 旧代码:
auto [ptrStrides, ptrOffsets] = getStridesAndOffset(memRefType);

// 新代码:
auto [ptrStrides, ptrOffsets] = memRefType.getStridesAndOffset();
```

---

## 4. bufferization::ToMemrefOp → ToBufferOp 迁移

### 4.1 问题描述

LLVM 22 中将 `bufferization::ToMemrefOp` 重命名为 `bufferization::ToBufferOp`。

### 4.2 变更示例

```cpp
// 旧代码 (LLVM 20)
return rewriter.create<bufferization::ToMemrefOp>(loc, ptrType, inputVal);
memrefMask = rewriter.create<bufferization::ToMemrefOp>(loc, maskTypeM, mask);
Value inputMemref = rewriter.create<bufferization::ToMemrefOp>(loc, dstType, val);
Value cmpMemref = rewriter.create<bufferization::ToMemrefOp>(loc, dstType, cmp);

// 新代码 (LLVM 22)
return rewriter.create<bufferization::ToBufferOp>(loc, ptrType, inputVal);
memrefMask = rewriter.create<bufferization::ToBufferOp>(loc, maskTypeM, mask);
Value inputMemref = rewriter.create<bufferization::ToBufferOp>(loc, dstType, val);
Value cmpMemref = rewriter.create<bufferization::ToBufferOp>(loc, dstType, cmp);
```

### 4.3 受影响的文件

```bash
third_party/ascend/lib/TritonToLinalg/LoadStoreConverter.cpp
third_party/ascend/lib/TritonToLinalg/TritonOpConverter.cpp
```

---

## 5. 模式重写方法名迁移

### 5.1 问题描述

MLIR 将 Pattern 的 `match()` 方法重命名为 `matchAndRewrite()`，`rewrite()` 合并到 `matchAndRewrite()` 中。

### 5.2 变更示例

```cpp
// 旧代码 (LLVM 20)
class MyPattern : public OpRewritePattern<MyOp> {
    LogicalResult match(MyOp op) const override;
    void rewrite(MyOp op, PatternRewriter &rewriter) const override;
};

// 新代码 (LLVM 22)
class MyPattern : public OpRewritePattern<MyOp> {
    LogicalResult matchAndRewrite(MyOp op, PatternRewriter &rewriter) const override;
};
```

---

## 6. ExtractSlice/InsertSlice 参数变更

### 6.1 问题描述

LLVM 22 要求 `tensor::ExtractSliceOp` 和 `tensor::InsertSliceOp` 的 `create` 方法同时提供动态值和静态值参数。

### 6.2 变更示例

```cpp
// 旧代码 (LLVM 20)
return self.create<tensor::ExtractSliceOp>(retTy, ful, offsets, sizes, strides);
auto ret = self.create<tensor::InsertSliceOp>(sub, ful, offsets, sizes, strides);

// 新代码 (LLVM 22)
// 需要额外提供 staticOffsets, staticSizes, staticStrides
llvm::SmallVector<int64_t> staticOffsets;
llvm::SmallVector<int64_t> staticSizes;
llvm::SmallVector<int64_t> staticStrides;

for (const auto &o : offs_vec) {
    // ...填充 offsets...
    staticOffsets.push_back(ShapedType::kDynamic);
}
for (const auto &s : sizs_vec) {
    staticSizes.push_back(s);  // size 是静态的
}
for (const auto &s : strd_vec) {
    // ...填充 strides...
    staticStrides.push_back(ShapedType::kDynamic);
}

return self.create<tensor::ExtractSliceOp>(retTy, ful,
    offsets, sizes, strides,
    staticOffsets, staticSizes, staticStrides);

auto ret = self.create<tensor::InsertSliceOp>(sub, ful,
    offsets, sizes, strides,
    staticOffsets, staticSizes, staticStrides);
```

### 6.3 受影响的文件

```bash
third_party/ascend/triton_ascend.cc  # Python 绑定的 IR 构建器
```

---

## 7. LLVM 兼容性宏与 Patch 机制

### 7.1 CMakeLists.txt 中的 LLVM 版本宏

Triton-Ascend 在 CMakeLists.txt 中定义了 LLVM 版本兼容宏，传递给 AscendNPU-IR：

```cmake
# third_party/ascend/CMakeLists.txt (实际在顶层 CMakeLists.txt)
option(LLVM_MAJOR_VERSION_21_COMPATIBLE "NPUIR build with LLVM 21 or later" OFF)
if(LLVM_MAJOR_VERSION_21_COMPATIBLE)
  add_definitions(-D__LLVM_MAJOR_VERSION_21_COMPATIBLE__)
endif()

option(LLVM_MAJOR_VERSION_22_COMPATIBLE "NPUIR build with LLVM 22" OFF)
if(LLVM_MAJOR_VERSION_22_COMPATIBLE)
  add_definitions(-D__LLVM_MAJOR_VERSION_21_COMPATIBLE__)  # 22 也继承 21 的兼容
  add_definitions(-D__LLVM_MAJOR_VERSION_22_COMPATIBLE__)
endif()
```

### 7.2 LLVM Patch 机制

#### 7.2.1 fad3272.patch

位置: `third_party/ascend/llvm_patch/fad3272.patch`

作用: 为 MLIR 标准 Operations 添加 `useCustomPropertiesEncoding = 1` 属性，确保 BC 格式兼容。

```
修改的 Op 定义文件:
- mlir/include/mlir/Dialect/Arith/IR/ArithOps.td       (+ TruncIOp)
- mlir/include/mlir/Dialect/ControlFlow/IR/ControlFlowOps.td (+ CondBranchOp)
- mlir/include/mlir/Dialect/Func/IR/FuncOps.td          (+ CallOp, FuncOp)
- mlir/include/mlir/Dialect/LLVMIR/LLVMIntrinsicOps.td  (+ AssumeOp)
- mlir/include/mlir/Dialect/Linalg/IR/LinalgStructuredOps.td (+ MatmulOp, BatchMatmulOp)
- mlir/include/mlir/Dialect/SCF/IR/SCFOps.td            (+ ForOp, WhileOp, ...)
```

应用方式:
```bash
# LLVM 源码目录中应用 patch
cd llvm-project
git apply third_party/ascend/llvm_patch/fad3272.patch
```

#### 7.2.2 AscendNPU-IR LLVM Patches

AscendNPU-IR 子模块携带了大量 LLVM patch（约 75+ 个），位于:
```
third_party/ascend/AscendNPU-IR/build-tools/patches/llvm-project/
```

这些 patch 覆盖:
- **BishengIR 支持** (0001, 0002, 0050, 0051, 0065)
- **MLIR 增强** (0004-0016, 0019-0049, 0054-0064, 0067-0075)
- **Backport 修复** (0018, 0022, 0026, 0031, 0039, 0041, 0048, 0055-0058)
- **Bug 修复** (0066, 0070-0074)

### 7.3 LLVM Hash 更新

```bash
# 查看当前 LLVM 版本
git log --oneline -1 third_party/ascend/AscendNPU-IR/build-tools/

# NPUIR 子模块升级记录
# 3.2.x: commit 0501294d3e (LLVM 20)
# 3.5.x: commit 8c903bbfd4 (LLVM 22)
# 更新命令:
cd third_party/ascend/AscendNPU-IR
git fetch
git checkout <new-commit>
cd ../../..
git add third_party/ascend/AscendNPU-IR
git commit -m "feat: update AscendNPU-IR to <new-commit>"
```

---

## 8. 编译错误诊断流程

### 8.1 标准诊断步骤

```bash
# Step 1: 查看完整编译错误（前50行足够定位）
python setup.py build 2>&1 | head -100

# Step 2: 定位错误类型
#   - "no member named 'get'" → OpFoldResult API 变更
#   - "no member named 'getStridesAndOffset'" → 改为成员方法
#   - "unknown type name 'ToMemrefOp'" → 改为 ToBufferOp
#   - "too many arguments to function 'create'" → 参数列表变更
#   - "no member named 'match'" → matchAndRewrite 重命名
#   - "NameError: name '_builder'" → Python 参数重命名

# Step 3: 对比上游 Triton 对应文件的当前写法
git diff <upstream-3.2-tag> <upstream-3.5-tag> -- <报错文件>

# Step 4: 参考已有修复提交
git log --oneline --grep="fix.*compile\|fix.*build\|get to cast"
```

### 8.2 常见错误与修复速查表

| 编译错误信息 | 原因 | 修复方式 |
|------------|------|---------|
| `error: 'get' is not a member of 'mlir::OpFoldResult'` | `get<>()` 被移除 | 使用 `cast<>()` |
| `error: 'is' is not a member of 'mlir::OpFoldResult'` | `is<>()` 被移除 | 使用 `isa<>()` |
| `error: 'getStridesAndOffset' was not declared` | 自由函数变成员方法 | `type.getStridesAndOffset()` |
| `error: 'ToMemrefOp' is not a member of 'mlir::bufferization'` | Op 被重命名 | 改用 `ToBufferOp` |
| `error: no matching function for call to 'create'` | 参数数量变化 | 添加 static 参数 |
| `error: 'match' marked 'override' but does not override` | 方法名变更 | 改为 `matchAndRewrite` |
| `error: 'isZeroIndex' is not a member` | 函数重命名 | 改用 `isZeroInteger` |
| `NameError: name '_builder' is not defined` | Python 参数重命名 | 改用 `_semantic` |
| `error: 'class mlir::triton::DotScaledOp' has no member named 'getLhs'` | 属性重命名 | 改用 `getA()` |

---

## 9. 关键修复提交参考

### 9.1 提交索引

| 提交 | 说明 | 涉及文件数 |
|------|------|-----------|
| `7222a734c6` | get→cast, is→isa, DotScale属性适配 | 7 |
| `887cafd2e6` | match→matchAndRewrite, ToMemref→ToBuffer, create参数, isZeroIndex→isZeroInteger | 10 |
| `f4d31cdf69` | getStridesAndOffset API迁移 | 2 |
| `27c12c87a5` | ExtractSlice/InsertSlice static参数适配 | 1 |
| `dc4363f891` | 移除3.4中废弃的函数 | 4 |
| `8db52f00f7` | LLVM_MAJOR_VERSION_22_COMPATIBLE宏 | 4 |
| `3979a32572` | eraseOp/replaceOp API适配 | 1 |

### 9.2 查看具体修复的方法

```bash
# 查看某个修复提交的具体变更
git show 7222a734c6

# 查看某个文件在升级过程中的所有变更
git log --oneline 577a2d2b..783789c -- <file>

# 对比升级前后的文件
git diff 577a2d2b 6744f5ff3c -- <file>
```
