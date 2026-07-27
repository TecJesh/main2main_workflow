# 单步模式工作流

```mermaid
flowchart TD
    A["ta-kickoff"] --> B["Phase 0: Prepare"]
    B --> C["Phase 1: Detect"]
    C -->|无新 commit| D["Done"]
    C -->|有新 commit| E["Phase 2: Plan"]
    E --> F["Phase 3: Per-Step Loop"]

    subgraph STEP["每个步骤"]
        F1["Merge"] --> F2{冲突?}
        F2 -->|是| F3["AI Resolve"]
        F2 -->|否| F4{LLVM 变更?}
        F3 --> F4
        F4 -->|是| F5["IR Patch Pipeline"]
        F4 -->|否| F6["Build + Fix"]
        F5 --> F7{通过?}
        F6 --> F7
        F7 -->|否| FAIL["UpgradeFailed"]
        F7 -->|是| F8["Test + Fix"]
        F8 -->|失败| FAIL
        F8 -->|通过| F9["Commit"]
        F9 --> F10["current_step += 1"]
    end

    STEP --> G["Phase 4: Finalize"]
    G -->|push| PR["Create PR"]

    style FAIL fill:#d73
    style D fill:#4a9
```

## IR Patch Pipeline（LLVM hash 变更时）

```mermaid
flowchart TD
    MERGE["Merge 后发现 LLVM hash 变更"] --> P1["1. 切换到目标 LLVM commit<br/>确保工作区干净"]
    P1 --> P2["2. 直接应用现有补丁<br/>llvm_patch_f6ded0b.patch"]
    P2 --> P3{应用成功?}
    P3 -->|否| P4["AI 分析失败原因<br/>根据当前 LLVM commit 调整补丁"]
    P4 --> P2
    P3 -->|是| P5["3. 编译 LLVM<br/>(编译失败则 AI 修复补丁)"]
    P5 --> P6{编译成功?}
    P6 -->|否| P7["AI 根据编译错误修复补丁"]
    P7 --> P5
    P6 -->|是| P8["4. 编译 TA + AI 修复编译错误"]
    P8 --> P9["5. Run pytest"]
    P9 --> P10{测试通过?}
    P10 -->|是| DONE["Done"]
    P10 -->|否| P11["AI 分类: IR 问题 vs 代码问题"]
    P11 -->|IR 问题| P12["AI 在现有补丁基础上<br/>补充缺失 OP 的适配<br/>(不重新生成)"]
    P12 --> P13["重新编译 LLVM"]
    P13 --> P8
    P11 -->|代码问题| P14["AI 修复代码 -> Rebuild"]
    P14 --> P9

    style DONE fill:#4a9
```

## 关键变化（vs 旧流程）

| 旧流程 | 新流程 |
|--------|--------|
| 先做 OP 分析 + 变更分析 | 直接应用现有补丁 |
| AI 从零生成补丁 | AI 在现有补丁上调整/补充 |
| 分析阶段在补丁之前 | 分析阶段在测试发现问题后 |
| 每次重新生成完整补丁 | 保留已有内容，只补充缺失 OP |

## 修复代码评价机制

```
AI 修复
  |
Layer 1: AI 自检 (prompt.md step 6)
  |-- 检查文件路径 -> 不在允许目录则自行回退
  |
Layer 2: 代码校验 (validate_fix)
  |-- 硬检查 -> 不通过则 git revert + 反馈 + 不消耗 attempt
  |
Layer 3: 实际验证
  |-- 编译/测试结果 -> 失败则继续 fix loop
```

详见 [fix-validation-flow.md](fix-validation-flow.md)
