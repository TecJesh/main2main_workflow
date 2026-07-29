# 单步模式工作流

```mermaid
flowchart TD
    A["ta-kickoff"] --> B["Phase 0: Prepare<br/>克隆/配置 repo、remotes"]
    B --> C["Phase 1: Detect<br/>检测待合并的上游 commits"]
    C -->|无新 commit| D["Done: Already Up-to-Date"]
    C -->|有新 commit| E["Phase 2: Plan<br/>按行数预算切分步骤"]
    E --> F["Phase 3: Per-Step Loop"]

    subgraph STEP["每个步骤 (while current_step < total_steps)"]
        F1["Step A: Merge<br/>git merge upstream commits"]
        F1 --> F2{有冲突?}
        F2 -->|是| F3["Step B: Resolve<br/>AI 解决冲突 (max_retries)"]
        F3 -->|未解决| FAIL["UpgradeFailed"]
        F3 -->|已解决| F4
        F2 -->|否| F4{LLVM 版本变更?}

        F4 -->|是| F5["Step C: IR Patch Pipeline<br/>Phase 1: 编译适配<br/>Phase 2: IR 补丁生成+测试"]
        F4 -->|否| F6["Step C: Build + Fix Loop<br/>编译 - AI 修复 - 重编译"]

        F5 --> F7{IR 补丁通过?}
        F7 -->|否| FAIL
        F7 -->|是| F8

        F6 --> F8{编译通过?}
        F8 -->|否| FAIL
        F8 -->|是| F9["Step D: Test + Fix Loop<br/>pytest - OOM 重跑 - AI 修复"]

        F9 --> F10{测试通过?}
        F10 -->|否| FAIL
        F10 -->|是| F11["Step E: Commit<br/>提交步骤进度"]

        F11 --> F12["current_step += 1"]
        F12 -->|还有步骤| F1
        F12 -->|全部完成| G
    end

    STEP --> G["Phase 4: Finalize<br/>生成 summary + cumulative patch"]

    G -->|PUSH_TO_GITHUB=true| H["Push + Create PR"]
    G -->|PUSH_TO_GITHUB=false| I["Done: work branch 保留"]

    style FAIL fill:#d73,stroke:#333
    style D fill:#4a9,stroke:#333
    style I fill:#4a9,stroke:#333
    style H fill:#4a9,stroke:#333
```

## IR Patch Pipeline (LLVM hash 变更时)

```mermaid
flowchart TD
    subgraph IR["IR Patch Pipeline"]
        P1["Phase 1: 编译适配"]
        P1A["Build clean LLVM"] --> P1B["Build TA"]
        P1B --> P1C{编译通过?}
        P1C -->|否| P1D["AI 修复 (只改 third_party/ascend/)<br/>AscendNPU-IR 专项文档"]
        P1D -->|retries 耗尽| IR_FAIL["IR Pipeline Failed"]
        P1D --> P1B
        P1C -->|是| P2["Phase 2: IR 补丁循环"]

        P2A["OP 分析 + LLVM 变更分析"] --> P2B["AI 生成 IR 补丁"]
        P2B --> P2C["应用补丁 + 编译 LLVM<br/>(失败时 AI 修复, 最多 10 次)"]
        P2C -->|成功| P2D["Build TA + pytest"]
        P2C -->|10 次耗尽| IR_FAIL
        P2D -->|通过| IR_PASS["Pipeline Passed"]
        P2D -->|失败| P2F["诊断: IR vs 代码"]
        P2F -->|IR 问题| P2G["重新生成补丁 (最多 3 次)"]
        P2G --> P2C
        P2F -->|代码问题| P2H["AI 修复 → Rebuild → 重测"]
        P2H --> P2D
    end

    style IR_FAIL fill:#d73,stroke:#333
    style IR_PASS fill:#4a9,stroke:#333
```

## 修复代码评价机制

```
AI 修复
  ↓
Layer 1: AI 自检 (prompt.md step 6)
  └─ 检查文件路径 → 不在允许目录则自行回退
  ↓
Layer 2: 代码校验 (validate_fix)
  └─ 硬检查 → 不通过则 git revert + 反馈 + 不消耗 attempt
  ↓
Layer 3: 实际验证
  └─ 编译/测试结果 → 失败则继续 fix loop
```

详见 [fix-validation-flow.md](fix-validation-flow.md)

## 环境变量速查

### 仓库 & 分支
| 变量 | 默认值 | 说明 |
|------|--------|------|
| `TRITON_ASCEND_PATH` | (当前目录) | triton-ascend 本地路径 |
| `TRITON_PATH` | (空) | 上游 triton 本地路径 |
| `TRITON_TARGET_COMMIT` | (upstream HEAD) | 要合并的目标 commit |
| `TA_BASE_BRANCH` | `upstream_sync` | 基线分支名 |
| `TA_WORK_BRANCH_BASE` | `upstream-ascend` | 工作分支 base remote |

### 构建 & 测试
| 变量 | 默认值 | 说明 |
|------|--------|------|
| `BUILD_PROCS` / `MAX_JOBS` | 32 | 并行编译数（`MAX_JOBS` 为旧名，向后兼容） |
| `TEST_PROCS` / `NUM_PROCS` | 8 | 并行 pytest 数（`NUM_PROCS` 为旧名） |
| `LLVM_PROJECT_PATH` | `~/llvm-project` | LLVM 源码路径 |
| `LLVM_INSTALL_PREFIX_SYNC` | `~/llvm-install-sync` | LLVM 安装路径 |
| `LLVM_INSTALL_PREFIX` | (空) | LLVM 安装前缀（优先级高于上面） |
| `SKIP_BUILD` | `false` | 跳过编译 |
| `SKIP_E2E_TEST` | `false` | 跳过测试 |
| `SKIP_BASELINE_LLVM` | `false` | 跳过基线 LLVM 编译（已有 LLVM 时使用） |
| `SKIP_LLVM_REBUILD` | `false` | 跳过 LLVM 版本变更时的重编译 |

### 测试目录（可插拔）
| 变量 | 默认值 | 说明 |
|------|--------|------|
| `TA_TEST_DIR` | `third_party/ascend/unittest/pytest_ut` | 主测试目录 |
| `TA_EXTRA_TEST_DIRS` | (空) | 额外 pytest 目录，逗号/空格分隔 |
| `TA_TEST_COMMAND` | (空) | 自定义测试命令（在 pytest ut 之后额外执行） |

### AI & 重试
| 变量 | 默认值 | 说明 |
|------|--------|------|
| `AI_BACKEND` | `auto` | AI 后端（`opencode` / `claude`） |
| `SKIP_AI_ANALYSIS` | `false` | 跳过 AI 调用 |
| `TA_MAX_RETRIES` | 10 | AI 修复最大重试次数 |
| `TA_LINE_BUDGET` | 1000 | 每步最大源码行数 |

### PR
| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PUSH_TO_GITHUB` | `false` | 自动创建 PR |
| `GITHUB_REPO` | `triton-lang/triton-ascend` | PR 目标仓库 |

### 其他
| 变量 | 默认值 | 说明 |
|------|--------|------|
| `TA_SINGLE_STEP_MODE` | `true` | 单步模式 |
| `TA_RESUME` | `false` | 从缓存恢复（跳过已完成的步骤） |
| `PYTHON` | `python3` | Python 解释器 |
| `CONDA_ENV` | `ta-upgrade` | Conda 环境名 |
| `TA_MAIN2MAIN_WORKSPACE` | `./workspace` | 工作区目录 |

## CLI 参数

```
ta-kickoff [选项]

  --triton-ascend-path PATH     triton-ascend 仓库路径
  --triton-path PATH            上游 triton 仓库路径
  --target-commit SHA           目标 commit
  --llvm-prefix PATH            LLVM 安装前缀
  --conda-env NAME              Conda 环境名
  --build-procs N               并行编译数
  --test-procs N                并行测试数
  --extra-test-dirs DIRS        额外测试目录（逗号分隔）
  --test-command CMD            自定义测试命令
```
