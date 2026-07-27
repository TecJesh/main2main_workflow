# 编译与测试修复代码的评价和测试机制

## 总体流程

```mermaid
flowchart TD
    subgraph BUILD["编译修复循环 (pipeline/build.py)"]
        B1["Build TA"] --> B2{编译通过?}
        B2 -->|是| B_DONE["build_passed=true"]
        B2 -->|否| B3["AI 修复 (ai_fix)"]
        B3 --> B4["代码评价 (validate_fix)"]
        B4 --> B5{校验通过?}
        B5 -->|否| B6["回退修改 + 写入拒绝原因"]
        B6 --> B7["不消耗 attempt 次数"]
        B7 --> B3
        B5 -->|是| B8["记录本次 fix attempt"]
        B8 --> B9{attempt <= max?}
        B9 -->|是| B1
        B9 -->|否| B_FAIL["build_passed=false"]
    end

    subgraph TEST["测试修复循环 (pipeline/test.py)"]
        T1["Run pytest"] --> T2{测试通过?}
        T2 -->|是| T_DONE["test_passed=true"]
        T2 -->|否| T3{"OOM 检测?"}
        T3 -->|是| T4["降并发重跑 (最多5次)"]
        T4 --> T5{OOM 消失?}
        T5 -->|是| T6["继续正常修复"]
        T5 -->|否| T_FAIL["test_passed=false"]
        T3 -->|否| T6
        T6 --> T7["AI 修复 (ai_fix)"]
        T7 --> T8["代码评价 (validate_fix)"]
        T8 --> T9{校验通过?}
        T9 -->|否| T10["回退修改 + 写入拒绝原因"]
        T10 --> T11["不消耗 attempt 次数"]
        T11 --> T7
        T9 -->|是| T12["Rebuild TA"]
        T12 --> T13{编译通过?}
        T13 -->|否| T14["attempt += 1"]
        T14 --> T15{attempt <= max?}
        T15 -->|是| T7
        T15 -->|否| T_FAIL
        T13 -->|是| T1
    end

    B_DONE --> NEXT["下一步"]
    B_FAIL --> TERMINATE["流程终止"]
    T_DONE --> NEXT
    T_FAIL --> TERMINATE
```

## 代码评价机制 (validate_fix)

```mermaid
flowchart TD
    A["AI 修复完成"] --> B["获取 modified_files 列表"]
    B --> C{"有修改文件?"}
    C -->|否| REJECT["❌ 拒绝: No files modified"]
    C -->|是| D["逐个检查文件路径"]
    D --> E{"文件在 third_party/ascend/ 下?"}
    E -->|全部是| PASS["✅ 校验通过<br/>记录 attempt"]
    E -->|有文件在外面| F["记录非法文件列表"]
    F --> G["git checkout -- ."]
    G --> H["git clean -fd"]
    H --> I["写入 fix_rejection.txt<br/>包含拒绝原因 + 允许的路径"]
    I --> J["追加到 fix_errors 列表<br/>(AI 下次修复会看到)"]
    J --> K["❌ 拒绝: 不消耗 attempt<br/>continue 重新修复"]

    style PASS fill:#4a9,stroke:#333
    style REJECT fill:#d73,stroke:#333
    style K fill:#d73,stroke:#333
```

## AI 自检机制 (prompt.md step 6)

```mermaid
flowchart LR
    subgraph AI_SELF["AI 修复时自检 (prompt.md)"]
        S1["Step 6: SELF-REVIEW before returning"]
        S2["列出每个修改的文件"]
        S3{"文件在 third_party/ascend/ 下?<br/>(测试修复也可在 python/triton_ascend/)"}
        S3 -->|否| S4["在返回前 REVERT 该修改"]
        S4 --> S3
        S3 -->|是| S5{"修复针对根因<br/>而非掩盖错误?"}
        S5 -->|否| S4
        S5 -->|是| S6["返回修改"]
    end

    style S4 fill:#d73,stroke:#333
    style S6 fill:#4a9,stroke:#333
```

## 双层防护总结

| 层级 | 位置 | 机制 | 失败处理 |
|------|------|------|----------|
| **第1层** | AI 自检 (prompt.md) | AI 提交前自查文件路径 + 根因 | 自行回退后重新修复 |
| **第2层** | 代码校验 (fix.py) | 硬检查 modified_files 路径 | git revert + 反馈文件 + 不消耗 attempt |
| **第3层** | 编译/测试验证 | 实际 build/test 结果 | 编译/测试失败 → 继续 fix loop |

## 关键设计决策

1. **拒绝不消耗 attempt**：修复被拒后 `continue` 在同一 attempt 重新修复（while 循环），确保 AI 有完整的 max_retries 次有效尝试
2. **拒绝后回退代码**：`git checkout -- .` + `git clean -fd` 清除所有修改，下一轮从干净状态开始
3. **拒绝反馈传递给 AI**：`fix_rejection.txt` 追加到 `fix_errors` 列表，AI 下次修复时在 error_logs 中看到
4. **OOM 单独处理**：OOM 不调用 AI，自动降并发重跑（test_procs 减半），与代码修复完全独立
