# 2026-09 知识提取总结（PR567~PR577 批次）

## 提取范围
16 篇最新反思：PR567（全量 + incr2/3/4）、ISSUE568-571、PR572（+incr2）、PR573-PR577 依赖管理系列。

## 新建知识文件
- **rules/lockfile_dependency_rules.md**：锁文件同步（error 级）、双轨解析消除、子项目防漂移、死依赖双向规则、wheel 可用性预检、统一升级脚本。
- **docs/dependency_management_architecture.md**：依赖声明结构（三份声明 + 子项目）、锁文件决策、三镜像漂移对策、venv 双向隔离。
- **plans/PR567_PR577_series_lessons.md**：证据纪律、语法 gate、全局副作用、资源防线、通知清理、深度审查能力建设、跨语言安全、工具限制元教训。

## 主题演进（相对 PR441-456 批次）
1. 依赖审查从"CHANGELOG 核对"进化到"锁文件全链路一致性"——漂移从 suggestion 升级为 error。
2. CI 与发布双轨解析（pip vs uv）被识别为"CI 过、发布挂"的结构性风险。
3. 多子项目（sandboxer/updater）版本下限一致成为新审查维度。
4. "零导入 ≠ 可删"与"零调用须注明保留意图"构成死依赖双向规则。
5. Python 3.14 迁移引入 wheel 可用性预检（`pip download --only-binary`）。

## 高频失误模式
- 锁文件不随依赖声明同步（PR575/576/577 连续出现）。
- 全量重审"无评论"无验证依据即给 10/10（PR567）。
- 环境变量注入只看增量两文件，未查全局读取点与加载顺序（PR567 incr4）。
- 语法级错误（`except A,B:`）由全库搜索才发现，测试因 import 失败根本没跑（PR572）。

## 后续建议
- CI 增加 `uv lock --check` + 全库 py_compile 两个 merge-gate。
- 建立标签字典（LABELS.md）与历史 PR 审查回归套件。
- 审查工具支持大文件分块读取，消除验证盲区。
