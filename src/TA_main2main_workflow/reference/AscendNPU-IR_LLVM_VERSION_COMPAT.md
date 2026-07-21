# AscendNPU-IR LLVM 版本兼容性适配指南

本文档整理了 AscendNPU-IR（bishengir）为适配 LLVM 20/21/22 版本变更所做的全部兼容性适配，供后续版本升级参考。

---

## 1. 编译系统：版本标志定义

**文件:** `CMakeLists.txt:17-36`

```cmake
option(LLVM_MAJOR_VERSION_20_COMPATIBLE "NPUIR build with LLVM 20" OFF)
if(LLVM_MAJOR_VERSION_20_COMPATIBLE)
  add_definitions(-D__LLVM_MAJOR_VERSION_20_COMPATIBLE__)
endif()

option(LLVM_MAJOR_VERSION_21_COMPATIBLE "NPUIR build with LLVM 21 or later" OFF)
if(LLVM_MAJOR_VERSION_21_COMPATIBLE)
  add_definitions(-D__LLVM_MAJOR_VERSION_21_COMPATIBLE__)
endif()

option(LLVM_MAJOR_VERSION_22_COMPATIBLE "NPUIR build with LLVM 22" OFF)
if(LLVM_MAJOR_VERSION_22_COMPATIBLE)
  add_definitions(-D__LLVM_MAJOR_VERSION_21_COMPATIBLE__)  # LLVM 22 也兼容 21 的变更
  add_definitions(-D__LLVM_MAJOR_VERSION_22_COMPATIBLE__)
endif()
```

**规则：** LLVM 22 同时启用 21 和 22 的标志，因为 21 的兼容性变更在 22 中仍需保留。

**子模块 TableGen 宏传递：**

`bishengir/include/bishengir/Dialect/HACC/IR/CMakeLists.txt:9-11`：
```cmake
if(LLVM_MAJOR_VERSION_21_COMPATIBLE)
  list(APPEND tblgen_feat_list -D__LLVM_MAJOR_VERSION_21_COMPATIBLE__)
endif()
```

`bishengir/include/bishengir/Dialect/HFusion/IR/CMakeLists.txt:6-11`：
```cmake
if(LLVM_MAJOR_VERSION_21_COMPATIBLE)
  list(APPEND tblgen_feat_list -D__LLVM_MAJOR_VERSION_21_COMPATIBLE__)
endif()
if(LLVM_MAJOR_VERSION_22_COMPATIBLE)
  list(APPEND tblgen_feat_list -D__LLVM_MAJOR_VERSION_22_COMPATIBLE__)
endif()
```

---

## 2. 兼容模式分类

### 2.1 `bufferization::ToMemrefOp` → `bufferization::ToBufferOp`（LLVM 22）

**变更说明：** LLVM 22 将 `bufferization::ToMemrefOp` 重命名为 `bufferization::ToBufferOp`。

**影响文件（共 6 处）：**

| 文件 | 行号 |
|------|------|
| `lib/ExecutionEngine/ConvertHIVMToUpstream.cpp` | 719-723 |
| `lib/ExecutionEngine/CreateHostMain.cpp` | 228-234 |
| `lib/Dialect/HIVM/IR/HIVMImpl.h` | 78-86 |
| `lib/Dialect/HIVM/Utils/Utils.cpp` | 103-107, 709-713, 914-922 |
| `lib/Dialect/HIVM/Transforms/InsertLoadStoreForMixCV/Utils.cpp` | 136-142 |

**兼容模式：** `#ifndef __LLVM_MAJOR_VERSION_22_COMPATIBLE__` 走旧 API，`#else` 走新 API。

```cpp
// 模式 A: 创建 op（类型作为 op 名称）
#ifndef __LLVM_MAJOR_VERSION_22_COMPATIBLE__
  using bufferCastOp = bufferization::ToMemrefOp;
#else
  using bufferCastOp = bufferization::ToBufferOp;
#endif

// 模式 B: isa 类型匹配（isa<OldOp> → isa<NewOp>）
#ifndef __LLVM_MAJOR_VERSION_22_COMPATIBLE__
  } else if (auto toMemrefOp = v.getDefiningOp<bufferization::ToMemrefOp>()) {
#else
  } else if (auto toBufferOp = v.getDefiningOp<bufferization::ToBufferOp>()) {
#endif

// 模式 C: isa 列表
#ifndef __LLVM_MAJOR_VERSION_22_COMPATIBLE__
  isa<..., bufferization::ToMemrefOp, bufferization::ToTensorOp>(userOp)
#else
  isa<..., bufferization::ToBufferOp, bufferization::ToTensorOp>(userOp)
#endif
```

**`ToTensorOp` 构造函数变更（LLVM 22，与上述相关但独立）：**

LLVM 22 的 `bufferization::ToTensorOp::create` 需要显式传入 tensor 类型，旧版从 memref 自动推导：

```cpp
// 旧版: 自动推导返回类型
rewriter.create<bufferization::ToTensorOp>(loc, alloc, true, true);
// 新版: 需要显式传入 tensor type
auto tensorType = RankedTensorType::get(targetShape, elementType);
rewriter.create<bufferization::ToTensorOp>(loc, tensorType, alloc, true, true);
```

影响文件：
- `lib/Dialect/HIVM/Utils/Utils.cpp:883-891`
- `lib/Dialect/HIVM/Transforms/InsertLoadStoreForMixCV/Utils.cpp:136-142`

---

### 2.2 `getStridesAndOffset` 从自由函数变成成员函数（LLVM 21）

**变更说明：** LLVM 21 之前 `getStridesAndOffset(memrefType)` 是全局自由函数；LLVM 21+ 变为 `memrefType.getStridesAndOffset()` 成员函数。

**影响文件：**

| 文件 | 行号 |
|------|------|
| `lib/Dialect/HIVM/IR/HIVMImpl.cpp` | 378-382 |
| `lib/Dialect/HIVM/IR/HIVMTraits.cpp` | 47-52 |
| `lib/Dialect/HIVM/Utils/Utils.cpp` | 1166-1170, 1296-1300 |
| `lib/Dialect/HIVM/IR/BiShengIRAggregatedOpInterface/DecomposeOperation.cpp` | 1173-1179 |

**兼容模式：**

```cpp
// 模式 A: 结构化绑定接收
#ifndef __LLVM_MAJOR_VERSION_21_COMPATIBLE__
  auto [strides, offset] = getStridesAndOffset(memrefType);
#else
  auto [strides, offset] = memrefType.getStridesAndOffset();
#endif

// 模式 B: 函数失败检查
#ifndef __LLVM_MAJOR_VERSION_21_COMPATIBLE__
  if (failed(getStridesAndOffset(srcType, srcStrides, srcOffset)))
#else
  if (failed(srcType.getStridesAndOffset(srcStrides, srcOffset)))
#endif
```

---

### 2.3 `RegionBuilderFn` 签名增加 `emitError` 回调（LLVM 22）

**变更说明：** LLVM 22 中 `RegionBuilderFn` 的类型签名增加了第四个参数 `function_ref<InFlightDiagnostic()>`，用于在 region builder 内部报错。

**影响文件：**
- `lib/Dialect/HFusion/IR/HFusionOps.cpp`（10+ 处）
- `tools/bishengir-hfusion-ods-gen/bishengir-hfusion-ods-yaml-gen.cpp`

**兼容模式：**

```cpp
// 类型别名定义
#ifndef __LLVM_MAJOR_VERSION_22_COMPATIBLE__
using RegionBuilderFn = llvm::function_ref<void(
    ImplicitLocOpBuilder &, Block &, ArrayRef<NamedAttribute>)>;
#else
using RegionBuilderFn = llvm::function_ref<void(
    ImplicitLocOpBuilder &, Block &, ArrayRef<NamedAttribute>,
    function_ref<InFlightDiagnostic()>)>;
#endif

// 调用点: 旧版 3 参数，新版 4 参数
#ifndef __LLVM_MAJOR_VERSION_22_COMPATIBLE__
  regionBuilder(b, *body, attrs);
#else
  regionBuilder(b, *body, attrs, [&]() {
    return mlir::emitError(opBuilder.getUnknownLoc());
  });
#endif

// 函数签名定义
#ifndef __LLVM_MAJOR_VERSION_22_COMPATIBLE__
std::function<void(ImplicitLocOpBuilder &, Block &, ArrayRef<NamedAttribute>)>
#else
std::function<void(ImplicitLocOpBuilder &, Block &, ArrayRef<NamedAttribute>,
                   function_ref<InFlightDiagnostic()>)>
#endif
ReduceWithIndexOp::getRegionBuilder() { ... }

// 函数体内接收额外参数（所有 getRegionBuilder 方法）
  return [](ImplicitLocOpBuilder &b, Block &block,
            ArrayRef<NamedAttribute> attrs
#ifdef __LLVM_MAJOR_VERSION_22_COMPATIBLE__
            , function_ref<InFlightDiagnostic()> emitError
#endif
  ) { ... };
```

涉及的自定义 Op：`ReduceWithIndexOp`, `ArangeOp`, `GatherOp`, `GatherMaskOp`, `Conv1DOp`, `Conv2DOp`, `Conv3DOp`

---

### 2.4 `MeshDialect` 头文件移除（LLVM 22）

**变更说明：** LLVM 22 中 `mlir/Dialect/Mesh/IR/MeshDialect.h` 被移除。

**影响文件：** `include/bishengir/Dialect/HFusion/IR/HFusion.h:21-23`

**兼容模式：**

```cpp
#ifndef __LLVM_MAJOR_VERSION_22_COMPATIBLE__
#include "mlir/Dialect/Mesh/IR/MeshDialect.h"
#endif
```

---

### 2.5 `CopyOpInterface` 从 MLIR 上游移除 → 本地 vendored（LLVM 22）

**变更说明：** LLVM 22 移除了 `CopyOpInterface`（PR #157711）。AscendNPU-IR 在本地定义了一个等价的 interface。

**文件结构：**
- `include/bishengir/Interfaces/CopyOpInterface.td` — vendored 定义
- `include/bishengir/Interfaces/CopyOpInterface.h` — 条件包含

**影响文件：**
- `include/bishengir/Interfaces/CopyOpInterface.h:21-28`
- `lib/Dialect/HIVM/IR/HIVMInterfaces.cpp:42-45`

**兼容模式：**

```cpp
// CopyOpInterface.h: LLVM 22 时使用本地 vendored 版本
#ifdef __LLVM_MAJOR_VERSION_22_COMPATIBLE__
#include "mlir/IR/OpDefinition.h"
#include "bishengir/Interfaces/CopyOpInterface.h.inc"
#endif

// HIVMInterfaces.cpp: LLVM 22 时编译本地实现
#ifdef __LLVM_MAJOR_VERSION_22_COMPATIBLE__
#include "bishengir/Interfaces/CopyOpInterface.cpp.inc"
#endif
```

---

### 2.6 `linalg::ElemwiseBinaryOp` / `linalg::ElemwiseUnaryOp` 移除（LLVM 22）

**变更说明：** LLVM 22 移除了 `linalg::ElemwiseBinaryOp` 和 `linalg::ElemwiseUnaryOp`，改用 `isElementwiseOp(op)` 通用函数替代。

**影响文件：** `lib/Dialect/Utils/Util.cpp:1161-1216`

**兼容模式：**

```cpp
// 模式 A: isa 检查替换为 isElementwiseOp
#ifndef __LLVM_MAJOR_VERSION_22_COMPATIBLE__
  return isa_and_present<linalg::ElemwiseBinaryOp, linalg::ElemwiseUnaryOp,
                         linalg::FillOp>(op);
#else
  return isa_and_present<linalg::FillOp>(op) || isElementwiseOp(op);
#endif

// 模式 B: 特定 UnaryOp 检查
#ifndef __LLVM_MAJOR_VERSION_22_COMPATIBLE__
  return isa_and_present<linalg::ElemwiseUnaryOp, linalg::FillOp>(op);
#else
  if (isa_and_present<linalg::FillOp>(op)) { return true; }
  if (isElementwiseOp(op)) {
    auto genericOp = dyn_cast<linalg::LinalgOp>(op);
    return genericOp.getNumDpsInputs() == 1;
  }
  return false;
#endif

// 模式 C: 合法 Op 列表中移除
bool isLegalOp(Operation *op) {
  if (isa<linalg::MapOp, linalg::FillOp, linalg::GenericOp,
#ifndef __LLVM_MAJOR_VERSION_22_COMPATIBLE__
          linalg::ElemwiseBinaryOp, linalg::ElemwiseUnaryOp,
#endif
          ...>(op)) { return true; }
}
```

---

### 2.7 `TargetSystemSpecAttr` / `DeviceIDTargetDeviceSpecPair` → `DataLayoutEntryInterface`（LLVM 22）

**变更说明：** LLVM 22 修改了 `TargetSystemSpecAttr` 的内部类型，从 `DeviceIDTargetDeviceSpecPair` 变成通用的 `DataLayoutEntryAttr` / `DataLayoutEntryInterface`。

**影响文件：** `lib/Dialect/HACC/Utils/Utils.cpp:189-195`

**兼容模式：**

```cpp
void setNPUTargetSpec(ModuleOp op, HACCTargetDeviceSpecInterface spec) {
  MLIRContext *ctx = op->getContext();
#ifndef __LLVM_MAJOR_VERSION_22_COMPATIBLE__
  SmallVector<DeviceIDTargetDeviceSpecPair> entries;
  entries.push_back({StringAttr::get(ctx, kNPUStr), spec});
#else
  SmallVector<DataLayoutEntryInterface> entries;
  entries.push_back(
      DataLayoutEntryAttr::get(ctx, StringAttr::get(ctx, kNPUStr), spec));
#endif
  op->setAttr(TargetSystemSpecAttr::name,
              TargetSystemSpecAttr::get(ctx, entries));
}
```

---

### 2.8 `DISubprogramAttr::get` 参数变更（LLVM 20 / 22）

**变更说明：** LLVM 20+ 中 `LLVM::DISubprogramAttr::get` 的参数增加了 `LLVM::DISubprogramFlags` 枚举替代原先的 `unsigned`，且中间参数的顺序/类型有调整。

**影响文件：** `lib/Dialect/HACC/Utils/Utils.cpp:284-287`

**兼容模式：**

```cpp
#if defined(__LLVM_MAJOR_VERSION_20_COMPATIBLE__) || defined(__LLVM_MAJOR_VERSION_22_COMPATIBLE__)
  auto newAttr = LLVM::DISubprogramAttr::get(
      llvmFunc->getContext(), DistinctAttr(), LLVM::DICompileUnitAttr(),
      originalAttr.getScope(), originalAttr.getName(),
      originalAttr.getLinkageName(), originalAttr.getFile(), unsigned(),
      unsigned(), LLVM::DISubprogramFlags::Optimized,
      originalAttr.getType(), {}, {});
#else
  auto newAttr = LLVM::DISubprogramAttr::get(
      llvmFunc->getContext(), DistinctAttr(), LLVM::DICompileUnitAttr(),
      originalAttr.getScope(), originalAttr.getName(),
      originalAttr.getLinkageName(), originalAttr.getFile(), unsigned(),
      unsigned(), originalAttr.getType(), {}, {});
#endif
```

关键差异：
- LLVM 20/22：`get(..., unsigned(), unsigned(), Flags, type, {}, {})` 多了 `Flags` 参数
- LLVM 19：`get(..., unsigned(), unsigned(), type, {}, {})` 参数较少

---

### 2.9 `Record::getDirectSuperClasses` 返回类型变更（LLVM 20/21）

**变更说明：** TableGen 的 `Record::getDirectSuperClasses` 方法签名逐版变化：
- LLVM 19: `getDirectSuperClasses(SmallVector<Record *>)`，元素为非 const
- LLVM 20/21: `getDirectSuperClasses(SmallVector<const Record *>)`，输出参数
- LLVM 22: `getDirectSuperClasses() -> ArrayRef<pair<const Record *, SMRange>>`，返回值

**影响文件：** `tools/bishengir-target-spec-tblgen/TargetSpecGen.cpp:105-119`

**兼容模式：**

```cpp
#if defined(__LLVM_MAJOR_VERSION_22_COMPATIBLE__)
  // LLVM 22: 返回 ArrayRef<pair<const Record *, SMRange>>
  auto superClasses = derivedClassRecord->getDirectSuperClasses();
  const Record *superClass = superClasses.front().first;
#elif defined(__LLVM_MAJOR_VERSION_21_COMPATIBLE__) || defined(__LLVM_MAJOR_VERSION_20_COMPATIBLE__)
  // LLVM 20/21: 输出参数 SmallVector<const Record *>
  SmallVector<const Record *> superClasses;
  derivedClassRecord->getDirectSuperClasses(superClasses);
  const Record *superClass = superClasses.front();
#else
  // LLVM 19: 输出参数 SmallVector<Record *>
  SmallVector<Record *> superClasses;
  derivedClassRecord->getDirectSuperClasses(superClasses);
  Record *superClass = superClasses.front();
#endif
```

同一文件中，函数签名也需要适配（共 3 处：`emitStrToSymFnForDeviceTarget`、`emitSymToStrFnForDeviceTarget` 等）：

```cpp
// LLVM 20+：Record 指针是 const
#if defined(__LLVM_MAJOR_VERSION_20_COMPATIBLE__) || defined(__LLVM_MAJOR_VERSION_21_COMPATIBLE__)
static void emitStrToSymFn(const std::vector<const Record *> &records, ...);
#else
static void emitStrToSymFn(const std::vector<Record *> &records, ...);
#endif
```

以及在 `emitSymToStrFn` 函数体内，`switch` 语句格式略有不同：

```cpp
// LLVM 20+：
OS << "  switch (val) {\n";
// LLVM 19：
OS << formatv("  switch (val) {{\n", enumName);
```

---

### 2.10 `arith::ConstantIntOp` 参数顺序变更（LLVM 22）

**变更说明：** LLVM 22 中 `arith::ConstantIntOp::create` 的参数顺序从 `(loc, value, type)` 变成 `(loc, type, value)`。

**影响文件：** `lib/Dialect/HFusion/IR/HFusionOps.cpp:2747-2751`

**兼容模式：**

```cpp
#ifndef __LLVM_MAJOR_VERSION_22_COMPATIBLE__
  return b.create<arith::ConstantIntOp>(loc, 0, ty);       // 旧: 值在前
#else
  return b.create<arith::ConstantIntOp>(loc, ty, static_cast<int64_t>(0)); // 新: 类型在前
#endif
```

---

### 2.11 `concatAffineMaps` 增加 MLIRContext 参数（LLVM 21）

**变更说明：** LLVM 21 中 `concatAffineMaps` 从 2 参数变为 3 参数，新增 `MLIRContext *` 参数。

**影响文件：** `include/bishengir/Dialect/HIVM/IR/HIVMInterfaces.td:539`

**兼容模式（TableGen）：**

```tablegen
// HIVMInterfaces.td
#ifndef __LLVM_MAJOR_VERSION_21_COMPATIBLE__
  concatAffineMaps(maps)                             // LLVM 19/20: 2 参数
#else
  concatAffineMaps(maps, $_op.getContext())           // LLVM 21+: 3 参数
#endif
```

---

### 2.12 TableGen `$_op` 自引用限制（LLVM 21）

**变更说明：** LLVM 21 的 TableGen 在 interface 方法的默认实现中，`$_op` 自引用不再能直接调用成员函数，必须先 cast 到 `ConcreteOp`。

**影响文件：** `include/bishengir/Dialect/HIVM/Interfaces/OpPipeInterface.td:63,82,101`

**兼容模式：**

```tablegen
// LLVM 19/20: 直接使用 $_op
$_op.getPipe()

// LLVM 21+: 先 cast 到 ConcreteOp
ConcreteOp op = $_op;
return op.getPipe();
```

---

### 2.13 `RecordKeeper` const 修饰符变更（LLVM 20/21 vs 22）

**变更说明：** TableGen 工具中 `RecordKeeper` 引用在 LLVM 20/21 是 `const`，LLVM 19 和 LLVM 22+ 是非 const。

**影响文件：** `tools/bishengir-target-spec-tblgen/bishengir-target-spec-tblgen.cpp:46`

**兼容模式：**

```cpp
#if defined(__LLVM_MAJOR_VERSION_20_COMPATIBLE__) || defined(__LLVM_MAJOR_VERSION_21_COMPATIBLE__)
static bool bishengirTargetSpecGenMain(raw_ostream &os, const RecordKeeper &records)
#else
static bool bishengirTargetSpecGenMain(raw_ostream &os, RecordKeeper &records)
#endif
```

---

### 2.14 `StringSwitch` 格式化字符串差异（LLVM 20/21）

**变更说明：** `emitSymToStrFnForDeviceTarget` 函数中 switch 语句的格式字符串有微小差异。

**影响文件：** `tools/bishengir-target-spec-tblgen/TargetSpecGen.cpp:273`

```cpp
#if defined(__LLVM_MAJOR_VERSION_20_COMPATIBLE__) || defined(__LLVM_MAJOR_VERSION_21_COMPATIBLE__)
  OS << "  switch (val) {\n";                  // 直接字符串
#else
  OS << formatv("  switch (val) {{\n", enumName);  // 带格式
#endif
```

---

## 3. 快速参考

| 版本 | 宏 | 关键变更 |
|------|-----|---------|
| LLVM 20 | `__LLVM_MAJOR_VERSION_20_COMPATIBLE__` | `DISubprogramAttr::get` 签名、`getDirectSuperClasses` const 化 |
| LLVM 21 | `__LLVM_MAJOR_VERSION_21_COMPATIBLE__` | `getStridesAndOffset` 成员函数化、上一条的 const 化 |
| LLVM 22 | `__LLVM_MAJOR_VERSION_22_COMPATIBLE__` + 上一条 | 见下表 |

### LLVM 21 变更速查表

| # | 变更项 | 适配方式 |
|---|--------|---------|
| 1 | `getStridesAndOffset` 从自由函数→成员函数 | 自由函数 for <21, 成员函数 for ≥21 |
| 2 | `concatAffineMaps` 增加 `MLIRContext*` 参数 | TableGen `#ifndef` 分支 |
| 3 | TableGen `$_op` 自引用需 cast `ConcreteOp` | TableGen `#ifndef` 分支，LLVM 21+ 先 cast |
| 4 | `Record` 和 `RecordKeeper` const 化 | `#if` 三版本分支 |

### LLVM 22 变更速查表

| # | 变更项 | 适配方式 |
|---|--------|---------|
| 1 | `bufferization::ToMemrefOp` → `ToBufferOp` | `#ifndef` 用旧名，`#else` 用新名 |
| 2 | `bufferization::ToTensorOp` 构造函数多一个 `Type` 参数 | `#ifndef` 旧签名，`#else` 新签名 |
| 3 | `RegionBuilderFn` 增加 `emitError` 参数 | `#ifndef` 3参数，`#else` 4参数 |
| 4 | `MeshDialect.h` 移除 | `#ifndef` 才 include |
| 5 | `CopyOpInterface` 移除 | 本地 vendored，`#ifdef` 才 include/编译 |
| 6 | `linalg::ElemwiseBinaryOp/UnaryOp` 移除 | 改用 `isElementwiseOp()` |
| 7 | `DeviceIDTargetDeviceSpecPair` → `DataLayoutEntryInterface` | `#ifndef` 旧类型，`#else` 新类型 |
| 8 | `getDirectSuperClasses` 返回值变化 | `#if` 三版本分支 |
| 9 | `arith::ConstantIntOp` 参数顺序 | `#ifndef` 旧序，`#else` 新序 |

---

## 4. 新版本适配流程

升级到 LLVM N+1 时，建议按以下步骤操作：

1. **建 CMake Option：** 在根 `CMakeLists.txt` 中添加 `LLVM_MAJOR_VERSION_N_COMPATIBLE` 选项
2. **传递到子模块：** 在各 `CMakeLists.txt` 的 `tblgen_feat_list` 中追加宏
3. **分类处理变更：** 参照上述模式，在每个变更点用 `#ifndef __LLVM_MAJOR_VERSION_N_COMPATIBLE__` 保留旧版路径
4. **复用机制：** 如果 N+1 也需保留 N 的变更，在 N+1 的 CMake 分支中同时 `add_definitions` N 的宏
5. **清理旧版：** 不再需要支持的旧版本，删除对应 CMake option 和 `#ifndef` 分支
