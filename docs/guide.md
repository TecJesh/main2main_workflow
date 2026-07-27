# TA Main2Main Auto-Sync -- 工作流完整指南

## 概述

TA_main2main_workflow 是一个自动化流水线，用于将上游 Triton 的更新同步到
triton-ascend（Triton 的 Ascend NPU 适配版）。通过 **git merge + AI 辅助**，
自动完成从检测更新、合并代码、解决冲突、编译构建、运行测试到提交 PR 的全流程。

### 架构

工作流采用模块化管道架构：

```
ta-kickoff (main.py)
  └── TA_Main2MainFlow (flow.py) — 143 workflow编排器
        ├── utils/     — TAConfig, WorkflowContext, TALogger, run_git, timed
        ├── pipeline/  — 13 个独立管道模块
        ├── agent/     — AI 适配器 + prompt 模板
        └── reference/ — AI 参考知识库
```

每个管道模块遵循统一签名：`def step(ctx: WorkflowContext, config: TAConfig) -> WorkflowContext`

### 渐进式步骤合并

当上游有多 commit 时，自动按代码变更量切分为多个步骤，每步分别合并、验证、修复：

1. 逐 commit 统计源码行变更
2. 按行数预算（`TA_LINE_BUDGET`，默认 1000 行）分组
3. LLVM hash 变更的 commit 独占一步
4. 超大 commit 独占一步

| 环境变量 | 说明 | 默认值 |
|---------|------|--------|
| `TA_LINE_BUDGET` | 每步骤最大源码变更行数 | `1000` |

---

## 工作流总览

```
Phase 0: Prepare     克隆/配置 repo，设置 remotes，fetch
Phase 1: Detect      检测待合并的上游 commits，计算 merge-base
Phase 2: Plan        按行数预算切分为步骤 (steps.json)
Phase 3: Per-Step    对每个步骤执行 merge→resolve→build→[ir_patch]→test→commit
Phase 4: Finalize    生成 cumulative patch + summary
Phase Push:          (可选) push + gh pr create
```

详见 [workflow.md](workflow.md) 中的 Mermaid 流程图。

---

## 环境变量

所有配置通过 `TAConfig.from_env()` 集中读取。CLI 参数可覆盖。

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `TRITON_ASCEND_PATH` | triton-ascend 仓库路径 | (workspace clone) |
| `TRITON_PATH` | 上游 triton 仓库路径 | -- |
| `TRITON_TARGET_COMMIT` | 目标 upstream commit | upstream HEAD |
| `AI_BACKEND` | AI 后端: opencode / claude / auto | auto |
| `TA_MAX_RETRIES` | AI 修复最大重试次数 | 10 |
| `IR_MAX_ITERATIONS` | IR 补丁最大迭代次数 | 3 |
| `SKIP_AI_ANALYSIS` | 跳过 AI 调用 | false |
| `SKIP_BUILD` | 跳过编译 | false |
| `SKIP_E2E_TEST` | 跳过测试 | false |
| `SKIP_LLVM_REBUILD` | 跳过 LLVM 重编译 | false |
| `SKIP_IR_PATCH` | 跳过 IR 补丁阶段 | false |
| `PUSH_TO_GITHUB` | 成功后创建 PR | false |
| `GITHUB_REPO` | PR 目标仓库 owner/name | triton-lang/triton-ascend |
| `GH_TOKEN` | GitHub token (PR 创建) | -- |
| `LLVM_PROJECT_PATH` | llvm-project 仓库路径 | ~/llvm-project |
| `LLVM_INSTALL_PREFIX_SYNC` | LLVM 安装前缀 | ~/llvm-install-sync |
| `NUM_PROCS` | pytest 并行 worker 数 | 16 |
| `TA_BASE_BRANCH` | 工作分支基准 | upstream_sync |
| `TA_PR_BASE_BRANCH` | PR 目标分支 | upstream-sync |
| `TA_LINE_BUDGET` | 每步骤最大变更行数 | 1000 |
| `TA_RESUME` | 断点续传模式 | false |

---

## 安装与运行

```bash
cd TA_main2main_workflow
pip install -e .

# 基本用法
ta-kickoff --triton-ascend-path ./triton-ascend

# 指定目标 commit
ta-kickoff --target-commit abc123def --max-retries 5 --num-procs 8

# Dry-run
SKIP_AI_ANALYSIS=true SKIP_BUILD=true SKIP_E2E_TEST=true ta-kickoff
```

---

## 各阶段详解

### Phase 0 -- Prepare (准备环境)

1. 确保 triton-ascend 仓库存在（clone 或使用本地路径）
2. 配置 origin 和 triton-upstream remotes
3. Fetch 两个 remote
4. 基于 base branch 创建 work branch
5. 解析 ascend HEAD 和 target commit

**负责模块**: `pipeline/prepare.py`

### Phase 1 -- Detect (检测更新)

1. 计算 merge-base
2. 列出待合并的 upstream commits
3. 统计改动文件数和行数
4. 输出 `workspace/detect.json`

**负责模块**: `pipeline/detect.py`

### Phase 2 -- Plan (规划步骤)

1. 逐 commit 统计源码行变更
2. 检测含 LLVM hash 变更的 commit（`cmake/llvm-hash.txt`）
3. 按行数预算分组，LLVM 变更和大 commit 独占步骤
4. 输出 `workspace/steps.json`

**负责模块**: `pipeline/plan.py`

### Phase 3 -- Per-Step Loop (步骤执行)

对每个步骤依次执行以下子阶段：

#### Step A: Merge (合并)

`git merge --no-ff` 合并步骤的 end_commit。检测冲突文件。

**负责模块**: `pipeline/merge.py`

#### Step B: Resolve (AI 解决冲突)

仅在 merge 有冲突时执行。AI 分析冲突并解决，最多 `max_retries` 次尝试。

**负责模块**: `pipeline/resolve.py`

#### Step C: Build + Fix (编译修复循环)

```
Round 0: build (首次尝试)
Round 1-N: AI fix → validate_fix → build
```

**修复校验 (validate_fix)**：所有 AI 改动必须在 `third_party/ascend/` 下。
校验失败则 git revert + 写入反馈文件 + 不消耗 attempt 次数。

**负责模块**: `pipeline/build.py`, `pipeline/fix.py`

**校验流程详见**: [fix-validation-flow.md](fix-validation-flow.md)

#### IR Patch Pipeline (LLVM hash 变更时)

当步骤的 reason 为 `llvm_version` 时，走完整的 IR 补丁管线：

**Phase 1 -- 编译适配**：
1. 编译 clean LLVM（无补丁）
2. 编译 TA + AI 修复编译错误
3. 只允许修改 `third_party/ascend/` 下的代码
4. AscendNPU-IR 错误自动检测，AI 参考专项文档

**Phase 2 -- IR 补丁 + 测试循环**：
1. OP 使用分析 → LLVM 变更分析 → AI 生成 IR 补丁
2. 应用补丁 + 编译 LLVM（apply/Build 失败时 AI 修复补丁，最多 10 次）
3. 编译 TA → pytest
4. 测试失败时 AI 分类：IR 问题则重新生成补丁（最多 3 次），代码问题则 AI 修复

**负责模块**: `pipeline/ir_patch.py`

#### Step D: Test + Fix (测试修复循环)

```
Round 0: pytest (首次)
Round 1-N: [OOM? → 降并发重跑] → AI fix → validate_fix → rebuild → pytest
```

- **OOM 处理**：检测到 NPU OOM 时自动降并发（test_procs 减半）重跑，不调用 AI
- **修复校验**：同编译修复，改动的文件必须符合要求
- **测试修复允许范围**：优先 `third_party/ascend/`，根因在上游时允许最小化修改

**负责模块**: `pipeline/test.py`, `pipeline/fix.py`

#### Step E: Commit (提交)

提交当前步骤的进度，含 AscendNPU-IR submodule 变更。

**负责模块**: `pipeline/commit.py`

### Phase 4 -- Finalize (收尾)

1. 生成 `final_summary.md`
2. 生成累积 patch (`final_target.patch`)
3. 生成 PR body (`pr_body.md`)

**负责模块**: `pipeline/finalize.py`

### Phase Push -- PR (可选)

当 `PUSH_TO_GITHUB=true` 时：
1. `git push -u origin <work_branch>`
2. `gh pr create` 创建 PR

**负责模块**: `pipeline/push_pr.py`

---

## 关键设计

### 不可变状态传递

`WorkflowContext` 是 dataclass，通过 `ctx.copy_with(field=value)` 返回新实例。
管道步骤从不原地修改 context，使得数据流完全显式且可测试。

### 配置集中管理

`TAConfig` dataclass 通过 `from_env()` 一次性读取所有环境变量。管道步骤接收
config 作为第二个参数，不再散落各处的 `os.getenv()` 调用。

### 日志系统

`TALogger` 提供统一的格式化输出：`header()`, `section()`, `status()`, `key_value()`,
`step()`, `ai_call()`, `ai_result()`, `table()`, `elapsed()`。

### Git 自动重试

`run_git()` 对网络操作（fetch, clone, push）自动重试 3 次，本地操作直接抛出异常。

### 修复评价三层防护

1. **AI 自检** (prompt.md)：提交前自查文件路径和修复根因
2. **代码硬校验** (validate_fix)：检查修改文件路径，不通过则 revert + 反馈
3. **实际验证**：编译/测试结果

详见 [fix-validation-flow.md](fix-validation-flow.md)

---

## 常见场景

### 无冲突同步
merge → (无冲突) → build → test → pass → commit → DONE

### 有冲突需修复
merge → conflict → AI resolve → build fail → AI fix → build pass → test fail → AI fix → test pass → commit → DONE

### LLVM 版本变更
merge → IR Patch Phase 1: build LLVM → fix compile errors → Phase 2: OP 分析 → 生成补丁 → 编译 LLVM → build TA → pytest → commit → DONE

### 修复耗尽
build → AI fix (R1 rejected) → AI fix (R2) → build fail → ... → max_retries exhausted → UpgradeFailed

work branch 保留，可手动排查。

---

## 输出文件

```
workspace/
├── detect.json
├── steps.json
├── build_result.json / build.log
├── test_result.json
├── test-logs/          (pytest JUnit XML + 日志)
├── llvm_build.log
├── fixes/              (每轮 AI 修复日志)
├── steps/step-N/       (每步产物)
├── ir-analysis/        (IR 分析报告)
├── final_summary.md
├── final_target.patch
├── pr_body.md
└── FAILURE.md          (仅失败时)
```

---

## 故障排查

### AI 后端不可用
安装 opencode CLI 或设置 `AI_BACKEND=claude`，或 `SKIP_AI_ANALYSIS=true` 手动处理。

### LLVM 编译失败
检查 `llvm_build.log`。确认 `LLVM_PROJECT_PATH` 和 `LLVM_INSTALL_PREFIX_SYNC` 正确。

### 测试持续 OOM
- 减少并行度: `NUM_PROCS=8 ta-kickoff`
- OOM 是瞬时资源问题，工作流会自动降并发重跑
- 参考 `reference/npu-oom-handling.md`

### PR 创建失败
- 确认 `gh auth status` 已登录
- 确认 `GITHUB_REPO` 格式正确
