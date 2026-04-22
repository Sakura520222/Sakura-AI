`
# Sakura AI Reviewer 项目概述

## 1. 项目简介与技术栈
基于大语言模型的智能 GitHub 代码审查与 Issue 分析机器人，具备跨文件依赖理解、全仓库扫描和项目记忆能力。
- **技术栈**：Python 3.11+ / FastAPI / MySQL & Redis / LLM & RAG / Docker / GitHub App & Telegram Bot

## 2. 架构设计与关键决策
采用后端分层架构，前后端同仓：
- `backend/api/`：路由；`core/`：配置与依赖注入；`models/`：数据结构
- `backend/services/`：核心业务（AI审查、记忆系统、RAG等）
- `backend/workers/`：异步任务；`telegram/`：Bot交互；`webui/`：前端
- **关键决策**：业务规则 YAML 外部化热更新；耗时任务 Worker 隔离；容器化交付。

## 3. 已知问题与注意事项
### PR194 遗留缺陷（未闭环）
- **`get_git_tree` 未过滤符号链接**：连续6轮未修复，已触发历史遗留阻断但decision映射未执行（规范缺陷已补丁），根因是适配器契约不完整
- **`search_files_tool.py` 回退链断裂(critical)**：`except ImportError`中`raise NotImplementedError`导致本地适配器下搜索100%失败，原`pass`可用但被"美化即破坏"
- **`local_repo_adapter.py` 适配器契约不完整**：缺少`sha`、`url`、`size`等属性，6轮热点文件，持续补边缘漏洞而非补齐契约
- **`decoded_content` 急加载副作用**：异常时机前移改变传播路径，未追溯调用方兼容性
- **`_REF_PATTERN` 创可贴式修复**：正则过滤输入未从设计层面定义适配器边界
- **`get_contents` 多态语义未保持**：PyGithub 目录返回列表、文件返回单个对象
- **适配器方法覆盖不全**：`get_branches()`、`get_pull()`等可能缺失
- **`_detect_default_branch` 静默回退 "main"**：入口处静默失败致扫描无意义

### PR194 已修复
- ~~路径遍历漏洞~~：三层符号链接防护（单文件检查→目录遍历逐项→`os.walk(followlinks=False)`）✅
- ~~符号链接目录遍历漏洞(`get_contents`)~~ ✅
- ~~ref 参数静默忽略~~ ✅
- ~~`repo_name.split("/", 1)` 无防御~~ ✅

### PR190 遗留缺陷（未闭环）
- **`github_write_service.py: