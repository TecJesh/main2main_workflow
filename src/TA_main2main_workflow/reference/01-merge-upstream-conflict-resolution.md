# Triton-Ascend 上游代码合并与冲突解决指导

## 概述

本文档记录了 Triton-Ascend 从 3.2.x（基于上游 `152ef2d`, LLVM 20）升级到 3.5.x（基于上游 `cfc0a9d`, LLVM 22）过程中，合并上游 Triton 代码的冲突解决方法和最佳实践。

升级涉及 2579 个提交，核心合并提交为 `6744f5ff3c`（merge release/3.5.x-upgrade into main）。

---

## 1. 升级总体策略

### 1.1 分阶段升级路径

升级采用渐进式合并策略，分为三个主要阶段：

```
Phase 1: 3.2.x → 3.3.x（过渡阶段）
  - 分支: release/3.3.x-upgrade
  - 合并上游 3.3.x 变更
  - 修复编译错误，适配 LLVM API 变更

Phase 2: 3.3.x → 3.4.x（中间阶段）
  - 分支: release/3.4.x
  - 合并上游 3.4.x 变更
  - 移除 3.4 中废弃的函数

Phase 3: 3.4.x → 3.5.x（最终阶段）
  - 分支: release/3.5.x-upgrade
  - 合并上游 3.5.x (cfc0a9d)
  - 合并 Triton-Ascend 主分支 (62eb951f)
  - 最终合并回 main: 6744f5ff3c
```

### 1.2 分支管理规范

- **release/3.5.x-upgrade**: 主升级分支，承接上游变更 + Ascend 适配
- **release/3.5.x-upgrade-candy-dev**: 开发者 candy 的适配分支
- **release/3.5.x-upgrade-jeshd-dev-***: 开发者 jeshd 的专项修复分支
- **main-merge-3.2.2-upgrade**: 3.2.2 版本维护者的合并分支

每个开发者在自己的 dev 分支上完成特定领域的适配工作，然后通过 MR 合并到 upgrade 主分支。

---

## 2. 冲突解决核心方法论

### 2.1 基本原则

在合并上游 Triton 代码时，遵循以下优先级：

1. **保留上游 Triton 的核心架构变更** — 不可回退
2. **适配 Ascend 后端到新架构** — 修改 Ascend 代码以适配新 API
3. **保留 Ascend 特有功能** — NPU 相关的编译流程、算子等
4. **回退上游不兼容的变更** — 仅在 Ascend 后端无法适配时使用 `git revert`

### 2.2 冲突分类与处理策略

| 冲突类型 | 处理策略 | 示例 |
|---------|---------|------|
| **Python 前端架构重构** | 保留上游新架构，适配 Ascend 后端钩子 | semantic.py 重构 |
| **C++ MLIR API 变更** | 全部采用新 API | `get<>()` → `cast<>()` |
| **编译流程变更** | 保留上游流程 + 插入 Ascend 特有步骤 | BC 字节码管线 |
| **算子定义变更** | 保留上游标准，适配 Ascend 转换器 | DotScale 属性重命名 |
| **构建系统** | 保留上游 CMake 结构 + Ascend 扩展 | LLVM 版本宏 |
| **测试用例** | 保留上游测试 + Ascend 特有测试 | pytest_ut 目录 |

---

## 3. 关键冲突案例详解

### 3.1 Python 前端架构重构（最大冲突）

#### 背景
上游 Triton 3.5.x 引入了 `TritonSemantic` 类，将 semantic.py 中所有函数重构为类方法，同时将 `_builder` 参数统一改为 `_semantic`。

#### 冲突范围
- `python/triton/language/semantic.py`：所有函数重构为 `TritonSemantic` 类方法
- `python/triton/language/core.py`：引入 `base_value`、`base_type`、`JITCallable` 等新类型
- `python/triton/compiler/code_generator.py`：IR 类型系统重构
- 所有 Ascend 扩展代码中引用 `_builder` 的地方

#### 解决方案

**Step 1**: 全局替换 `_builder` → `_semantic` 参数
```python
# 旧代码（3.2.x）
def program_id(axis: int, builder: ir.builder) -> tl.tensor:
    return tl.tensor(builder.create_get_program_id(axis), tl.int32)

# 新代码（3.5.x）
class TritonSemantic(Generic[TensorTy]):
    builder: ir.builder

    def __init__(self, builder):
        self.builder = builder

    def program_id(self, axis: int) -> TensorTy:
        return self.tensor(self.builder.create_get_program_id(axis), tl.int32)
```

**Step 2**: 适配 Ascend 扩展中的调用
```python
# Ascend 扩展代码需要同步更新参数名
# 涉及文件:
#   - python/triton/extension/buffer/language/core.py
#   - python/triton/language/extra/cann/extension/core.py
#   - third_party/ascend/backend/compiler.py
```

**Step 3**: 适配新的 IR 类型系统
```python
# 3.2.x: _value
isinstance(o, _value)

# 3.5.x: base_value
isinstance(o, base_value)

# 3.2.x: constexpr
isinstance(o, constexpr)

# 3.5.x: constexpr + dtype + JITCallable 都视为 constexpr
isinstance(o, (constexpr, language.core.dtype, JITCallable))
```

#### 关键提交
- `6744f5ff3c` — 主合并，引入 TritonSemantic
- `2c5aa74204` — 修复 custom op compile error: `_builder` → `_semantic`
- `2fa52eaa2e` — 修复 create_sync_block_set API
- `f735f8296c` — 修复 libdevice 中的 `_builder` → `_semantic`

### 3.2 字节码（Bytecode）编译管线引入

#### 背景
由于 TritonAscend（基于 LLVM 22）和 AscendNPU-IR 使用不同版本的 LLVM，直接传递 MLIR 文本会导致 IR 兼容性问题。解决方案是在编译流程中引入 BC（Bytecode）作为中间格式。

#### 新增工具
- `bin/triton-mlir-opt.cpp`：将 MLIR 文本转为 BC 格式
- `bin/bishengir-opt`（已存在）：将 BC 转回 MLIR 文本

#### 编译流程变更
```
旧流程（3.2.x）:
  Linalg IR → LLIR → Binary (bishengir-compile 直接处理)

新流程（3.5.x, use_bytecode=True）:
  Linalg IR → MLIR Bytecode (triton-mlir-opt) → LLIR (bishengir-opt) → Binary (bishengir-compile)
```

#### 关键代码
```python
# third_party/ascend/backend/compiler.py
class NPUOptions:
    use_bytecode: bool = True  # 新选项，默认启用 BC 模式

def linalg_to_bc_by_triton_mlir_opt(linalg, metadata, opt):
    """Linalg IR → MLIR Bytecode"""
    subprocess.check_call([
        triton_mlir_opt_path,
        ttadapter_path,
        "--emit-bytecode",
        "-o", bc_path,
    ])
    return bc_data

def bc_to_linalg_by_bishengir_opt(bc_data, metadata, opt):
    """MLIR Bytecode → MLIR Text (LLIR)"""
    subprocess.check_call([
        bishengir_opt_path,
        bc_path,
        "-o", mlir_path,
    ])
    return linalg_text
```

#### 关键提交
- `f2cf3f2c5b` — 引入 triton-mlir-opt 和 BC 编译流程
- `6223b79851` — LLVM patch 支持 BC 格式
- `69387fab37` — 重命名 llvm_patch 目录

### 3.3 算子属性重命名（DotScale）

上游 Triton 3.5.x 将 `DotScaledOp` 的属性名从 `lhs/lhsScale/rhsScale/rhs` 改为 `a/aScale/bScale/b`。

```cpp
// 冲突代码修复
// 旧版本 (3.2.x):
Value lhs = adaptor.getLhs();
Value lhsScale = adaptor.getLhsScale();
Value rhsScale = adaptor.getRhsScale();
Value rhs = adaptor.getRhs();

// 新版本 (3.5.x):
Value lhs = adaptor.getA();
Value lhsScale = adaptor.getAScale();
Value rhsScale = adaptor.getBScale();
Value rhs = adaptor.getB();
```

#### 关键提交
- `7222a734c6` — DotScale 属性名适配

---

## 4. 合并冲突解决操作指南

### 4.1 标准合并流程

```bash
# 1. 准备工作分支
git checkout -b merge-upstream-3.x.x

# 2. 合并上游 Triton 的特定版本
git merge <upstream-triton-commit> --no-commit

# 3. 查看冲突文件
git diff --name-only --diff-filter=U

# 4. 分类处理冲突
# - third_party/ascend/**     → 需要手动适配 Ascend 后端
# - python/triton/language/** → 保留上游 + 适配 Ascend 钩子
# - 其他冲突文件              → 通常保留上游版本

# 5. 解决冲突后，验证编译
python setup.py build

# 6. 验证提交
git commit -m "merge: upstream Triton <version>"
```

### 4.2 常见冲突模式速查

| 文件/目录 | 冲突原因 | 处理方式 | 责任人 |
|----------|---------|---------|--------|
| `python/triton/language/semantic.py` | 上游 API 重构 | 保留上游，适配 Ascend 调用 | Frontend |
| `python/triton/language/core.py` | 类型系统变更 | 保留上游，更新 Ascend 引用 | Frontend |
| `python/triton/compiler/code_generator.py` | IR 生成逻辑变更 | 保留上游，适配 Ascend 扩展 | Compiler |
| `third_party/ascend/lib/TritonToLinalg/*` | MLIR API 变更 | 适配新 API | Backend |
| `third_party/ascend/lib/TritonToUnstructure/*` | MLIR API 变更 | 适配新 API | Backend |
| `third_party/ascend/backend/compiler.py` | 编译流程变更 | 保留 Ascend 流程 + 上游改进 | Compiler |
| `third_party/ascend/triton_ascend.cc` | IR 构建器参数变更 | 添加新的 static 参数 | Backend |
| `CMakeLists.txt` | 构建选项变更 | 保留上游 + Ascend 扩展宏 | Build |
| `setup.py` | 依赖版本更新 | 采用上游新依赖版本 | Build |
| `cmake/llvm-hash.txt` | LLVM 版本号变更 | **直接采用上游版本**（该文件由上游维护） | Build |

### 4.3 使用 `git blame` 定位问题

当编译报错时，快速定位是上游还是 Ascend 引入的变更：

```bash
# 找出报错行的最后修改者
git blame <file> -L <line>,<line>

# 对比两个版本的差异
git diff <upstream-3.2-ref> <upstream-3.5-ref> -- <file>

# 查看文件在两个分支上的历史
git log --oneline <upstream-3.2-ref>..<upstream-3.5-ref> -- <file>
```

---

## 5. 注意事项与最佳实践

### 5.1 DO

1. **分阶段合并** — 不要一次性跳过大版本，3.2→3.3→3.4→3.5 逐步来
2. **为每个适配领域创建独立分支** — 编译修复、测试修复、LLVM 适配各一个分支
3. **提交信息使用中文说明适配原因** — 如 "fix: LLVM升级后 xx.get<Value> 被 cast<Value>(xx) 替代"
4. **先保证编译通过，再跑测试** — 大规模合并后先 `python setup.py build`
5. **保留上游的测试，Ascend 测试放在 `third_party/ascend/unittest/`**

### 5.2 DON'T

1. **不要直接 git merge 上游 main** — 上游 main 可能包含不稳定的 API
2. **不要删除上游的测试用例** — 用 `pytest.mark.skip` 标记暂时不支持的
3. **不要随意回退上游变更** — 仅在 Ascend 后端无法适配时使用 revert
4. **不要跳过中间版本** — 3.2→3.5 直接跳会导致大量冲突难以定位

### 5.3 冲突预防

1. **定期同步上游** — 每周或每两周合并一次上游小变更
2. **保持 Ascend 代码的模块化** — 尽量将 Ascend 特有逻辑封装在独立文件中
3. **关注上游 Changelog** — 特别关注标记为 `[BC-breaking]` 的变更
4. **建立自动化合并测试** — 使用 workflow 自动检测编译错误
