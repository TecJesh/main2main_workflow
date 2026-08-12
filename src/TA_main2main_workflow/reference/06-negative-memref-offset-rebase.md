# Negative memref offset — pointer rebase（禁止 maxsi 夹紧）

> 适用：`test_neg_index*` 失败、或报错  
> `expected offsets to be non-negative, but got -N`  
> 出现在 `TritonToLinalg` / 内嵌 `Canonicalizer` 之后。

## 一句话结论

**线性 offset 总和 `< 0` 时，把负偏移吸收进指针地址（rebase），再 `reinterpret_cast offset:[0]`。**  
**禁止** `arith.maxsi(neg, 0)` 把负 offset 夹成 0；那只过 verifier，**地址语义错**。

---

## 错误信号

```text
error: expected offsets to be non-negative, but got -6
[`TritonToLinalg` on 'builtin.module', `Canonicalizer` on 'builtin.module']
```

典型用例：`third_party/ascend/unittest/pytest_ut/test_neg_index.py`  
`tl.load(in_ptr + ((-NEG) + arange), mask=(i>=NEG), other=0)`。

---

## 根因（勿判成 “verifier 变严”）

1. 旧 `NegOffsetElim`：`Attribute(-N)` → `%c-N : index`（仍是负常量）。
2. LLVM 3.7+ `ReinterpretCastOpConstantFolder`（[#163505](https://github.com/llvm/llvm-project/pull/163505)）把 `%c-N` 折回 `staticOffsets=[-N]`。
3. `ViewLikeInterface` 拒绝负 **static** offset（3.6/3.7 规则相同）。

3.6 能过是因为没有 ConstantFolder，不是负索引语义在 3.6 不同。

---

## 正确修复（专家方案）

**文件**：`third_party/ascend/lib/TritonToLinalg/BlockPtrAnalysis.cpp`  
**函数**：`BlockDataParser::rewriteAddPtr`  
（IntToPtr→`pointer_cast` 之后，`createCastOp` 之前）

```text
linear = Σ getConstantIntValue(offsets[i])   // 有动态维则跳过
if linear 存在 && linear < 0:
    指针前进 linear 个元素（一次，不是按维循环）
    所有 offsets = 0
    再 createCastOp → reinterpret_cast offset:[0]
```

地址前进两条路径：

| 分支 | 条件 | 做法 |
|---|---|---|
| A | source 已是 `hivm.pointer_cast` | `addrs[0] + linear * elemBytes`（i64） |
| B | 普通 memref（函数参） | `extract_aligned_pointer_as_index` → 字节加减 → `index_cast` → i64 |

然后：`hivm.hir.pointer_cast(%addr) : memref<?xT>`（**动态 `?` 基址**；定长 tile 由后续 `reinterpret_cast sizes` 给出）。

等价变换：

```text
base + (i + S) , S<0
  ≡  (base advanced by S elems) + i
```

有加有减但 **总和 ≥ 0**：**不要改**（本来合法）。

---

## 禁止的错误修复

### ❌ maxsi 夹紧（曾被 AI 采用，错误）

```cpp
// inferBlockOffset 里
retOffset = maxsi(constant(-6), constant(0));  // → 0
```

| | maxsi 夹 0 | rebase |
|---|---|---|
| verifier | 过 | 过 |
| `out[6]`（NEG=6） | `in[6]`（**错**） | `in[0]`（对） |
| 含义 | 丢掉负 base | 负 base 并进指针 |

### ❌ 仅 Attribute→`%c-N`（NegOffsetElim）

3.7 ConstantFolder 会再折回负 static → 编译失败。

### ❌ 负 `reinterpret_cast` / `subview` offset

同样撞 ViewLike verifier。

---

## 回归测试（改完必须过）

- Pytest：`test_neg_index.py::test_neg_index_load`  
  `test_mixed_static_index_load`（`(8,2)` 和负 / `(4,10)` 和不负）
- Lit：`unittest/Conversion/General/TritonToLinalg/test_load_with_neg_index.mlir`  
  Branch B（函数参 extract）、Branch A（int_to_ptr）、zero-offset 不 rebase

数值参考：`out[index:] = in[:n-index]`（不是错误的 `out[index:]=in[:index]`，除非 `n==2*index`）。

---

## 详细背景

升级 PR 描述（可选精读）：仓库外文档或团队 `升级PR描述/0811_Neg_offset.md`。
