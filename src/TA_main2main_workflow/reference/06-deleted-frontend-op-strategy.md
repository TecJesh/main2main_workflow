# 上游删除前端 TritonOp(.td 定义)的处置策略

> 适用模式:conflict / fix。触发场景:上游合并代码删除了前端 `.td` 文件中定义的
> TritonOp(如 `include/triton/Dialect/Triton/IR/TritonOps.td`),而 Ascend 后端
> (`third_party/ascend/`、`lib/Target/Ascend/`)仍在使用该 op。

---

## 1. 背景与目标

上游经常重构前端 op 定义:删除某个 TritonOp、把它合并进另一个 op、或改由其他
机制表达。此时 Ascend 后端的对应代码(Pass 中的 op 匹配、conversion 逻辑、后端
代码生成)仍然引用旧 op,直接跟随上游删除会导致编译失败。

**目标**:让合并后的代码**编译、测试通过**,同时把"op 被删了"这个事实显式暴露
给人工,而不是悄悄跟随上游删掉后端能力。这类 op 变更属于需要后续人工适配的
工作,必须在 PR 描述中体现。

**核心策略**:

1. **保留后端代码** — 不删除 Ascend 后端对该 op 的处理逻辑
2. **op 以前端源码 patch 方式存活** — 把合并前 `.td` 中的 op 定义恢复到工作区,
   由 workflow 机械回流进 `third_party/ascend/patch/triton-ascend-*.patch`(构建时
   setup.py 自动 apply,后端因此能编译通过)
3. **skip 对应测试用例并写明原因**
4. **PR 描述体现 op 变更点**,标注需人工适配

---

## 2. 处置步骤

### 2.1 保留后端代码

- `third_party/ascend/`、`lib/Target/Ascend/` 中对该 op 的处理代码**一律保留**。
- 仅当**其他**上游 API 变更(与 op 删除无关)导致这些代码编译不过时,做最小适配。
- 绝对不要因为"op 没了"就删除后端逻辑——那等于悄悄放弃了 Ascend 对该 op 的
  支持,且人工后续适配时无法恢复。

### 2.2 恢复合并前的 op 定义(改源码,不动 patch 文件)

在 fix mode 中,本 step 的 merge commit 就是当前 HEAD,其**第一父提交就是合并前
的状态**,可直接取出被删的 op:

```bash
# 查看合并前的文件内容(合并前 = 该 step merge commit 的第一父提交)
git -C <triton-ascend> show HEAD^1:include/triton/Dialect/Triton/IR/TritonOps.td

# 反查本次删除内容(HEAD 与 HEAD^1 的差异即"删了什么")
git -C <triton-ascend> diff HEAD HEAD^1 -- include/triton/Dialect/Triton/IR/TritonOps.td
```

若 op 在本 step 之前就已不在(HEAD^1 里也没有),用 `git log` 找到最后一个还有该
op 定义的提交,再从那个提交取。

把 op 定义(以及编译所需的 C++ 侧代码,如 `lib/Dialect/Triton/IR/Ops.cpp` 中的
builder/verifier/print/parse)恢复到合并后的文件里,其余合并进来的上游改动保持
不动。

**注意**:
- 只允许改 op 定义文件:`include/triton/Dialect/Triton/IR/` 与
  `lib/Dialect/Triton/IR/` 下的文件
- **不要直接改 `.patch` 文件** — 由 workflow 机械回流(见 §3),AI 手写 unified
  diff hunk 极易出错且无法自检

### 2.3 报告恢复文件(op_restore_files.txt)

把本次恢复的文件逐个写入 `op_restore_files.txt`(每行一个 repo-root 相对路径):

```
include/triton/Dialect/Triton/IR/TritonOps.td
lib/Dialect/Triton/IR/Ops.cpp
```

workflow 读取该清单后:
1. `git diff HEAD -- <file>` 机械回流进 `triton-ascend-*.patch`(diff 天然包含旧
   patch 内容 + 本次恢复,直接替换 section)
2. 还原被触碰的源码到 HEAD
3. `git apply --check` 验证更新后的 patch
4. 编译/测试通过后按 commit_plan 白名单提交 patch 文件

### 2.4 skip 对应测试用例

测试用例**不要删除**,用 `pytest.mark.skip` 标记并写明原因(仅限
`third_party/ascend/unittest/` 下的测试):

```python
@pytest.mark.skip(
    reason="upstream removed <OpName>; Ascend backend keeps it "
           "via TA source patch — manual adaptation required"
)
def test_affected_case():
    ...
```

reason 必须包含:被删的 op 名 + "后端经 TA source patch 保留" + "待人工适配"。

### 2.5 写入 step_summary.md

在 step_summary.md 中增加 `Manual adaptation required` 小节,逐条记录:

- 被删 op 名称
- 删除它的上游提交 SHA
- 恢复的文件路径(与 op_restore_files.txt 一致)
- skip 的测试用例

report mode 会收集这些条目进入 PR 描述的 "Manual Adaptation Required" 一节。

---

## 3. 底层机制:fix-then-regenerate(父仓库源码 patch)

triton-ascend 的构建脚本(setup.py)每次编译前对 patch 触碰的源码文件执行
`git checkout --` 再 `git apply`,因此**留在源码里的恢复会在下次编译时被静默冲掉**。
必须走"改源码 → 工作流回流 patch → 还原源码"的机制:

```
1. ai_fix:AI 在源码里恢复 op 定义(仅限 .td / Ops.cpp 路径)
   → 写入 op_restore_files.txt
2. regenerate_source_patch(机械、确定性):
   git diff HEAD -- <file> → 合并进 triton-ascend-*.patch
   (优先选择已包含该文件 section 的 patch;否则选主 patch)
3. restore_workspace:显式 git checkout HEAD -- 还原被触碰源码
4. 验证:git apply --check;失败则输出 git 原始报错、还原 patch 文件、丢弃本次修复
5. 下次编译:setup.py 自动 checkout + apply 更新后的 patch,op 恢复、后端编译通过
6. 编译/测试全部通过后:commit_plan → 白名单提交(含 triton-ascend-*.patch 文件;
   workflow 会把 AI 白名单遗漏的 dirty patch 文件兜底补进提交)
```

与 npuir patch 回流机制完全同构(见 `docs/auto-patch-commit-design.md` §5.1),
区别仅在于目标文件在父仓库、目标 patch 是 `triton-ascend-*.patch`。

---

## 4. 边界与注意事项

| # | 场景 | 处置 |
|---|------|------|
| 1 | op 被重命名(未删除,只是换了名字) | 不适用本策略 — 后端适配新名字,见 04-ir-compatibility |
| 2 | 上游删除的是 Ascend 特有 API(`python/triton_ascend/` 等) | 见 04-ir-compatibility-and-backend-adaptation.md §7 |
| 3 | 测试因 op 删除失败 | 按 §2.4 skip,不删测试;参考 03 号文档 §5.2 的 skip 格式规范 |
| 4 | 冲突直接由 op 删除引起 | conflict mode 里保留后端侧、接受上游对前端文件的删除,**不在冲突解决阶段恢复 op**;等 fix 循环按本策略处理 |
| 5 | 恢复后 build 仍报 op 相关错误 | 检查是否还缺 C++ 侧代码(Ops.cpp 的 builder/verifier)、是否还有其他上游文件引用了被删定义;恢复范围可扩展到 `include/triton/Dialect/Triton/IR/` 与 `lib/Dialect/Triton/IR/` 内的其他文件 |
| 6 | 后续人工适配 | 移除 patch 中的恢复 section、适配后端代码到上游新实现、取消测试 skip —— 由人工完成,PR 描述中已列出全部点位 |

---

## 5. 相关文档

- `01-merge-upstream-conflict-resolution.md` — 合并与冲突解决总策略
- `03-unit-test-failure-diagnosis-and-fixes.md` — 测试失败定位与 skip 格式规范
- `04-ir-compatibility-and-backend-adaptation.md` — op 重命名/IR 结构变更的处理
- `docs/auto-patch-commit-design.md` — patch 回流机制(fix-then-regenerate)的设计依据
