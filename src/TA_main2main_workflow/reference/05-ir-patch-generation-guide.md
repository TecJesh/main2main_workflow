# IR Patch Generation Guide — LLVM Backward Compatibility

Use this guide during IR patch generation (mode: `ir_generate_patch`). The goal is to produce `.patch` files that modify TA-side LLVM so it generates IR compatible with the **unmodified** AscendNPU-IR (BishengIR).

---

## 1. Core Strategy: TA-Side LLVM Direct OP Patch

### 1.1 Why Direct OP Patch (Not BC Patch)

Two approaches exist for IR backward compatibility:

| 方案 | 机制 | 影响范围 |
|------|------|---------|
| BC patch (`useCustomPropertiesEncoding`) | 修改 MLIR 属性序列化格式 | **TA + NPU-IR 两侧都需要 patch LLVM** |
| **Direct OP patch** (`ir_compatibility_patch_example.patch` 方式) | 修改 TA 侧 LLVM 的 OP 定义，生成旧版 NPU-IR 可解析的 IR | **仅 TA 侧 LLVM** |

**BC 方案不可行的原因**：BC patch 要求 TA 和 NPU-IR 两侧都对 LLVM 打补丁。TA 侧打补丁后重新编译即可，但 NPU-IR 侧打完 LLVM 补丁后还需要重新编译整个 NPU-IR 包才能获得可用的 `bishengir-opt`。TA 侧无法给 NPU-IR 打 LLVM 补丁，也无法编译 NPU-IR 包。

**直接 OP patch 方案可行**：仅修改 TA 侧的 LLVM（OP 定义、assemblyFormat、兼容别名），使新 LLVM 生成的 IR 能被旧版 NPU-IR 解析。NPU-IR 侧完全不需要改动。

### 1.2 工作原理

```
TA 侧 LLVM（打补丁后，新版本）
    ↓ 生成 IR（兼容旧格式）
NPU-IR / BishengIR（未修改，旧版本）
    ↓ 正常解析 IR
NPU 硬件执行
```

TA 侧 patch 的目标是：让新版 LLVM **输出旧版 NPU-IR 能看懂的 IR**。

---

## 2. Patch 格式与模板

### 2.1 模板参考：`ir_compatibility_patch_example.patch`

以 `ir_compatibility_patch_example.patch`（与本指南同目录）为模板，遵循 `git format-patch` 格式：

```diff
From: TA Sync Bot <ta-sync-bot@users.noreply.github.com>
Subject: [PATCH] Backward-compatible OP definitions for AscendNPU-IR

Modify TA-side LLVM OP definitions so the generated IR remains
compatible with the unmodified AscendNPU-IR / BishengIR compiler.

diff --git a/mlir/include/mlir/Dialect/XXX/IR/XXXOps.td ...
--- a/mlir/include/mlir/Dialect/XXX/IR/XXXOps.td
+++ b/mlir/include/mlir/Dialect/XXX/IR/XXXOps.td
@@ ... @@ def XXXOp : ... {
   let arguments = (ins ...);
   ...
+  // backward compat: accept old operand name / format
 }
```

### 2.2 常见 Patch 模式

| 上游 LLVM 变更 | TA 侧 Patch 策略 |
|---------------|-----------------|
| Op 重命名（如 `IndirectLoadOp` → `UnstructuredLoadOp`） | 保留新 Op，**添加旧名称的兼容别名** |
| assemblyFormat 变更 | 修改 assemblyFormat 同时接受新旧格式，或修改 custom printer/parser |
| Op 属性 getter 重命名（如 `getLhs()` → `getA()`） | 添加旧名称的 alias getter，或修改 Ascend 后端调用处 |
| create() 参数增加 | 添加默认参数的重载，使旧签名仍然可用 |
| Pass 选项重命名 | 添加旧选项名的 alias 映射 |
| Op trait 变更 | 检查 Ascend 后端是否依赖该 trait，必要时在 patch 中保留 |

### 2.3 不需要 Patch 的变更

以下变更**不需要** LLVM patch，应在 code adaptation 阶段修改 Ascend 后端代码：
- C++ API 调用方式变更（`ofr.get<Value>()` → `cast<Value>(ofr)`）
- 头文件路径移动
- 函数签名变更（更新调用处即可）

---

## 3. OP 定义变更分析

### 3.1 对比检查清单

对 Ascend 后端使用的每个 OP，对比新旧 LLVM 版本的 TableGen 定义：

1. **Op 名称** — 是否被重命名？
2. **assemblyFormat** — 格式字符串是否变化？旧格式是否仍可解析？
3. **Op 参数（arguments/results）** — 顺序、名称、类型是否变化？
4. **Op trait** — 是否新增/移除？
5. **builder/create() 方法** — 参数签名是否变化？
6. **custom printer/parser** — 输出格式是否变化？

### 3.2 定位 OP 定义

```bash
# 查找 Op 的 TableGen 定义
grep -r "def XXXOp" llvm-project/mlir/include/

# 对比两个 LLVM 版本的差异
cd llvm-project
git diff <old-hash> <new-hash> -- mlir/include/mlir/Dialect/
```

---

## 4. Patch 组织规范

**生成单个完整的 patch 文件**：`ir_compat.patch`

放置于 `{WORKSPACE_DIR}/ir-patches/ir_compat.patch`。

要求：
- **完整性优先**：必须覆盖 `changes_report.json` 中所有 `needs_patch: true` 的 OP，遗漏任何一个都会导致外层循环重试（LLVM 重编约 2 小时，代价高昂）
- 作为一个原子变更 apply 到 llvm-project 的目标 commit
- 遵循 `git format-patch` 格式，包含清晰的 commit message
- 仅修改 `mlir/` 下的 `.td` 和 `.cpp` 文件
- 参考 `ir_compatibility_patch_example.patch` 的风格

---

## 5. 验证方式

```bash
# 1. Apply patch
cd llvm-project
git apply /path/to/generated.patch

# 2. 重新编译 LLVM
# (由 workflow 的 _check_and_rebuild_llvm 自动完成)

# 3. 编译 Triton-Ascend → 运行 pytest
# (由 workflow Phase 4 自动完成)
```

---

## 6. AscendNPU-IR Patches 参考

Location: `third_party/ascend/AscendNPU-IR/build-tools/patches/llvm-project/`

这些是 NPU-IR 侧的 patches，TA 侧 workflow **不应用也不会修改它们**。在生成 TA 侧 patch 时，检查是否与这些 patch 有冲突即可。
