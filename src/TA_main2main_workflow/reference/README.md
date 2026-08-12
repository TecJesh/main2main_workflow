# Triton-Ascend 3.2 → 3.5 升级适配指导文档

## 文档索引

本目录包含 Triton-Ascend 从 3.2.x（LLVM 20）升级到 3.5.x（LLVM 22）过程中人工适配工作的完整分析和指导文档。

**分析范围:** 提交 `577a2d2b` 到 `783789c`（2579 commits）

**核心合并提交:** `6744f5ff3c` — merge release/3.5.x-upgrade into main

---

## 文档列表

### [01-merge-upstream-conflict-resolution.md](./01-merge-upstream-conflict-resolution.md)
**上游代码合并与冲突解决指导**
- 升级总体策略（三阶段：3.2→3.3→3.4→3.5）
- 分支管理规范和团队协作模式
- 按文件类型的冲突解决策略速查表
- 关键冲突案例详解（Python 前端重构、BC 管线、DotScale 属性重命名）
- 标准合并操作流程
- 最佳实践与注意事项

### [02-llvm-version-adaptation-and-compile-fixes.md](./02-llvm-version-adaptation-and-compile-fixes.md)
**LLVM 版本升级适配与编译报错修复指导**
- LLVM/MLIR API 变更对照表（15+ 项 API 变更）
- OpFoldResult 类型转换迁移（`get<>()` → `cast<>()`）
- MemRefType API 迁移（`getStridesAndOffset`）
- bufferization Op 重命名（`ToMemrefOp` → `ToBufferOp`）
- ExtractSlice/InsertSlice 参数变更
- LLVM 兼容性宏体系（CMakeLists.txt 配置）
- LLVM Patch 机制（fad3272/f6ded0b）
- 编译错误诊断流程和速查表
- 关键修复提交索引

### [03-unit-test-failure-diagnosis-and-fixes.md](./03-unit-test-failure-diagnosis-and-fixes.md)
**单元测试用例报错定位与修复指导**
- 测试架构概览（pytest_ut/device_ut/Conversion/）
- 8 种典型测试失败案例详解
- API 签名不匹配：`tl.load` 缺少 `other` 参数
- Pass 选项废弃：`force-simt-template` → `compile-mode`
- 自定义算子回退到社区版本
- NPUIR 更新导致测试跳过
- 测试参数膨胀导致 CI 超时
- 负 base offset / `test_neg_index`：pointer rebase（禁止 maxsi）
- 升级后测试检查清单
- 测试调试工具与技巧

### [04-ir-compatibility-and-backend-adaptation.md](./04-ir-compatibility-and-backend-adaptation.md)
**IR 兼容性问题修复指导**
- 跨版本 IR 兼容性根本原因分析
- Bytecode (BC) 编译管线设计与实现
- triton-mlir-opt 工具介绍
- Op 名称/IR 结构变更（indirect→unstructured 等）
- AscendNPU-IR 子模块更新流程
- LLVM Patch 机制的 IR 兼容性保障
- Ascend 特有 API 的移除与适配
- [BC-breaking] 上游变更速查
- IR 兼容性问题诊断流程

---

## 核心发现摘要

### 升级规模
- 2579 个提交
- 202 个文件涉及 Ascend 后端变更（+30,056 行, -2,197 行）
- LLVM 版本：20 → 22

### 三大核心兼容性变更（来自合并提交描述）
1. **Bytecode (BC) 编译管线** — 解决 TritonAscend 与 AscendNPU-IR 之间的跨 LLVM 版本 IR 兼容性问题
2. **Python 前端架构重构** — 适配上游 TritonSemantic 类，全局 `_builder` → `_semantic`
3. **LLVM/MLIR API 适配** — ToMemrefOp→ToBufferOp, getStridesAndOffset, OpFoldResult casting

### 关键开发团队
- candyhong — 升级总协调，LLVM 适配，BC 管线
- jeshd — 编译修复，`_builder`→`_semantic` 迁移
- wangzhanpeng5 — NPUIR 更新，测试修复
- zhang-chunli01 — ExtractSlice/InsertSlice 适配
- zhudada0120 — TritonParse 适配，TritonToLinalg 冲突解决
- lowdy1 — 后端 Pass 适配，Utils 迁移

### 测试结果
- 总计 2090 个功能回归测试
- 1983 通过 (94.9%)
- 107 跳过 (5.1%)

---

## 参考

- 上游 Triton: https://github.com/triton-lang/triton
- Triton-Ascend: https://gitcode.com/Ascend/triton-ascend
- 字节码兼容性问题: https://gitcode.com/Ascend/triton-ascend/issues/403
- LLVM Patch: `third_party/ascend/patch/llvm_patch_f6ded0b.patch`
