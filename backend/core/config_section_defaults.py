"""统一配置存储的内置默认值（单一事实源）
Built-in defaults for the unified config store (single source of truth).

内容由 config/strategies.yaml 与 config/labels.yaml 逐值迁移而来（2026-08-16，
统一配置存储改造 S1）。迁移后两个 yaml 文件不再被运行时读取，本模块成为
strategy.* / label.* 十个配置节的默认值来源；用户自定义存于 app_config 表
的节键中，读取时经叶子级深度合并覆盖此处默认。

sakura_memory 死配置验证结论（grep backend/ 全量验证）：
context_enhancement.sakura_memory 并非死配置，存在 4 个运行时消费方，整块
保留且值维持 yaml 原值：
- backend/services/sakura_memory_service.py（_get_config 读取 reflection/
  issue_reflection/consolidation/initialization/directory_convention 子键）
- backend/services/sakura_knowledge_extractor.py
- backend/services/ai_reviewer/tools/sakura_tool.py
- backend/services/agent_team/tools/sakura_docs_tool.py

注意 / NOTE:
- 本模块常量是共享默认值，禁止任何调用方原地修改（读取请经
  backend/core/config_sections.py 的深拷贝出口）。
"""

# ====================================================================
# strategies.yaml 迁移 / migrated from strategies.yaml
# ====================================================================

STRATEGY_SECTION_DEFAULTS: dict = {
    # ---- 审查策略分级：quick/standard/deep/large 四档 ----
    "strategies": {
        # 小改动策略
        "quick": {
            "name": "⚡️ 快速审查",
            "conditions": {"max_files": 10, "max_lines": 5000},
            "prompt": """\
Focus on syntax errors, obvious functional bugs, confirmed security issues,
and regressions in the changed behavior. Keep the review concise and evidence-based.
""",
        },
        # 标准审查策略
        "standard": {
            "name": "🔍 标准审查",
            "conditions": {"max_files": 50, "max_lines": 20000},
            "prompt": """\
Review correctness, security, error handling, compatibility, performance,
and maintainability. Prioritize concrete defects over stylistic preferences.
""",
        },
        # 深度审查策略
        "deep": {
            "name": "🔬 深度审查",
            "conditions": {"max_files": 500, "max_lines": 100000},
            "prompt": """\
Perform a deep architectural and behavioral review. Trace cross-file effects,
concurrency and data-flow risks, security boundaries, failure handling, and regressions.
""",
        },
        # 超大 PR 策略
        "large": {
            "name": "📋 汇总报告",
            "conditions": {"max_files": 999999, "max_lines": 99999999},
            "prompt": """\
Review this large PR by first mapping the changed modules and highest-risk areas.
Inspect representative and critical files, report confirmed defects, and note material
coverage limitations in the summary without inventing findings.
""",
        },
    },

    # ---- 文件过滤规则 ----
    "file_filters": {
        # 跳过的文件类型
        "skip_extensions": [
            ".lock",
            ".gitignore",
            ".dockerignore",
            "LICENSE",
        ],
        # 跳过的文件路径
        "skip_paths": [
            "node_modules/",
            "vendor/",
            ".git/",
            "dist/",
            "build/",
            "__pycache__/",
            ".venv/",
            "venv/",
        ],
        # 代码文件扩展名（文档与配置类文本文件自 3.0.0 重构起长期纳入审查范围）
        "code_extensions": [
            ".py",
            ".js",
            ".ts",
            ".tsx",
            ".jsx",
            ".go",
            ".java",
            ".cpp",
            ".c",
            ".h",
            ".cs",
            ".php",
            ".rb",
            ".rs",
            ".swift",
            ".kt",
            ".scala",
            ".sh",
            ".sql",
            ".css",
            ".scss",
            ".html",
            ".vue",
            ".svelte",
            ".yaml",
            ".yml",
            ".md",
            ".json",
            ".toml",
            ".cfg",
            ".ini",
            ".txt",
            "Dockerfile",
            "Makefile",
        ],
    },

    # ---- 上下文增强配置 ----
    "context_enhancement": {
        # 是否启用项目结构展示
        "enable_project_structure": True,
        # 项目结构最大文件数
        "max_structure_files": 1000,
        # 是否启用 AI 函数工具（让 AI 自主查看文件）
        "enable_ai_tools": True,
        # 单个文件最大读取大小（字节），500KB
        "max_file_size": 500000,
        # deep 策略下包含的最大文件数（按变更量排序取前 N 个）
        "max_files_for_deep_strategy": 500,
        # 单文件最大读取行数（超过则截断并提示 AI 使用行范围读取）
        "max_file_lines": 3000,
        # 搜索匹配时的默认上下文行数（匹配行前后各显示多少行）
        "default_context_lines": 20,
        # 搜索匹配时的最大上下文行数
        "max_context_lines": 500,
        # 跨文件搜索工具配置
        "search_in_files": {
            # 优先使用 GitHub Search API
            "use_search_api": True,
            # 默认上下文行数
            "default_context_lines": 50,
            # 默认最大结果数
            "default_max_results": 20,
            # 是否跳过二进制文件
            "skip_binary": True,
            # 回退模式下最大扫描文件数
            "max_files_to_search": 100,
            # 并发获取文件内容的线程数（过高可能触发 GitHub 次级速率限制）
            "concurrency": 8,
        },
        # Git 工具配置
        "git_tools": {
            # 默认返回分支数量
            "default_branch_count": 20,
            # 默认返回提交数量
            "default_commit_count": 30,
        },
        # 外部 CI 失败注入配置：由 check_run.completed / workflow_job.completed
        # webhook 采集，审查时注入上下文
        "ci_failure_injection": {
            # 是否启用
            "enabled": True,
            # 失败记录保留天数（TTL）
            "retention_days": 7,
            # 单次审查最多注入的失败记录数
            "max_records": 10,
            # 单条失败最多展示的 annotations 数（超出部分计数提示，不截断文本）
            "max_annotations_per_record": 8,
        },
        # .sakura/ 记忆系统配置（有运行时消费方，保留 yaml 原值，见模块 docstring）
        "sakura_memory": {
            # 是否启用记忆系统
            "enabled": True,
            # 反思配置
            "reflection": {
                # 是否启用审查后反思
                "enabled": True,
                # 反思使用的模型（None=使用与审查相同的模型）
                "model": None,
                # 反思 Prompt 模板路径（可选覆盖）
                "prompt_template": None,
                # 反思 prompt 中包含的最大评论条数
                "max_comments": 30,
                # 反思 prompt 中包含的最大变更文件条数
                "max_changed_files": 30,
                # 反思 prompt 中包含的最大新增提交条数
                "max_new_commits": 20,
            },
            # Issue 反思配置
            "issue_reflection": {
                # 是否启用 Issue 分析后反思
                "enabled": True,
                # Issue 反思使用的模型（None=使用与审查相同的模型）
                "model": None,
                # Issue 反思 Prompt 模板路径（可选覆盖）
                "prompt_template": None,
            },
            # 合并配置
            "consolidation": {
                # 触发合并的反思轮数
                "interval": 5,
                # 合并使用的模型（None=使用与审查相同的模型）
                "model": None,
                # memory.md 最大字符数
                "max_memory_chars": 2000,
                # SAKURA.md 最大字符数
                "max_sakura_chars": 3000,
                # 合并后是否清理旧的反思文件
                "cleanup_old_reflections": False,
                # 一个文件失败时是否仍提交另一个
                "partial_commit": True,
            },
            # 知识提取配置
            "knowledge_extraction": {
                # 是否启用自动知识提取
                "enabled": True,
                # 触发提取的最低反思轮数
                "min_reflections": 15,
            },
            # 初始化配置
            "initialization": {
                # 是否自动初始化（新仓库自动创建 .sakura/）
                "auto_init": True,
                # 自动初始化的提交信息
                "init_commit_message": "chore: initialize .sakura/ directory for Sakura AI",
            },
            # 目录结构约定
            "directory_convention": {
                "enabled": True,
                # 初始化时自动创建子目录
                "auto_create_subdirs": True,
                "categories": {
                    "memory": {
                        "description": "审查反思文件 / Reflection files (auto-generated)",
                        "priority": 0,
                        # 反思文件由系统动态生成，不需要占位文件
                        "skip_placeholder": True,
                    },
                    "rules": {
                        "description": "审查规则、编码规范 / Review rules, coding standards",
                        "priority": 1,
                        "placeholder": (
                            "# 审查规则\n\n在此目录下存放项目审查规则和编码规范。\n"
                        ),
                    },
                    "docs": {
                        "description": (
                            "架构文档、设计决策 / Architecture docs, design decisions"
                        ),
                        "priority": 2,
                        "placeholder": (
                            "# 项目文档\n\n在此目录下存放项目架构文档和设计决策。\n"
                        ),
                    },
                    "plans": {
                        "description": (
                            "开发计划、路线图 / Development plans, roadmaps"
                        ),
                        "priority": 3,
                        "placeholder": "# 开发计划\n\n在此目录下存放开发计划和路线图。\n",
                    },
                },
            },
        },
    },

    # ---- 审查批准策略配置 ----
    "review_policy": {
        # 是否启用自动批准功能（建议先设为 false 观察效果）
        "enabled": True,
        # 批准阈值：分数 >= 此值才考虑批准
        "approve_threshold": 8,
        # 阻断阈值：分数 < 此值自动请求变更
        "block_threshold": 4,
        # 是否在存在 Critical 问题时自动请求变更
        "block_on_critical": True,
        # 允许的 Major 问题数量上限
        "max_major_issues": 1,
        # 忽略的文件模式（这些文件不影响评分决策）
        "ignored_patterns": [
            "*.md",
            "LICENSE",
            ".gitignore",
            "*.lock",
            "*.txt",
            "CHANGELOG*",
            "*.yml",
            "*.yaml",
        ],
        # 特定仓库的覆盖配置（可选），例如：
        #   "owner/repo-name": {"approve_threshold": 9, "block_threshold": 5}
        "repo_overrides": {},
        # 幂等性检查：是否检查已有 Review 避免重复提交
        "enable_idempotency_check": True,
        # Review 评论模板（中文，默认）
        "review_templates": {
            "approve": """\
## 🌸 Sakura AI 审查报告 - {strategy_name}

### ✅ **审查通过**

{summary}

## 代码质量评分：{score}/10
### AI审查决策：批准合并

{comment_summary}
""",
            "request_changes": """\
## 🌸 Sakura AI 审查报告 - {strategy_name}

### ❌ **请求变更**

{summary}

## 代码质量评分：{score}/10
### AI审查决策：需要修复后重新提交

**阻断原因**：{decision_reason}

{comment_summary}
""",
            "comment": """\
## 🌸 Sakura AI 审查报告 - {strategy_name}

### 💬 **审查评论**

{summary}

## 代码质量评分：{score}/10
### AI审查决策：请人工复审

{comment_summary}
""",
        },
        # Review 评论模板（英文）
        "review_templates_en": {
            "approve": """\
## 🌸 Sakura AI Review Report - {strategy_name}

### ✅ **Approved**

{summary}

## Code Quality Score: {score}/10
### AI Decision: Approve merge

{comment_summary}
""",
            "request_changes": """\
## 🌸 Sakura AI Review Report - {strategy_name}

### ❌ **Changes Requested**

{summary}

## Code Quality Score: {score}/10
### AI Decision: Changes required before resubmitting

**Block Reason**: {decision_reason}

{comment_summary}
""",
            "comment": """\
## 🌸 Sakura AI Review Report - {strategy_name}

### 💬 **Review Comment**

{summary}

## Code Quality Score: {score}/10
### AI Decision: Manual review requested

{comment_summary}
""",
        },
    },

    # ---- Issue 分析配置 ----
    "issue_analysis": {
        # Issue 分类定义
        "categories": [
            {
                "name": "bug",
                "description": "软件缺陷、功能异常、崩溃",
                "keywords": [
                    "bug",
                    "error",
                    "crash",
                    "broken",
                    "fail",
                    "issue",
                    "wrong",
                    "fix",
                ],
            },
            {
                "name": "feature",
                "description": "新功能请求、功能增强",
                "keywords": [
                    "feature",
                    "request",
                    "add",
                    "support",
                    "implement",
                    "new",
                ],
            },
            {
                "name": "question",
                "description": "使用问题、疑问",
                "keywords": ["question", "how", "help", "why", "what", "?"],
            },
            {
                "name": "documentation",
                "description": "文档改进",
                "keywords": ["doc", "readme", "documentation", "guide", "tutorial"],
            },
            {
                "name": "enhancement",
                "description": "功能增强、改进",
                "keywords": [
                    "enhancement",
                    "improve",
                    "optimize",
                    "upgrade",
                    "refactor",
                ],
            },
            {
                "name": "performance",
                "description": "性能问题",
                "keywords": ["slow", "performance", "speed", "latency", "memory", "leak"],
            },
            {
                "name": "security",
                "description": "安全问题",
                "keywords": [
                    "security",
                    "vulnerability",
                    "xss",
                    "injection",
                    "auth",
                    "permission",
                    "cve",
                ],
            },
            {
                "name": "refactor",
                "description": "代码重构、技术债清理",
                "keywords": [
                    "refactor",
                    "cleanup",
                    "tech debt",
                    "deprecation",
                    "rewrite",
                ],
            },
            {
                "name": "other",
                "description": "其他无法归类的 Issue",
                "keywords": [],
            },
        ],
        # Issue 优先级判定规则
        "priority_rules": {
            "critical": {
                "keywords": [
                    "urgent",
                    "critical",
                    "blocker",
                    "production",
                    "down",
                    "data loss",
                    "security",
                ]
            },
            "high": {
                "keywords": [
                    "important",
                    "should",
                    "need",
                    "affect",
                    "impact",
                    "breaking",
                ]
            },
            "medium": {"keywords": ["moderate", "should", "affect some"]},
            "low": {
                "keywords": [
                    "minor",
                    "nice to have",
                    "low priority",
                    "suggestion",
                    "cosmetic",
                ]
            },
        },
        # PR-Issue 关联关键词
        "issue_reference_keywords": [
            "fixes",
            "fix",
            "closes",
            "close",
            "resolves",
            "resolve",
            "addresses",
            "related to",
            "refs",
            "ref",
        ],
        # 关联 Issue 数量上限
        "max_linked_issues_in_prompt": 5,
        # Issue 分析系统提示词（英文 focus，强化型契约由代码注入）
        "system_prompt": """\
Analyze the GitHub issue against the repository code base. Establish concrete
evidence with tools before judging category, priority, and feasibility, then
recommend labels, assignees, milestone, duplicate detection, and a normalized
title when the original is unclear.

## Analysis focus
- Classify the issue precisely; prefer the most specific applicable category.
- Estimate priority from user-reported impact and reproduction evidence, not wording alone.
- Assess feasibility by inspecting the affected code paths and their complexity.
- Recommend labels only from the available repository labels, each with a confidence and reason.
- Recommend assignees only from repository collaborators who plausibly own the affected area.
- Suggest a duplicate only when you can cite the other issue number with evidence.
- Suggest a normalized title only when the original title is unclear or malformed.
""",
        # 自动评论模板（中文，默认）
        "comment_template": """\
## 🌸 Sakura AI Issue 分析报告

### 📋 分析结果
- **分类**: {category}
- **优先级**: {priority}
- **可行性**: {feasibility}
{suggested_title_section}

### 📝 摘要
{summary}

### 🏷️ 建议标签
{labels}

### 👥 建议指派人
{assignees}

{related_info}

---
*此评论由 [Sakura AI](https://github.com/Sakura520222/Sakura-AI) 自动生成。*
""",
        # 自动评论模板（英文）
        "comment_template_en": """\
## 🌸 Sakura AI Issue Analysis Report

### 📋 Analysis Result
- **Category**: {category}
- **Priority**: {priority}
- **Feasibility**: {feasibility}
{suggested_title_section}

### 📝 Summary
{summary}

### 🏷️ Suggested Labels
{labels}

### 👥 Suggested Assignees
{assignees}

{related_info}

---
*This comment was generated automatically by [Sakura AI](https://github.com/Sakura520222/Sakura-AI).*
""",
    },

    # ---- PR 总结配置 ----
    "pr_summary": {
        "system_prompt": """\
你是专业的代码审查助手，擅长总结代码变更。请根据提供的 PR 变更信息，生成简洁清晰的总结。
要求：
- 使用中文
- 结构化输出：变更概览、主要改动列表、影响范围
- 突出重要变更（新增功能、重构、Bug修复等）
- 保持简洁，不超过 300 字
""",
        "user_template": """\
请总结以下 PR 的变更内容：

PR 标题: {title}
变更文件数: {file_count}
代码变更: +{additions}/-{deletions}

变更文件列表:
{file_list}

Commit 信息:
{commits}
""",
    },

    # ---- PR 依赖图配置（mode 等运行时开关在动态配置组，此处仅模板） ----
    "pr_dependency_graph": {
        "system_prompt": """\
你是代码依赖分析专家。根据提供的 PR 变更文件及其 import/模块引用信息，生成 Mermaid 语法描述的依赖关系图。

要求：
- 使用中文标注节点（文件短名），保持简洁可读
- 只包含有实际依赖关系的文件，不要孤立节点
- 使用 graph TD（从上到下）方向
- PR 变更文件使用实线连接，变更文件对外部模块的依赖使用虚线连接（style ... stroke-dasharray: 5 5）
- 节点总数不超过 {max_nodes} 个
- 输出纯 Mermaid 代码块，不要额外解释
""",
        "user_template": """\
请根据以下 PR 变更信息生成依赖关系图：

PR 标题: {title}
总文件数: {file_count}  代码文件数: {code_file_count}  本轮分析文件数: {analyzed_file_count}

文件依赖关系:
{import_context}

请生成 Mermaid graph TD 语法的依赖图。只展示文件间的直接依赖关系。
如果变更文件之间没有任何依赖关系，请输出文字说明"该 PR 的变更文件之间没有明显的模块依赖关系"。
""",
    },
}

# ====================================================================
# labels.yaml 迁移 / migrated from labels.yaml
# ====================================================================

LABEL_SECTION_DEFAULTS: dict = {
    # ---- 标签定义：当仓库没有标签时使用的默认标签 ----
    "labels": {
        "bug": {"color": "d73a4a", "description": "Something isn't working"},
        "documentation": {
            "color": "0075ca",
            "description": "Improvements or additions to documentation",
        },
        "duplicate": {
            "color": "cfd3d7",
            "description": "This issue or pull request already exists",
        },
        "enhancement": {"color": "a2eeef", "description": "New feature or request"},
        "good first issue": {"color": "7057ff", "description": "Good for newcomers"},
        "help wanted": {
            "color": "008672",
            "description": "Extra attention is needed",
        },
        "invalid": {"color": "e4e669", "description": "This doesn't seem right"},
        "question": {
            "color": "d876e3",
            "description": "Further information is requested",
        },
        "wontfix": {"color": "ffffff", "description": "This will not be worked on"},
        "refactor": {
            "color": "fbca04",
            "description": "Code refactoring (non-functional change)",
        },
        "performance": {"color": "5319e7", "description": "Performance optimization"},
        "test": {"color": "bfd4f2", "description": "Test related changes"},
        "dependencies": {"color": "0366d6", "description": "Dependency updates"},
        "ci": {"color": "ffefdb", "description": "CI/CD configuration changes"},
        "style": {"color": "c5def5", "description": "Code style adjustments"},
        "build": {"color": "ededed", "description": "Build system changes"},
    },

    # ---- AI 标签推荐行为控制 ----
    "recommendation": {
        "enabled": True,
        "confidence_threshold": 0.7,
        "auto_create": True,
    },

    # ---- 标签冲突规则：当 PR 已有 key 中的标签时，不会自动添加 value 列表中的标签。
    # 用于增量审查时避免基于增量 diff 内容推荐与 PR 整体意图矛盾的标签。 ----
    "conflict_rules": {
        # 新功能 PR 的增量修复不应打 bug 标签
        "enhancement": ["bug"],
        # 重构 PR 的增量修复不应打 bug 标签
        "refactor": ["bug"],
        # 文档 PR 不应打 bug/enhancement 标签
        "documentation": ["bug", "enhancement"],
        # 测试 PR 不应打 bug 标签
        "test": ["bug"],
        # 样式 PR 不应打 bug/enhancement 标签
        "style": ["bug", "enhancement"],
    },
}
