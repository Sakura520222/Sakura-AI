# 项目记忆

累计反思 16 次

## 核心审查原则

- **"无评论"≠"无问题"**：空结果可能源于工具故障；"无问题"结论须附验证依据（全库搜索命令、CI run 链接）
- **高评分警惕确认偏误**：高分审查仍须做负向用例验证（如依赖升级的默认行为变化）
- **结论逻辑自洽**："阻断合并"必须对应 error/major 标签
- **最简单变更须最严格审查**：仅两行依赖变更也宜用 full/medium 策略，quick 易漏检
- **Fail-Closed**：宁可误报不可漏报；文档不可作验证依据
- **审查报告结构化**：摘要→关键风险→修复建议，阻断项须在摘要阶段可见

## 依赖与锁文件（高频主题）

- **锁文件同步=阻断项**：改 pyproject/requirements 必须同 PR 更新 uv.lock；CI 强制 `uv lock --check`，漂移标 error 而非 minor
- **CI 与发布链路一致**：CI 用 pip install -r 而发布用 uv.lock 会"CI 过、发布挂"；统一 `uv sync --locked`
- **双清单镜像自动校验**：pyproject↔requirements 用脚本比对；子项目依赖下限须与根项目一致防版本漂移
- **死依赖检测**：全库零调用须注明保留意图或删除；但"无导入点≠可删除"，须查 CI/Dockerfile/迁移脚本等非代码消费点
- **文档硬编码版本同步**：README/docs/CI 示例中的依赖约束须与声明文件比对
- **升级必附 Release Notes**：核对 Breaking Changes；关键库升级后跑兼容性回归测试
- **约束与 wheel**：关键库建议加上限（>=X,<Y）；升级前验证新 Python 版本 wheel 可用性（pip download --only-binary）
- **工具限制**：大文件（如 uv.lock）超搜索上限时改用分块读取，勿因此跳过验证

## 核心模块与并发

- **无界循环须物理硬约束**：while True 保留 timeout/max_iterations
- **状态写入幂等与并发**：双写清理原子化；补偿扫描唯一约束/锁，分页+退避
- **迁移脚本幂等**+回退路径测试；nullable 回退分支须有测试
- **配置项须有真实消费点**；生产代码 Fixture 风格命名警惕代码错位

## 安全与错误处理

- **异常脱敏分级**：USER_INPUT_ERROR 可透传，INTERNAL_ERROR 脱敏；禁止裸 except
- **跨语言链路易遗漏**：UDS 权限、容器用户映射、digest 锁定、--cap-drop

## Issue 分析

- **优先级纳入影响范围与恢复成本**：数据永久缺失类升 high
- **重复检测仅靠关键字会误/漏报**；低置信度标签二次审查
- **AI 审查改进**：高风险场景提示词结构化；adversarial pass 仅高风险文件触发；历史 PR 构建回归套件
