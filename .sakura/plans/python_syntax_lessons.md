# Python 语法一致性经验教训

## 版本基线（首要前提）

本项目目标运行时为 **Python 3.14+**。判断"语法错误"或"Python 2 残留"时，必须以 3.14 语法规则为准——语言演进会重新合法化旧写法，把已合法的语法误报为 Python 2 错误会稀释真实缺陷信号。

> **历史更正（2026-08-12）**：早期版本将 `except Exception, e:` 作为"Python 2 语法错误"的典型案例。该判断在 Python 3.14 已不成立（见下文 PEP 758）。

## except 逗号语法：PEP 758 已合法化（不可误报）

PEP 758（Final，2025-03-14，Python 3.14）重新允许 `except` / `except*` 的**无括号逗号分隔异常类型列表**，**仅在不使用 `as` 子句时**生效：

```python
except ExceptionA, ExceptionB:          # ✅ 3.14 合法
except* ExceptionA, ExceptionB:         # ✅ 3.14 合法
except (ExceptionA, ExceptionB):        # ✅ 一直合法
except (ExceptionA, ExceptionB) as e:   # ✅ 带 as 时必须加括号
except ExceptionA, ExceptionB as e:     # ❌ SyntaxError：带 as 时必须括号
```

**审查铁律**：不得把 `except A, B:` 形式的无括号逗号写法当作 Python 2 残留误报。

### `except Exception, e:` 的语义陷阱（不再是 SyntaxError）

由于 PEP 758，`except Exception, e:` 在 Python 3.14 **不再是语法错误**，会被解析为"捕获 `Exception` 或名字 `e` 指向的类型"——把 `e` 当作第二个异常类型，而**不会**按 Python 2 语义绑定异常对象。后果：

- 模块能正常导入，**不会"阻断导入链"**
- 绑定意图静默失效；`e` 若未定义在运行时抛 `NameError`
- 审查时应建议改为 `except Exception as e:`，但**理由是"语义偏移、绑定不发生"**，不是"Python 2 语法错误"

## 真正在 Python 3.14 仍是 SyntaxError 的 Python 2 写法

以下可放心标记为缺陷：

- **`print` 语句**（`print "x"`）→ 必须使用 `print()` 函数
- **带 `as` 的无括号多类型逗号写法**（`except A, B as e:`）→ 必须 `except (A, B) as e:`

## 名字层面的 Python 2 残留（NameError，非 SyntaxError）

这些名字在 Python 3 不是内置，引用是**运行时 NameError**（不是编译期 SyntaxError），仍属缺陷但性质不同，不应归为"语法错误"：

- `xrange` → `range()`
- `raw_input` → `input()`
- `unicode` / `basestring` / `iteritems()` 等 → 用 Python 3 对应替代

## 风格问题（合法，不得列为语法缺陷）

- `%` 旧式字符串格式化在 Python 3.14 完全合法；仅作为可选风格建议时提及 f-string / `str.format()`。

## 强制规则

### 语法检查
- 任何"Python 2 语法"断言，先按 3.14 运行时归类：SyntaxError / 合法但语义偏移 / 名字层 NameError / 风格。
- 误报合法语法（如 PEP 758 逗号写法）会污染审查可信度。

### 导入链验证
- 基础模块必须能被正常导入
- 1-2 个直接依赖模块必须能正常导入
- 检查是否存在循环导入

### 测试有效性
- 测试环境 Python 版本与生产一致
- 验证测试是否真的加载了被测模块
- 语法错误不应导致测试框架本身失败

## 审查优先级

**业务逻辑与运行时语义优先于"语法风格"标签。** 真正的 SyntaxError 高于业务逻辑错误；但合法语法的"语义偏移"问题同样需要关注，且更隐蔽。

## 经验总结

1. **"Python 2 语法"是会过时的判断**：语言演进会重新合法化旧写法（PEP 758 对 except 逗号语法）。审查结论必须标注适用的 Python 版本基线。
2. **区分 SyntaxError 与语义偏移**：`except Exception, e:` 在 3.14 不是 SyntaxError，而是静默语义变化——比语法错误更难发现。
3. **简单错误可能阻断整个系统**：基础模块的真正 SyntaxError 会产生放射性影响（原结论，适用于确实非法的写法）。
4. **测试通过不等于代码正确**：语法错误可能让测试框架无法加载模块。
5. **自动化检查需要人工复核**：linter / AI 的"Python 2 语法"判断必须按当前运行时版本核验。
