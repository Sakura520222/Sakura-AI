
sakura-ai-reviewer  | 2026-05-15 06:10:55 | INFO     | backend.api.webhook:handle_github_webhook - 收到GitHub事件: pull_request
sakura-ai-reviewer  | 2026-05-15 06:10:55 | INFO     | backend.core.github_app:extract_pr_info_from_webhook - 成功提取PR信息: Sakura520222/Sakura-AI-Reviewer#305
sakura-ai-reviewer  | 2026-05-15 06:10:55 | INFO     | backend.services.telegram_service:check_and_consume_quota - 管理员/超级管理员跳过配额检查: Sakura520222 (role: super_admin)
sakura-ai-reviewer  | 2026-05-15 06:10:56 | INFO     | backend.telegram.notifications:send_review_start - ✅ 发送审查开始通知: Sakura520222/Sakura-AI-Reviewer#305 → 1 人
sakura-ai-reviewer  | 2026-05-15 06:10:56 | INFO     | backend.api.webhook:handle_pull_request_event - 已提交审查任务: Sakura520222/Sakura-AI-Reviewer#305, 任务标识: Sakura520222/Sakura-AI-Reviewer#305
sakura-ai-reviewer  | INFO:     104.23.213.144:0 - "POST /api/webhook/github HTTP/1.1" 200 OK
sakura-ai-reviewer  | 2026-05-15 06:10:56 | INFO     | backend.workers.review_worker:_get_review_semaphore - 审查并发信号量初始化: 最大 3 个并发任务
sakura-ai-reviewer  | 2026-05-15 06:10:56 | INFO     | backend.workers.review_worker:process_review_task - [f3ddfcb6] 开始处理审查任务: Sakura520222/Sakura-AI-Reviewer#305
sakura-ai-reviewer  | 2026-05-15 06:10:56 | WARNING  | backend.core.github_app:get_repo_client - Integration为空，尝试重新创建...
sakura-ai-reviewer  | 2026-05-15 06:10:56 | INFO     | backend.core.github_app:_create_integration - 开始创建GitHub Integration, App ID: 2973872 (类型: str)
sakura-ai-reviewer  | 2026-05-15 06:10:56 | INFO     | backend.core.github_app:_create_integration - 正在创建GithubIntegration实例...
sakura-ai-reviewer  | 2026-05-15 06:10:56 | INFO     | backend.core.github_app:_create_integration - ✓ GitHub Integration创建成功, App ID: 2973872
sakura-ai-reviewer  | 2026-05-15 06:10:57 | INFO     | backend.core.github_app:get_repo_client - ✓ 成功获取仓库 Sakura520222/Sakura-AI-Reviewer 的访问令牌
sakura-ai-reviewer  | 2026-05-15 06:10:58 | INFO     | backend.services.pr_analyzer:_analyze_pr_sync - 开始分析PR: Sakura520222/Sakura-AI-Reviewer#305
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines - 🔍 开始解析 backend/core/config.py 的 patch
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   📦 Hunk #1: 原文件第481-486行 → PR后第481-495行
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   ✓ Hunk #1 解析完成: +9 -0 行, 包含6行上下文, PR后行号范围: 481-495
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   📦 Hunk #2: 原文件第960-965行 → PR后第969-984行
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   ✓ Hunk #2 解析完成: +10 -0 行, 包含6行上下文, PR后行号范围: 969-984
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   📦 Hunk #3: 原文件第972-979行 → PR后第991-1007行
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   ✓ Hunk #3 解析完成: +9 -0 行, 包含8行上下文, PR后行号范围: 991-1007
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   📦 Hunk #4: 原文件第1127-1132行 → PR后第1155-1165行
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   ✓ Hunk #4 解析完成: +5 -0 行, 包含6行上下文, PR后行号范围: 1155-1165
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   📦 Hunk #5: 原文件第1263-1268行 → PR后第1296-1310行
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   ✓ Hunk #5 解析完成: +9 -0 行, 包含6行上下文, PR后行号范围: 1296-1310
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines - ✅ 文件 backend/core/config.py 共 5 个 hunk, 提取行号 74 个: [481, 482, 483, 484, 485, 486, 487, 488, 489, 490, 491, 492, 493, 494, 495]...
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines - 🔍 开始解析 backend/models/database.py 的 patch
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   📦 Hunk #1: 原文件第873-878行 → PR后第873-881行
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   ✓ Hunk #1 解析完成: +3 -0 行, 包含6行上下文, PR后行号范围: 873-881
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines - ✅ 文件 backend/models/database.py 共 1 个 hunk, 提取行号 9 个: [873, 874, 875, 876, 877, 878, 879, 880, 881]
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines - 🔍 开始解析 backend/services/agent_team/iteration_loop.py 的 patch
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   📦 Hunk #1: 原文件第59-64行 → PR后第59-66行
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   ✓ Hunk #1 解析完成: +2 -0 行, 包含6行上下文, PR后行号范围: 59-66
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   📦 Hunk #2: 原文件第120-125行 → PR后第122-129行
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   ✓ Hunk #2 解析完成: +2 -0 行, 包含6行上下文, PR后行号范围: 122-129
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines - ✅ 文件 backend/services/agent_team/iteration_loop.py 共 2 个 hunk, 提取行号 16 个: [59, 60, 61, 62, 63, 64, 65, 66, 122, 123, 124, 125, 126, 127, 128]...
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines - 🔍 开始解析 backend/services/agent_team/professional_reviewer.py 的 patch
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   📦 Hunk #1: 原文件第105-113行 → PR后第105-120行
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   ✓ Hunk #1 解析完成: +8 -1 行, 包含8行上下文, PR后行号范围: 105-120
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   📦 Hunk #2: 原文件第126-135行 → PR后第133-148行
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   ✓ Hunk #2 解析完成: +7 -1 行, 包含9行上下文, PR后行号范围: 133-148
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines - ✅ 文件 backend/services/agent_team/professional_reviewer.py 共 2 个 hunk, 提取行号 32 个: [105, 106, 107, 108, 109, 110, 111, 112, 113, 114, 115, 116, 117, 118, 119]...
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines - 🔍 开始解析 backend/services/agent_team/tools/registry.py 的 patch
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   📦 Hunk #1: 原文件第19-24行 → PR后第19-29行
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   ✓ Hunk #1 解析完成: +5 -0 行, 包含6行上下文, PR后行号范围: 19-29
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   📦 Hunk #2: 原文件第51-56行 → PR后第56-64行
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   ✓ Hunk #2 解析完成: +3 -0 行, 包含6行上下文, PR后行号范围: 56-64
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines - ✅ 文件 backend/services/agent_team/tools/registry.py 共 2 个 hunk, 提取行号 20 个: [19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 56, 57, 58, 59]...
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines - 🔍 开始解析 backend/services/agent_team/tools/sakura_docs_tool.py 的 patch
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   📦 Hunk #1: 原文件第0--1行 → PR后第1-299行
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   ✓ Hunk #1 解析完成: +299 -0 行, 包含0行上下文, PR后行号范围: 1-299
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines - ✅ 文件 backend/services/agent_team/tools/sakura_docs_tool.py 共 1 个 hunk, 提取行号 299 个: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]...
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines - 🔍 开始解析 backend/services/agent_team/tools/sakura_memory_tool.py 的 patch
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   📦 Hunk #1: 原文件第0--1行 → PR后第1-161行
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   ✓ Hunk #1 解析完成: +161 -0 行, 包含0行上下文, PR后行号范围: 1-161
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines - ✅ 文件 backend/services/agent_team/tools/sakura_memory_tool.py 共 1 个 hunk, 提取行号 161 个: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]...
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines - 🔍 开始解析 backend/services/ai_reviewer/constants.py 的 patch
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   📦 Hunk #1: 原文件第69-75行 → PR后第69-75行
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   ✓ Hunk #1 解析完成: +1 -1 行, 包含6行上下文, PR后行号范围: 69-75
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   📦 Hunk #2: 原文件第429-434行 → PR后第429-459行
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   ✓ Hunk #2 解析完成: +25 -0 行, 包含6行上下文, PR后行号范围: 429-459
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   📦 Hunk #3: 原文件第441-446行 → PR后第466-472行
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   ✓ Hunk #3 解析完成: +1 -0 行, 包含6行上下文, PR后行号范围: 466-472
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   📦 Hunk #4: 原文件第493-498行 → PR后第519-525行
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   ✓ Hunk #4 解析完成: +1 -0 行, 包含6行上下文, PR后行号范围: 519-525
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines - ✅ 文件 backend/services/ai_reviewer/constants.py 共 4 个 hunk, 提取行号 52 个: [69, 70, 71, 72, 73, 74, 75, 429, 430, 431, 432, 433, 434, 435, 436]...
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines - 🔍 开始解析 backend/services/ai_reviewer/prompt_builder.py 的 patch
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   📦 Hunk #1: 原文件第164-170行 → PR后第164-178行
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   ✓ Hunk #1 解析完成: +9 -1 行, 包含6行上下文, PR后行号范围: 164-178
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   📦 Hunk #2: 原文件第192-197行 → PR后第200-206行
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   ✓ Hunk #2 解析完成: +1 -0 行, 包含6行上下文, PR后行号范围: 200-206
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines - ✅ 文件 backend/services/ai_reviewer/prompt_builder.py 共 2 个 hunk, 提取行号 22 个: [164, 165, 166, 167, 168, 169, 170, 171, 172, 173, 174, 175, 176, 177, 178]...
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines - 🔍 开始解析 backend/services/ai_reviewer/tools/handler.py 的 patch
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   📦 Hunk #1: 原文件第174-179行 → PR后第174-188行
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   ✓ Hunk #1 解析完成: +9 -0 行, 包含6行上下文, PR后行号范围: 174-188
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines - ✅ 文件 backend/services/ai_reviewer/tools/handler.py 共 1 个 hunk, 提取行号 15 个: [174, 175, 176, 177, 178, 179, 180, 181, 182, 183, 184, 185, 186, 187, 188]
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines - 🔍 开始解析 backend/services/ai_reviewer/tools/sakura_tool.py 的 patch
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   📦 Hunk #1: 原文件第133-138行 → PR后第133-189行
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   ✓ Hunk #1 解析完成: +51 -0 行, 包含6行上下文, PR后行号范围: 133-189
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines - ✅ 文件 backend/services/ai_reviewer/tools/sakura_tool.py 共 1 个 hunk, 提取行号 57 个: [133, 134, 135, 136, 137, 138, 139, 140, 141, 142, 143, 144, 145, 146, 147]...
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines - 🔍 开始解析 backend/services/issue_analyzer.py 的 patch
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   📦 Hunk #1: 原文件第250-258行 → PR后第250-264行
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   ✓ Hunk #1 解析完成: +8 -2 行, 包含7行上下文, PR后行号范围: 250-264
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines - ✅ 文件 backend/services/issue_analyzer.py 共 1 个 hunk, 提取行号 15 个: [250, 251, 252, 253, 254, 255, 256, 257, 258, 259, 260, 261, 262, 263, 264]
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines - 🔍 开始解析 backend/services/sakura_consolidation_agent.py 的 patch
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   📦 Hunk #1: 原文件第0--1行 → PR后第1-618行
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   ✓ Hunk #1 解析完成: +618 -0 行, 包含0行上下文, PR后行号范围: 1-618
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines - ✅ 文件 backend/services/sakura_consolidation_agent.py 共 1 个 hunk, 提取行号 618 个: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]...
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines - 🔍 开始解析 backend/services/sakura_knowledge_extractor.py 的 patch
sakura-ai-reviewer  | 2026-05-15 06:10:59 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   📦 Hunk #1: 原文件第0--1行 → PR后第1-529行
sakura-ai-reviewer  | 2026-05-15 06:11:00 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   ✓ Hunk #1 解析完成: +529 -0 行, 包含0行上下文, PR后行号范围: 1-529
sakura-ai-reviewer  | 2026-05-15 06:11:00 | INFO     | backend.services.pr_analyzer:_extract_changed_lines - ✅ 文件 backend/services/sakura_knowledge_extractor.py 共 1 个 hunk, 提取行号 529 个: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]...
sakura-ai-reviewer  | 2026-05-15 06:11:00 | INFO     | backend.services.pr_analyzer:_extract_changed_lines - 🔍 开始解析 backend/services/sakura_memory_service.py 的 patch
sakura-ai-reviewer  | 2026-05-15 06:11:00 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   📦 Hunk #1: 原文件第161-167行 → PR后第161-173行
sakura-ai-reviewer  | 2026-05-15 06:11:00 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   ✓ Hunk #1 解析完成: +7 -1 行, 包含6行上下文, PR后行号范围: 161-173
sakura-ai-reviewer  | 2026-05-15 06:11:00 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   📦 Hunk #2: 原文件第182-188行 → PR后第188-200行
sakura-ai-reviewer  | 2026-05-15 06:11:00 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   ✓ Hunk #2 解析完成: +7 -1 行, 包含6行上下文, PR后行号范围: 188-200
sakura-ai-reviewer  | 2026-05-15 06:11:00 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   📦 Hunk #3: 原文件第446-451行 → PR后第458-476行
sakura-ai-reviewer  | 2026-05-15 06:11:00 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   ✓ Hunk #3 解析完成: +13 -0 行, 包含6行上下文, PR后行号范围: 458-476
sakura-ai-reviewer  | 2026-05-15 06:11:00 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   📦 Hunk #4: 原文件第878-887行 → PR后第903-912行
sakura-ai-reviewer  | 2026-05-15 06:11:00 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   ✓ Hunk #4 解析完成: +3 -3 行, 包含7行上下文, PR后行号范围: 903-912
sakura-ai-reviewer  | 2026-05-15 06:11:00 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   📦 Hunk #5: 原文件第899-1003行 → PR后第924-1014行
sakura-ai-reviewer  | 2026-05-15 06:11:00 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   ✓ Hunk #5 解析完成: +58 -72 行, 包含33行上下文, PR后行号范围: 924-1014
sakura-ai-reviewer  | 2026-05-15 06:11:00 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   📦 Hunk #6: 原文件第1016-1030行 → PR后第1027-1157行
sakura-ai-reviewer  | 2026-05-15 06:11:00 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   ✓ Hunk #6 解析完成: +117 -1 行, 包含14行上下文, PR后行号范围: 1027-1157
sakura-ai-reviewer  | 2026-05-15 06:11:00 | INFO     | backend.services.pr_analyzer:_extract_changed_lines - ✅ 文件 backend/services/sakura_memory_service.py 共 6 个 hunk, 提取行号 277 个: [161, 162, 163, 164, 165, 166, 167, 168, 169, 170, 171, 172, 173, 188, 189]...
sakura-ai-reviewer  | 2026-05-15 06:11:00 | INFO     | backend.services.pr_analyzer:_extract_changed_lines - 🔍 开始解析 backend/webui/routes/__init__.py 的 patch
sakura-ai-reviewer  | 2026-05-15 06:11:00 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   📦 Hunk #1: 原文件第20-25行 → PR后第20-26行
sakura-ai-reviewer  | 2026-05-15 06:11:00 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   ✓ Hunk #1 解析完成: +1 -0 行, 包含6行上下文, PR后行号范围: 20-26
sakura-ai-reviewer  | 2026-05-15 06:11:00 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   📦 Hunk #2: 原文件第47-50行 → PR后第48-52行
sakura-ai-reviewer  | 2026-05-15 06:11:00 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   ✓ Hunk #2 解析完成: +1 -0 行, 包含4行上下文, PR后行号范围: 48-52
sakura-ai-reviewer  | 2026-05-15 06:11:00 | INFO     | backend.services.pr_analyzer:_extract_changed_lines - ✅ 文件 backend/webui/routes/__init__.py 共 2 个 hunk, 提取行号 12 个: [20, 21, 22, 23, 24, 25, 26, 48, 49, 50, 51, 52]
sakura-ai-reviewer  | 2026-05-15 06:11:00 | INFO     | backend.services.pr_analyzer:_extract_changed_lines - 🔍 开始解析 backend/webui/routes/sakura_memory.py 的 patch
sakura-ai-reviewer  | 2026-05-15 06:11:00 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   📦 Hunk #1: 原文件第0--1行 → PR后第1-512行
sakura-ai-reviewer  | 2026-05-15 06:11:00 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   ✓ Hunk #1 解析完成: +512 -0 行, 包含0行上下文, PR后行号范围: 1-512
sakura-ai-reviewer  | 2026-05-15 06:11:00 | INFO     | backend.services.pr_analyzer:_extract_changed_lines - ✅ 文件 backend/webui/routes/sakura_memory.py 共 1 个 hunk, 提取行号 512 个: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]...
sakura-ai-reviewer  | 2026-05-15 06:11:00 | INFO     | backend.services.pr_analyzer:_extract_changed_lines - 🔍 开始解析 backend/webui/templates/components/sidebar.html 的 patch
sakura-ai-reviewer  | 2026-05-15 06:11:00 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   📦 Hunk #1: 原文件第152-157行 → PR后第152-165行
sakura-ai-reviewer  | 2026-05-15 06:11:00 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   ✓ Hunk #1 解析完成: +8 -0 行, 包含6行上下文, PR后行号范围: 152-165
sakura-ai-reviewer  | 2026-05-15 06:11:00 | INFO     | backend.services.pr_analyzer:_extract_changed_lines - ✅ 文件 backend/webui/templates/components/sidebar.html 共 1 个 hunk, 提取行号 14 个: [152, 153, 154, 155, 156, 157, 158, 159, 160, 161, 162, 163, 164, 165]
sakura-ai-reviewer  | 2026-05-15 06:11:00 | INFO     | backend.services.pr_analyzer:_extract_changed_lines - 🔍 开始解析 backend/webui/templates/config_strategies.html 的 patch
sakura-ai-reviewer  | 2026-05-15 06:11:00 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   📦 Hunk #1: 原文件第230-236行 → PR后第230-236行
sakura-ai-reviewer  | 2026-05-15 06:11:00 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   ✓ Hunk #1 解析完成: +1 -1 行, 包含6行上下文, PR后行号范围: 230-236
sakura-ai-reviewer  | 2026-05-15 06:11:00 | INFO     | backend.services.pr_analyzer:_extract_changed_lines - ✅ 文件 backend/webui/templates/config_strategies.html 共 1 个 hunk, 提取行号 7 个: [230, 231, 232, 233, 234, 235, 236]
sakura-ai-reviewer  | 2026-05-15 06:11:00 | INFO     | backend.services.pr_analyzer:_extract_changed_lines - 🔍 开始解析 backend/webui/templates/sakura_memory.html 的 patch
sakura-ai-reviewer  | 2026-05-15 06:11:00 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   📦 Hunk #1: 原文件第0--1行 → PR后第1-171行
sakura-ai-reviewer  | 2026-05-15 06:11:00 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   ✓ Hunk #1 解析完成: +171 -0 行, 包含0行上下文, PR后行号范围: 1-171
sakura-ai-reviewer  | 2026-05-15 06:11:00 | INFO     | backend.services.pr_analyzer:_extract_changed_lines - ✅ 文件 backend/webui/templates/sakura_memory.html 共 1 个 hunk, 提取行号 171 个: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]...
sakura-ai-reviewer  | 2026-05-15 06:11:00 | INFO     | backend.services.pr_analyzer:_extract_changed_lines - 🔍 开始解析 backend/workers/agent_team_worker.py 的 patch
sakura-ai-reviewer  | 2026-05-15 06:11:00 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   📦 Hunk #1: 原文件第106-112行 → PR后第106-112行
sakura-ai-reviewer  | 2026-05-15 06:11:00 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   ✓ Hunk #1 解析完成: +1 -1 行, 包含6行上下文, PR后行号范围: 106-112
sakura-ai-reviewer  | 2026-05-15 06:11:00 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   📦 Hunk #2: 原文件第115-123行 → PR后第115-125行
sakura-ai-reviewer  | 2026-05-15 06:11:00 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   ✓ Hunk #2 解析完成: +3 -1 行, 包含8行上下文, PR后行号范围: 115-125
sakura-ai-reviewer  | 2026-05-15 06:11:00 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   📦 Hunk #3: 原文件第379-389行 → PR后第381-397行
sakura-ai-reviewer  | 2026-05-15 06:11:00 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   ✓ Hunk #3 解析完成: +8 -2 行, 包含9行上下文, PR后行号范围: 381-397
sakura-ai-reviewer  | 2026-05-15 06:11:00 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   📦 Hunk #4: 原文件第393-401行 → PR后第401-415行
sakura-ai-reviewer  | 2026-05-15 06:11:00 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   ✓ Hunk #4 解析完成: +7 -1 行, 包含8行上下文, PR后行号范围: 401-415
sakura-ai-reviewer  | 2026-05-15 06:11:00 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   📦 Hunk #5: 原文件第405-411行 → PR后第419-425行
sakura-ai-reviewer  | 2026-05-15 06:11:00 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   ✓ Hunk #5 解析完成: +1 -1 行, 包含6行上下文, PR后行号范围: 419-425
sakura-ai-reviewer  | 2026-05-15 06:11:00 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   📦 Hunk #6: 原文件第418-429行 → PR后第432-443行
sakura-ai-reviewer  | 2026-05-15 06:11:00 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   ✓ Hunk #6 解析完成: +2 -2 行, 包含10行上下文, PR后行号范围: 432-443
sakura-ai-reviewer  | 2026-05-15 06:11:00 | INFO     | backend.services.pr_analyzer:_extract_changed_lines - ✅ 文件 backend/workers/agent_team_worker.py 共 6 个 hunk, 提取行号 69 个: [106, 107, 108, 109, 110, 111, 112, 115, 116, 117, 118, 119, 120, 121, 122]...
sakura-ai-reviewer  | 2026-05-15 06:11:00 | INFO     | backend.services.pr_analyzer:_extract_changed_lines - 🔍 开始解析 backend/workers/scan_worker.py 的 patch
sakura-ai-reviewer  | 2026-05-15 06:11:00 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   📦 Hunk #1: 原文件第536-544行 → PR后第536-550行
sakura-ai-reviewer  | 2026-05-15 06:11:00 | INFO     | backend.services.pr_analyzer:_extract_changed_lines -   ✓ Hunk #1 解析完成: +8 -2 行, 包含7行上下文, PR后行号范围: 536-550
sakura-ai-reviewer  | 2026-05-15 06:11:00 | INFO     | backend.services.pr_analyzer:_extract_changed_lines - ✅ 文件 backend/workers/scan_worker.py 共 1 个 hunk, 提取行号 15 个: [536, 537, 538, 539, 540, 541, 542, 543, 544, 545, 546, 547, 548, 549, 550]
sakura-ai-reviewer  | 2026-05-15 06:11:00 | INFO     | backend.services.pr_analyzer:_extract_changed_lines - 🎯 构建 Diff 安全区完成，覆盖 22 个文件
sakura-ai-reviewer  | 2026-05-15 06:11:00 | INFO     | backend.services.pr_analyzer:_analyze_pr_sync - PR分析完成: Sakura520222/Sakura-AI-Reviewer#305, 文件数: 22, 变更行数: 2809, 策略: deep
sakura-ai-reviewer  | 2026-05-15 06:11:01 | INFO     | backend.services.code_vector_store:_init_client - ✅ 代码向量存储初始化成功: ./data/chroma
sakura-ai-reviewer  | 2026-05-15 06:11:02 | INFO     | backend.services.embedding_service:_init_client - ✅ 嵌入服务初始化成功: siliconflow (BAAI/bge-m3)
sakura-ai-reviewer  | 2026-05-15 06:11:02 | INFO     | backend.workers.review_worker:process_review_task - [f3ddfcb6] 开始代码索引...
sakura-ai-reviewer  | 2026-05-15 06:11:02 | INFO     | backend.services.pr_code_indexer:index_pr_changes - 开始索引PR #305的代码变更，仓库: Sakura520222/Sakura-AI-Reviewer
sakura-ai-reviewer  | 2026-05-15 06:11:02 | INFO     | backend.core.github_app:get_repo_client - ✓ 成功获取仓库 Sakura520222/Sakura-AI-Reviewer 的访问令牌
sakura-ai-reviewer  | INFO:     127.0.0.1:38252 - "GET /health HTTP/1.1" 200 OK
sakura-ai-reviewer  | 2026-05-15 06:11:21 | INFO     | backend.services.code_index_service:index_pr_changes - 开始索引PR #305的代码文件，仓库: Sakura520222/Sakura-AI-Reviewer
sakura-ai-reviewer  | 2026-05-15 06:11:22 | INFO     | backend.services.code_vector_store:delete_by_file - ✅ 已删除文件 backend/core/config.py 的 139 个代码块
sakura-ai-reviewer  | 2026-05-15 06:11:25 | INFO     | backend.services.code_vector_store:upsert_code_chunks - ✅ 已更新 143 个代码块到 Sakura520222/Sakura-AI-Reviewer 的代码向量库
sakura-ai-reviewer  | 2026-05-15 06:11:25 | INFO     | backend.services.code_vector_store:delete_by_file - ✅ 已删除文件 backend/models/database.py 的 83 个代码块
sakura-ai-reviewer  | 2026-05-15 06:11:26 | INFO     | backend.services.code_vector_store:upsert_code_chunks - ✅ 已更新 84 个代码块到 Sakura520222/Sakura-AI-Reviewer 的代码向量库
sakura-ai-reviewer  | 2026-05-15 06:11:26 | INFO     | backend.services.code_vector_store:delete_by_file - ✅ 已删除文件 backend/services/agent_team/iteration_loop.py 的 15 个代码块
sakura-ai-reviewer  | 2026-05-15 06:11:27 | INFO     | backend.services.code_vector_store:upsert_code_chunks - ✅ 已更新 16 个代码块到 Sakura520222/Sakura-AI-Reviewer 的代码向量库
sakura-ai-reviewer  | 2026-05-15 06:11:27 | INFO     | backend.services.code_vector_store:delete_by_file - ✅ 已删除文件 backend/services/agent_team/professional_reviewer.py 的 23 个代码块
sakura-ai-reviewer  | 2026-05-15 06:11:27 | INFO     | backend.services.code_vector_store:upsert_code_chunks - ✅ 已更新 24 个代码块到 Sakura520222/Sakura-AI-Reviewer 的代码向量库
sakura-ai-reviewer  | 2026-05-15 06:11:27 | INFO     | backend.services.code_vector_store:delete_by_file - ✅ 已删除文件 backend/services/agent_team/tools/registry.py 的 12 个代码块
sakura-ai-reviewer  | 2026-05-15 06:11:27 | INFO     | backend.services.code_vector_store:upsert_code_chunks - ✅ 已更新 12 个代码块到 Sakura520222/Sakura-AI-Reviewer 的代码向量库
sakura-ai-reviewer  | 2026-05-15 06:11:28 | INFO     | backend.services.code_vector_store:upsert_code_chunks - ✅ 已更新 25 个代码块到 Sakura520222/Sakura-AI-Reviewer 的代码向量库
sakura-ai-reviewer  | 2026-05-15 06:11:28 | INFO     | backend.services.code_vector_store:upsert_code_chunks - ✅ 已更新 12 个代码块到 Sakura520222/Sakura-AI-Reviewer 的代码向量库
sakura-ai-reviewer  | 2026-05-15 06:11:28 | INFO     | backend.services.code_vector_store:delete_by_file - ✅ 已删除文件 backend/services/ai_reviewer/constants.py 的 27 个代码块
sakura-ai-reviewer  | 2026-05-15 06:11:29 | INFO     | backend.services.code_vector_store:upsert_code_chunks - ✅ 已更新 28 个代码块到 Sakura520222/Sakura-AI-Reviewer 的代码向量库
sakura-ai-reviewer  | 2026-05-15 06:11:29 | INFO     | backend.services.code_vector_store:delete_by_file - ✅ 已删除文件 backend/services/ai_reviewer/prompt_builder.py 的 34 个代码块
sakura-ai-reviewer  | 2026-05-15 06:11:29 | INFO     | backend.services.code_vector_store:upsert_code_chunks - ✅ 已更新 35 个代码块到 Sakura520222/Sakura-AI-Reviewer 的代码向量库
sakura-ai-reviewer  | 2026-05-15 06:11:29 | INFO     | backend.services.code_vector_store:delete_by_file - ✅ 已删除文件 backend/services/ai_reviewer/tools/handler.py 的 15 个代码块
sakura-ai-reviewer  | 2026-05-15 06:11:30 | INFO     | backend.services.code_vector_store:upsert_code_chunks - ✅ 已更新 16 个代码块到 Sakura520222/Sakura-AI-Reviewer 的代码向量库
sakura-ai-reviewer  | 2026-05-15 06:11:30 | INFO     | backend.services.code_vector_store:delete_by_file - ✅ 已删除文件 backend/services/ai_reviewer/tools/sakura_tool.py 的 18 个代码块
sakura-ai-reviewer  | 2026-05-15 06:11:30 | INFO     | backend.services.code_vector_store:upsert_code_chunks - ✅ 已更新 21 个代码块到 Sakura520222/Sakura-AI-Reviewer 的代码向量库
sakura-ai-reviewer  | 2026-05-15 06:11:30 | INFO     | backend.services.code_vector_store:delete_by_file - ✅ 已删除文件 backend/services/issue_analyzer.py 的 33 个代码块
sakura-ai-reviewer  | 2026-05-15 06:11:31 | INFO     | backend.services.code_vector_store:upsert_code_chunks - ✅ 已更新 34 个代码块到 Sakura520222/Sakura-AI-Reviewer 的代码向量库
sakura-ai-reviewer  | 2026-05-15 06:11:31 | INFO     | backend.services.code_vector_store:upsert_code_chunks - ✅ 已更新 38 个代码块到 Sakura520222/Sakura-AI-Reviewer 的代码向量库
sakura-ai-reviewer  | 2026-05-15 06:11:32 | INFO     | backend.services.code_vector_store:upsert_code_chunks - ✅ 已更新 36 个代码块到 Sakura520222/Sakura-AI-Reviewer 的代码向量库
sakura-ai-reviewer  | 2026-05-15 06:11:32 | INFO     | backend.services.code_vector_store:delete_by_file - ✅ 已删除文件 backend/services/sakura_memory_service.py 的 84 个代码块
sakura-ai-reviewer  | 2026-05-15 06:11:33 | INFO     | backend.services.code_vector_store:upsert_code_chunks - ✅ 已更新 91 个代码块到 Sakura520222/Sakura-AI-Reviewer 的代码向量库
sakura-ai-reviewer  | 2026-05-15 06:11:33 | INFO     | backend.services.code_vector_store:delete_by_file - ✅ 已删除文件 backend/webui/routes/__init__.py 的 3 个代码块
sakura-ai-reviewer  | 2026-05-15 06:11:34 | INFO     | backend.services.code_vector_store:upsert_code_chunks - ✅ 已更新 4 个代码块到 Sakura520222/Sakura-AI-Reviewer 的代码向量库
sakura-ai-reviewer  | 2026-05-15 06:11:34 | INFO     | backend.services.code_vector_store:upsert_code_chunks - ✅ 已更新 30 个代码块到 Sakura520222/Sakura-AI-Reviewer 的代码向量库
sakura-ai-reviewer  | 2026-05-15 06:11:34 | INFO     | backend.services.code_vector_store:delete_by_file - ✅ 已删除文件 backend/workers/agent_team_worker.py 的 38 个代码块
sakura-ai-reviewer  | 2026-05-15 06:11:35 | INFO     | backend.services.code_vector_store:upsert_code_chunks - ✅ 已更新 39 个代码块到 Sakura520222/Sakura-AI-Reviewer 的代码向量库
sakura-ai-reviewer  | 2026-05-15 06:11:35 | INFO     | backend.services.code_vector_store:delete_by_file - ✅ 已删除文件 backend/workers/scan_worker.py 的 62 个代码块
sakura-ai-reviewer  | 2026-05-15 06:11:36 | INFO     | backend.services.code_vector_store:upsert_code_chunks - ✅ 已更新 62 个代码块到 Sakura520222/Sakura-AI-Reviewer 的代码向量库
sakura-ai-reviewer  | 2026-05-15 06:11:36 | INFO     | backend.services.code_index_service:index_pr_changes - PR #305 索引完成: 索引=19, 跳过=0, 失败=0, 删除=0, 代码块=750
sakura-ai-reviewer  | 2026-05-15 06:11:36 | INFO     | backend.services.pr_code_indexer:index_pr_changes - PR #305 代码索引完成: 索引=19, 跳过=0, 失败=0, 代码块=750
sakura-ai-reviewer  | 2026-05-15 06:11:36 | INFO     | backend.workers.review_worker:process_review_task - [f3ddfcb6] 代码索引完成
sakura-ai-reviewer  | 2026-05-15 06:11:36 | INFO     | backend.services.vector_store:_init_client - ✅ ChromaDB 客户端初始化成功: ./data/chroma
sakura-ai-reviewer  | 2026-05-15 06:11:36 | INFO     | backend.services.embedding_service:_init_client - ✅ 重排序服务初始化成功: siliconflow (BAAI/bge-reranker-v2-m3)
sakura-ai-reviewer  | 2026-05-15 06:11:36 | INFO     | backend.workers.review_worker:process_review_task - [f3ddfcb6] 仓库尚无文档索引，开始自动索引 .sakura/ 文档...
sakura-ai-reviewer  | 2026-05-15 06:11:37 | INFO     | backend.core.github_app:get_repo_client - ✓ 成功获取仓库 Sakura520222/Sakura-AI-Reviewer 的访问令牌
sakura-ai-reviewer  | 2026-05-15 06:11:42 | INFO     | backend.services.rag_service:index_repository_docs - 🔄 开始索引仓库文档: Sakura520222/Sakura-AI-Reviewer
sakura-ai-reviewer  | 2026-05-15 06:11:42 | INFO     | backend.services.document_service:scan_sakura_directory - 📄 扫描到 0 个 Markdown 文件
sakura-ai-reviewer  | 2026-05-15 06:11:42 | INFO     | backend.services.rag_service:index_repository_docs - 仓库 Sakura520222/Sakura-AI-Reviewer 中没有文档
sakura-ai-reviewer  | 2026-05-15 06:11:42 | INFO     | backend.workers.review_worker:process_review_task - [f3ddfcb6] 文档索引完成: 0 文件, 0 块
sakura-ai-reviewer  | 2026-05-15 06:11:42 | INFO     | backend.workers.review_worker:_do - [f3ddfcb6] 创建审查记录: 391
sakura-ai-reviewer  | 2026-05-15 06:11:44 | INFO     | backend.core.github_app:get_repo_client - ✓ 成功获取仓库 Sakura520222/Sakura-AI-Reviewer 的访问令牌
sakura-ai-reviewer  | 2026-05-15 06:11:49 | INFO     | backend.services.ai_reviewer.api_client:_retry_loop - ✅ AI调用成功（耗时 3.8秒，重试 0 次）
sakura-ai-reviewer  | 2026-05-15 06:11:49 | INFO     | backend.services.ai_reviewer.pr_summary:generate_summary - PR 总结生成完成，长度: 656 字符
sakura-ai-reviewer  | 2026-05-15 06:11:50 | INFO     | backend.services.ai_reviewer.pr_summary:update_pr_body - PR body 已更新（追加/替换 AI 摘要）
sakura-ai-reviewer  | 2026-05-15 06:11:50 | INFO     | backend.workers.review_worker:process_review_task - [f3ddfcb6] PR 变更总结已更新
sakura-ai-reviewer  | 2026-05-15 06:11:52 | INFO     | backend.api.webhook:handle_github_webhook - 收到GitHub事件: pull_request
sakura-ai-reviewer  | 2026-05-15 06:11:52 | INFO     | backend.core.github_app:extract_pr_info_from_webhook - 成功提取PR信息: Sakura520222/Sakura-AI-Reviewer#305
sakura-ai-reviewer  | 2026-05-15 06:11:52 | INFO     | backend.api.webhook:handle_pull_request_event - 忽略PR动作: edited
sakura-ai-reviewer  | INFO:     104.23.211.137:0 - "POST /api/webhook/github HTTP/1.1" 200 OK
sakura-ai-reviewer  | INFO:     127.0.0.1:57592 - "GET /health HTTP/1.1" 200 OK
sakura-ai-reviewer  | 2026-05-15 06:12:10 | INFO     | backend.services.ai_reviewer.pr_dependency_graph:update_pr_body_with_graph - PR body 已更新（注入依赖图）
sakura-ai-reviewer  | 2026-05-15 06:12:10 | INFO     | backend.services.ai_reviewer.pr_dependency_graph:generate_dependency_graph - 静态 PR 依赖图已生成，长度: 704 字符
sakura-ai-reviewer  | 2026-05-15 06:12:10 | INFO     | backend.workers.review_worker:process_review_task - [f3ddfcb6] PR 依赖图已生成并注入
sakura-ai-reviewer  | 2026-05-15 06:12:10 | INFO     | backend.workers.review_worker:process_review_task - [f3ddfcb6] 创建占位评论...
sakura-ai-reviewer  | 2026-05-15 06:12:11 | INFO     | backend.services.comment_service:create_placeholder_comment - ✓ 已创建占位评论到PR: Sakura520222/Sakura-AI-Reviewer#305 (Comment ID: 4457383395)
sakura-ai-reviewer  | 2026-05-15 06:12:12 | INFO     | backend.api.webhook:handle_github_webhook - 收到GitHub事件: pull_request
sakura-ai-reviewer  | 2026-05-15 06:12:12 | INFO     | backend.core.github_app:extract_pr_info_from_webhook - 成功提取PR信息: Sakura520222/Sakura-AI-Reviewer#305
sakura-ai-reviewer  | 2026-05-15 06:12:12 | INFO     | backend.api.webhook:handle_pull_request_event - 忽略PR动作: edited
sakura-ai-reviewer  | INFO:     172.68.245.17:0 - "POST /api/webhook/github HTTP/1.1" 200 OK
sakura-ai-reviewer  | 2026-05-15 06:12:12 | INFO     | backend.services.pr_analyzer:_get_project_structure_sync - 获取项目结构完成，共 500 个项目（已过滤skip_paths）
sakura-ai-reviewer  | 2026-05-15 06:12:12 | INFO     | backend.services.github_write_service:__init__ - GitHubWriteService initialized
sakura-ai-reviewer  | 2026-05-15 06:12:13 | INFO     | backend.api.webhook:handle_github_webhook - 收到GitHub事件: issue_comment
sakura-ai-reviewer  | INFO:     172.70.135.107:0 - "POST /api/webhook/github HTTP/1.1" 200 OK
sakura-ai-reviewer  | 2026-05-15 06:12:14 | INFO     | backend.workers.review_worker:process_review_task - [f3ddfcb6] 已注入 .sakura/ 记忆上下文: SAKURA.md(5650字), memory.md(7732字)
sakura-ai-reviewer  | 2026-05-15 06:12:16 | INFO     | backend.services.issue_embedding_service:verify_related_issues - Issue #154 content 长度: 131, 前100字: '[Enhancement][Medium] 添加 i18n 国际化支持，支持多语言 WebUI 界面和 AI 输出\n当前全是中文，webui界面语言只有中文。PR审查、issues分析AI等都只输出中'
sakura-ai-reviewer  | 2026-05-15 06:12:16 | INFO     | backend.services.issue_embedding_service:verify_related_issues - Issue #304 content 长度: 333, 前100字: '新功能及改进建议\nIssue 提出了一套完整的 `.sakura/` 知识管理系统增强方案，包含五个核心子需求：(1) 自动维护 rules/docs/plans 等子目录（当前仅初始化 SAKURA'
sakura-ai-reviewer  | 2026-05-15 06:12:16 | INFO     | backend.services.issue_embedding_service:verify_related_issues - Issue #224 content 长度: 5908, 前100字: "[bug][medium] read_file 工具读取目录路径时崩溃: 'list' object has no attribute 'size'\n错误日志：\n```\nsakura-ai-revie"
sakura-ai-reviewer  | 2026-05-15 06:12:16 | INFO     | backend.services.issue_embedding_service:verify_related_issues - Issue #131 content 长度: 247, 前100字: '改进建议-WebUI中按用户配置\nIssue 请求新增按用户的独立配置功能。当前系统配置架构为全局单例模式：Settings（环境变量）→ AppConfig（数据库全局）→ YAML 文件 → 硬编'
sakura-ai-reviewer  | 2026-05-15 06:12:16 | INFO     | backend.services.issue_embedding_service:verify_related_issues - AI 验证请求: 4 个候选, issues 内容长度: 6619
sakura-ai-reviewer  | 2026-05-15 06:12:22 | INFO     | backend.services.ai_reviewer.api_client:_retry_loop - ✅ AI调用成功（耗时 5.8秒，重试 0 次）
sakura-ai-reviewer  | 2026-05-15 06:12:22 | INFO     | backend.services.issue_embedding_service:verify_related_issues - AI 验证原始响应: {"verified": ["304"], "reasons": {"154": "PR 未涉及国际化或多语言支持，仅新增知识管理功能，与 Issue 无关", "304": "PR 直接实现了 Issue 描述的核心需求：新增 sakura_memory_tool 用于读取反思文件、增强 AI 提示词以关注项目规范、新增 WebUI 知识管理界面，符合关联条件", "224": "PR 未修复 read_file 工具读取目录路径时的崩溃问题，仅新增知识管理工具，与 Issue 无关", "131": "PR 未涉及用户级别配置功能，仅新增全局知识管理，与 Issue 无关"}}
sakura-ai-reviewer  | 2026-05-15 06:12:22 | INFO     | backend.services.issue_embedding_service:verify_related_issues - AI 验证过滤了 3 个误判: 保留 [304], 移除 [154, 224, 131]
sakura-ai-reviewer  | INFO:     127.0.0.1:39122 - "GET /health HTTP/1.1" 200 OK
sakura-ai-reviewer  | 2026-05-15 06:12:24 | INFO     | backend.workers.review_worker:process_review_task - [f3ddfcb6] 语义关联了 1 个 Issues: [304]
sakura-ai-reviewer  | 2026-05-15 06:12:24 | INFO     | backend.workers.review_worker:process_review_task - [f3ddfcb6] 使用AI工具增强模式进行审查（支持分批处理）
sakura-ai-reviewer  | 2026-05-15 06:12:24 | INFO     | backend.workers.review_worker:process_review_task - [f3ddfcb6] 并行启动AI标签推荐...
sakura-ai-reviewer  | 2026-05-15 06:12:24 | INFO     | backend.services.ai_reviewer.reviewer:review_pr_with_tools_batched - PR规模较小 (22 个文件)，使用标准审查模式（启用AI工具）
sakura-ai-reviewer  | 2026-05-15 06:12:24 | INFO     | backend.services.ai_reviewer.reviewer:review_pr_with_tools - 开始AI审查（带工具支持），策略: deep
sakura-ai-reviewer  | 2026-05-15 06:12:24 | INFO     | backend.services.label_service:get_repo_labels - 从GitHub获取标签列表: Sakura520222/Sakura-AI-Reviewer
sakura-ai-reviewer  | 2026-05-15 06:12:24 | INFO     | backend.core.model_context:get_context_window - 使用自定义上下文窗口: 200K tokens
sakura-ai-reviewer  | 2026-05-15 06:12:26 | INFO     | backend.api.webhook:handle_github_webhook - 收到GitHub事件: pull_request
sakura-ai-reviewer  | 2026-05-15 06:12:26 | INFO     | backend.core.github_app:extract_pr_info_from_webhook - 成功提取PR信息: Sakura520222/Sakura-AI-Reviewer#305
sakura-ai-reviewer  | 2026-05-15 06:12:26 | INFO     | backend.api.webhook:handle_pull_request_event - 忽略PR动作: edited
sakura-ai-reviewer  | INFO:     104.23.211.137:0 - "POST /api/webhook/github HTTP/1.1" 200 OK
sakura-ai-reviewer  | 2026-05-15 06:12:26 | INFO     | backend.core.github_app:get_repo_client - ✓ 成功获取仓库 Sakura520222/Sakura-AI-Reviewer 的访问令牌
sakura-ai-reviewer  | 2026-05-15 06:12:27 | INFO     | backend.core.github_app:get_repo_labels - 成功获取仓库 Sakura520222/Sakura-AI-Reviewer 的 19 个标签
sakura-ai-reviewer  | 2026-05-15 06:12:28 | INFO     | backend.core.github_app:get_repo_client - ✓ 成功获取仓库 Sakura520222/Sakura-AI-Reviewer 的访问令牌
sakura-ai-reviewer  | 2026-05-15 06:12:30 | INFO     | backend.core.github_app:get_pr_labels - 成功获取 PR Sakura520222/Sakura-AI-Reviewer#305 的 0 个标签: []
sakura-ai-reviewer  | 2026-05-15 06:12:30 | INFO     | backend.services.ai_reviewer.label_recommender:recommend_labels - 开始AI标签推荐分析
sakura-ai-reviewer  | 2026-05-15 06:12:32 | INFO     | backend.services.ai_reviewer.api_client:_retry_loop - ✅ AI调用成功（耗时 1.8秒，重试 0 次）
sakura-ai-reviewer  | 2026-05-15 06:12:32 | INFO     | backend.services.ai_reviewer.label_recommender:recommend_labels - AI标签推荐响应长度: 406 字符
sakura-ai-reviewer  | 2026-05-15 06:12:32 | INFO     | backend.services.ai_reviewer.result_parser:parse_label_recommendation - 成功解析 3 个标签推荐
sakura-ai-reviewer  | 2026-05-15 06:12:32 | INFO     | backend.services.ai_reviewer.label_recommender:recommend_labels - AI标签推荐完成，共 3 个推荐
sakura-ai-reviewer  | 2026-05-15 06:12:33 | INFO     | backend.core.github_app:get_repo_client - ✓ 成功获取仓库 Sakura520222/Sakura-AI-Reviewer 的访问令牌
sakura-ai-reviewer  | 2026-05-15 06:12:35 | INFO     | backend.core.github_app:add_labels_to_pr - 成功给 PR #305 添加标签: ['enhancement']
sakura-ai-reviewer  | 2026-05-15 06:12:35 | INFO     | backend.workers.review_worker:run_label_recommendation - [f3ddfcb6] 标签应用完成: 已应用 1 个, 建议 2 个, 冲突跳过 0 个
sakura-ai-reviewer  | 2026-05-15 06:12:36 | INFO     | backend.api.webhook:handle_github_webhook - 收到GitHub事件: pull_request
sakura-ai-reviewer  | 2026-05-15 06:12:36 | INFO     | backend.core.github_app:extract_pr_info_from_webhook - 成功提取PR信息: Sakura520222/Sakura-AI-Reviewer#305
sakura-ai-reviewer  | 2026-05-15 06:12:36 | INFO     | backend.api.webhook:handle_pull_request_event - 忽略PR动作: labeled
sakura-ai-reviewer  | INFO:     172.70.174.126:0 - "POST /api/webhook/github HTTP/1.1" 200 OK
sakura-ai-reviewer  | 2026-05-15 06:12:39 | INFO     | backend.services.ai_reviewer.api_client:_retry_loop - ✅ AI调用成功（耗时 14.6秒，重试 0 次）
sakura-ai-reviewer  | 2026-05-15 06:12:39 | INFO     | backend.services.ai_reviewer.reviewer:_run_tool_loop - 执行工具 read_file: {"file_path":"backend/services/agent_team/tools/base.py"}
sakura-ai-reviewer  | 2026-05-15 06:12:40 | INFO     | backend.services.ai_reviewer.reviewer:_run_tool_loop - 执行工具 read_file: {"file_path":"backend/services/sakura_memory_service.py","start_line":1,"end_line":50}
sakura-ai-reviewer  | 2026-05-15 06:12:45 | INFO     | backend.services.ai_reviewer.reviewer:_run_tool_loop - 执行工具 read_sakura_docs: {}
sakura-ai-reviewer  | 2026-05-15 06:12:45 | INFO     | backend.core.model_context:get_context_window - 使用自定义上下文窗口: 200K tokens
sakura-ai-reviewer  | INFO:     127.0.0.1:49660 - "GET /health HTTP/1.1" 200 OK
sakura-ai-reviewer  | 2026-05-15 06:12:57 | INFO     | backend.services.ai_reviewer.api_client:_retry_loop - ✅ AI调用成功（耗时 12.3秒，重试 0 次）
sakura-ai-reviewer  | 2026-05-15 06:12:58 | INFO     | backend.services.ai_reviewer.reviewer:_run_tool_loop - 执行工具 read_file: {"file_path":"backend/services/sakura_memory_service.py","start_line":1070,"end_line":1160}
sakura-ai-reviewer  | 2026-05-15 06:13:00 | INFO     | backend.services.ai_reviewer.reviewer:_run_tool_loop - 执行工具 search_in_files: {"keyword":"_update_state","file_extension":".py","directory":"backend/services","max_results":10}
sakura-ai-reviewer  | 2026-05-15 06:13:01 | INFO     | backend.services.ai_reviewer.reviewer:_run_tool_loop - 执行工具 search_in_files: {"keyword":"SakuraMemoryState","file_extension":".py","directory":"backend/models","max_results":5}
sakura-ai-reviewer  | 2026-05-15 06:13:01 | INFO     | backend.core.model_context:get_context_window - 使用自定义上下文窗口: 200K tokens
sakura-ai-reviewer  | 2026-05-15 06:13:09 | INFO     | backend.services.ai_reviewer.api_client:_retry_loop - ✅ AI调用成功（耗时 7.8秒，重试 0 次）
sakura-ai-reviewer  | 2026-05-15 06:13:10 | INFO     | backend.services.ai_reviewer.reviewer:_run_tool_loop - 执行工具 read_file: {"file_path":"backend/services/sakura_memory_service.py","start_line":855,"end_line":940}
sakura-ai-reviewer  | 2026-05-15 06:13:11 | INFO     | backend.services.ai_reviewer.reviewer:_run_tool_loop - 执行工具 read_file: {"file_path":"backend/models/database.py","start_line":860,"end_line":895}
sakura-ai-reviewer  | 2026-05-15 06:13:11 | INFO     | backend.core.model_context:get_context_window - 使用自定义上下文窗口: 200K tokens
sakura-ai-reviewer  | 2026-05-15 06:13:20 | INFO     | backend.services.ai_reviewer.api_client:_retry_loop - ✅ AI调用成功（耗时 9.4秒，重试 0 次）
sakura-ai-reviewer  | 2026-05-15 06:13:21 | INFO     | backend.services.ai_reviewer.reviewer:_run_tool_loop - 执行工具 read_file: {"file_path":"backend/services/sakura_memory_service.py","start_line":350,"end_line":380}
sakura-ai-reviewer  | 2026-05-15 06:13:23 | INFO     | backend.services.ai_reviewer.reviewer:_run_tool_loop - 执行工具 search_in_files: {"keyword":"_get_or_create_state","file_extension":".py","directory":"backend/services","max_results":5}
sakura-ai-reviewer  | 2026-05-15 06:13:23 | INFO     | backend.core.model_context:get_context_window - 使用自定义上下文窗口: 200K tokens
sakura-ai-reviewer  | INFO:     127.0.0.1:52630 - "GET /health HTTP/1.1" 200 OK
sakura-ai-reviewer  | 2026-05-15 06:13:29 | INFO     | backend.services.ai_reviewer.api_client:_retry_loop - ✅ AI调用成功（耗时 5.4秒，重试 0 次）
sakura-ai-reviewer  | 2026-05-15 06:13:29 | INFO     | backend.services.ai_reviewer.reviewer:_run_tool_loop - 执行工具 read_file: {"file_path":"backend/services/sakura_memory_service.py","start_line":940,"end_line":1040}
sakura-ai-reviewer  | 2026-05-15 06:13:30 | INFO     | backend.services.ai_reviewer.reviewer:_run_tool_loop - 执行工具 search_in_files: {"keyword":"knowledge_extracted","file_extension":".py","max_results":10}
sakura-ai-reviewer  | 2026-05-15 06:13:30 | INFO     | backend.core.model_context:get_context_window - 使用自定义上下文窗口: 200K tokens
sakura-ai-reviewer  | 2026-05-15 06:13:40 | INFO     | backend.services.ai_reviewer.api_client:_retry_loop - ✅ AI调用成功（耗时 9.6秒，重试 0 次）
sakura-ai-reviewer  | 2026-05-15 06:13:41 | INFO     | backend.services.ai_reviewer.reviewer:_run_tool_loop - 执行工具 search_in_files: {"keyword":"knowledge_extracted","max_results":15}
sakura-ai-reviewer  | 2026-05-15 06:13:41 | INFO     | backend.services.ai_reviewer.reviewer:_run_tool_loop - 执行工具 search_in_files: {"keyword":"ALTER TABLE sakura_memory_states","max_results":5}
sakura-ai-reviewer  | 2026-05-15 06:13:41 | INFO     | backend.core.model_context:get_context_window - 使用自定义上下文窗口: 200K tokens
sakura-ai-reviewer  | 2026-05-15 06:13:52 | INFO     | backend.services.ai_reviewer.api_client:_retry_loop - ✅ AI调用成功（耗时 10.4秒，重试 0 次）
sakura-ai-reviewer  | 2026-05-15 06:13:53 | INFO     | backend.services.ai_reviewer.reviewer:_run_tool_loop - 执行工具 search_in_files: {"keyword":"sakura_memory_states","file_extension":".sql","max_results":5}
sakura-ai-reviewer  | 2026-05-15 06:13:54 | INFO     | backend.services.ai_reviewer.reviewer:_run_tool_loop - 执行工具 list_directory: {"directory":"docker/mysql-init"}
sakura-ai-reviewer  | 2026-05-15 06:13:56 | INFO     | backend.services.ai_reviewer.reviewer:_run_tool_loop - 执行工具 search_in_files: {"keyword":"SchemaMigration","file_extension":".py","max_results":5}
sakura-ai-reviewer  | 2026-05-15 06:13:56 | INFO     | backend.core.model_context:get_context_window - 使用自定义上下文窗口: 200K tokens
sakura-ai-reviewer  | INFO:     127.0.0.1:33610 - "GET /health HTTP/1.1" 200 OK
sakura-ai-reviewer  | 2026-05-15 06:14:07 | INFO     | backend.services.ai_reviewer.api_client:_retry_loop - ✅ AI调用成功（耗时 11.2秒，重试 0 次）
sakura-ai-reviewer  | 2026-05-15 06:14:07 | INFO     | backend.services.ai_reviewer.reviewer:_run_tool_loop - 执行工具 read_file: {"file_path":"docker/mysql-init/init.sql","start_line":107,"end_line":140}
sakura-ai-reviewer  | 2026-05-15 06:14:08 | INFO     | backend.services.ai_reviewer.reviewer:_run_tool_loop - 执行工具 read_file: {"file_path":"backend/models/database.py","start_line":930,"end_line":1000}
sakura-ai-reviewer  | 2026-05-15 06:14:08 | INFO     | backend.core.model_context:get_context_window - 使用自定义上下文窗口: 200K tokens
sakura-ai-reviewer  | INFO:     127.0.0.1:46636 - "GET /health HTTP/1.1" 200 OK
sakura-ai-reviewer  | 2026-05-15 06:14:43 | INFO     | backend.services.ai_reviewer.api_client:_retry_loop - ✅ AI调用成功（耗时 34.2秒，重试 0 次）
sakura-ai-reviewer  | 2026-05-15 06:14:43 | INFO     | backend.services.ai_reviewer.reviewer:_run_tool_loop - 执行工具 read_file: {"file_path":"backend/services/sakura_memory_service.py","start_line":1071,"end_line":1100}
sakura-ai-reviewer  | 2026-05-15 06:14:44 | INFO     | backend.services.ai_reviewer.reviewer:_run_tool_loop - 执行工具 search_in_files: {"keyword":"sakura_extraction_model","file_extension":".py","max_results":5}
sakura-ai-reviewer  | 2026-05-15 06:14:44 | INFO     | backend.core.model_context:get_context_window - 使用自定义上下文窗口: 200K tokens
sakura-ai-reviewer  | 2026-05-15 06:14:54 | INFO     | backend.services.ai_reviewer.api_client:_retry_loop - ✅ AI调用成功（耗时 9.4秒，重试 0 次）
sakura-ai-reviewer  | 2026-05-15 06:14:54 | INFO     | backend.services.ai_reviewer.reviewer:_run_tool_loop - 执行工具 search_in_files: {"file_extension":".py","keyword":"sakura_extraction_model","directory":"backend/core"}
sakura-ai-reviewer  | 2026-05-15 06:14:55 | INFO     | backend.services.ai_reviewer.reviewer:_run_tool_loop - 执行工具 read_file: {"file_path":"backend/services/sakura_consolidation_agent.py","start_line":1,"end_line":15}
sakura-ai-reviewer  | 2026-05-15 06:14:55 | INFO     | backend.core.model_context:get_context_window - 使用自定义上下文窗口: 200K tokens
sakura-ai-reviewer  | INFO:     127.0.0.1:42642 - "GET /health HTTP/1.1" 200 OK
sakura-ai-reviewer  | INFO:     127.0.0.1:53360 - "GET /health HTTP/1.1" 200 OK
sakura-ai-reviewer  | 2026-05-15 06:15:48 | INFO     | backend.services.ai_reviewer.api_client:_retry_loop - ✅ AI调用成功（耗时 53.5秒，重试 0 次）
sakura-ai-reviewer  | 2026-05-15 06:15:53 | INFO     | backend.services.ai_reviewer.reviewer:_run_tool_loop - 执行工具 search_in_files: {"file_extension":".yaml","keyword":"sakura_memory","max_results":10}
sakura-ai-reviewer  | 2026-05-15 06:15:54 | INFO     | backend.services.ai_reviewer.reviewer:_run_tool_loop - 执行工具 search_in_files: {"file_extension":".py","keyword":"sakura_knowledge_extraction_model","max_results":5}
sakura-ai-reviewer  | 2026-05-15 06:15:54 | INFO     | backend.core.model_context:get_context_window - 使用自定义上下文窗口: 200K tokens
sakura-ai-reviewer  | INFO:     127.0.0.1:43228 - "GET /health HTTP/1.1" 200 OK
sakura-ai-reviewer  | 2026-05-15 06:16:02 | INFO     | backend.services.ai_reviewer.api_client:_retry_loop - ✅ AI调用成功（耗时 8.0秒，重试 0 次）
sakura-ai-reviewer  | 2026-05-15 06:16:03 | INFO     | backend.services.ai_reviewer.reviewer:_run_tool_loop - 执行工具 read_file: {"file_path":"config/strategies.yaml","start_line":1018,"end_line":1100}
sakura-ai-reviewer  | 2026-05-15 06:16:04 | INFO     | backend.services.ai_reviewer.reviewer:_run_tool_loop - 执行工具 read_file: {"file_path":"backend/webui/translations/zh-CN.yaml","start_line":630,"end_line":730}
sakura-ai-reviewer  | 2026-05-15 06:16:04 | INFO     | backend.core.model_context:get_context_window - 使用自定义上下文窗口: 200K tokens
sakura-ai-reviewer  | 2026-05-15 06:16:15 | INFO     | backend.services.ai_reviewer.api_client:_retry_loop - ✅ AI调用成功（耗时 10.6秒，重试 0 次）
sakura-ai-reviewer  | 2026-05-15 06:16:16 | INFO     | backend.services.ai_reviewer.reviewer:_run_tool_loop - 执行工具 read_file: {"file_path":"backend/webui/translations/zh-CN.yaml","start_line":730,"end_line":740}
sakura-ai-reviewer  | 2026-05-15 06:16:17 | INFO     | backend.services.ai_reviewer.reviewer:_run_tool_loop - 执行工具 read_file: {"file_path":"backend/webui/translations/en.yaml","start_line":630,"end_line":740}
sakura-ai-reviewer  | 2026-05-15 06:16:17 | INFO     | backend.core.model_context:get_context_window - 使用自定义上下文窗口: 200K tokens
sakura-ai-reviewer  | INFO:     127.0.0.1:42568 - "GET /health HTTP/1.1" 200 OK
sakura-ai-reviewer  | 2026-05-15 06:16:33 | INFO     | backend.services.ai_reviewer.api_client:_retry_loop - ✅ AI调用成功（耗时 15.7秒，重试 0 次）
sakura-ai-reviewer  | 2026-05-15 06:16:34 | INFO     | backend.services.ai_reviewer.reviewer:_run_tool_loop - 执行工具 search_in_files: {"file_extension":".yaml","keyword":"toast.sakura","max_results":10}
sakura-ai-reviewer  | 2026-05-15 06:16:34 | INFO     | backend.services.ai_reviewer.reviewer:_run_tool_loop - 执行工具 search_in_files: {"file_extension":"yaml","keyword":"sakura_knowledge_extraction_model","max_results":5}
sakura-ai-reviewer  | 2026-05-15 06:16:34 | INFO     | backend.core.model_context:get_context_window - 使用自定义上下文窗口: 200K tokens
sakura-ai-reviewer  | 2026-05-15 06:16:46 | INFO     | backend.services.ai_reviewer.api_client:_retry_loop - ✅ AI调用成功（耗时 11.5秒，重试 0 次）
sakura-ai-reviewer  | 2026-05-15 06:16:46 | INFO     | backend.services.ai_reviewer.reviewer:_run_tool_loop - 执行工具 search_in_files: {"file_extension":"yaml","keyword":"sakura_consolidate_triggered","max_results":5}
sakura-ai-reviewer  | 2026-05-15 06:16:47 | INFO     | backend.services.ai_reviewer.reviewer:_run_tool_loop - 执行工具 search_in_files: {"file_extension":"yaml","keyword":"sakura_file_saved","max_results":5}
sakura-ai-reviewer  | 2026-05-15 06:16:47 | INFO     | backend.core.model_context:get_context_window - 使用自定义上下文窗口: 200K tokens
sakura-ai-reviewer  | 2026-05-15 06:16:55 | INFO     | backend.services.ai_reviewer.api_client:_retry_loop - ✅ AI调用成功（耗时 7.8秒，重试 0 次）
sakura-ai-reviewer  | 2026-05-15 06:16:58 | INFO     | backend.services.ai_reviewer.reviewer:_run_tool_loop - 执行工具 search_in_files: {"file_extension":"yaml","keyword":"sakura","directory":"backend/webui/translations","max_results":20}
sakura-ai-reviewer  | 2026-05-15 06:16:58 | INFO     | backend.core.model_context:get_context_window - 使用自定义上下文窗口: 200K tokens
sakura-ai-reviewer  | INFO:     127.0.0.1:56470 - "GET /health HTTP/1.1" 200 OK
sakura-ai-reviewer  | 2026-05-15 06:17:10 | INFO     | backend.services.ai_reviewer.api_client:_retry_loop - ✅ AI调用成功（耗时 12.1秒，重试 0 次）
sakura-ai-reviewer  | 2026-05-15 06:17:10 | INFO     | backend.services.ai_reviewer.reviewer:_run_tool_loop - 执行工具 search_in_files: {"file_extension":"yaml","keyword":"sakura_repo_not_initialized","max_results":5}
sakura-ai-reviewer  | 2026-05-15 06:17:13 | INFO     | backend.services.ai_reviewer.reviewer:_run_tool_loop - 执行工具 search_in_files: {"file_extension":"yaml","keyword":"toast:","directory":"backend/webui/translations","context_lines":5,"max_results":3}
sakura-ai-reviewer  | 2026-05-15 06:17:13 | INFO     | backend.core.model_context:get_context_window - 使用自定义上下文窗口: 200K tokens
sakura-ai-reviewer  | 2026-05-15 06:17:31 | INFO     | backend.services.ai_reviewer.api_client:_retry_loop - ✅ AI调用成功（耗时 17.8秒，重试 0 次）
sakura-ai-reviewer  | 2026-05-15 06:17:32 | INFO     | backend.services.ai_reviewer.reviewer:_run_tool_loop - 执行工具 read_file: {"file_path":"backend/webui/translations/zh-CN.yaml","start_line":1122,"end_line":1150}
sakura-ai-reviewer  | 2026-05-15 06:17:32 | INFO     | backend.core.model_context:get_context_window - 使用自定义上下文窗口: 200K tokens
sakura-ai-reviewer  | INFO:     127.0.0.1:55076 - "GET /health HTTP/1.1" 200 OK
sakura-ai-reviewer  | 2026-05-15 06:17:40 | INFO     | backend.services.ai_reviewer.api_client:_retry_loop - ✅ AI调用成功（耗时 8.6秒，重试 0 次）
sakura-ai-reviewer  | 2026-05-15 06:17:41 | INFO     | backend.services.ai_reviewer.reviewer:_run_tool_loop - 执行工具 read_file: {"file_path":"backend/webui/translations/zh-CN.yaml","start_line":1150,"end_line":1165}
sakura-ai-reviewer  | 2026-05-15 06:17:41 | INFO     | backend.core.model_context:get_context_window - 使用自定义上下文窗口: 200K tokens
sakura-ai-reviewer  | 2026-05-15 06:17:50 | INFO     | backend.services.ai_reviewer.api_client:_retry_loop - ✅ AI调用成功（耗时 9.1秒，重试 0 次）
sakura-ai-reviewer  | 2026-05-15 06:17:51 | INFO     | backend.services.ai_reviewer.reviewer:_run_tool_loop - 执行工具 read_file: {"file_path":"backend/webui/translations/zh-CN.yaml","start_line":1165,"end_line":1200}
sakura-ai-reviewer  | 2026-05-15 06:17:51 | INFO     | backend.core.model_context:get_context_window - 使用自定义上下文窗口: 200K tokens
sakura-ai-reviewer  | 2026-05-15 06:18:03 | INFO     | backend.services.ai_reviewer.api_client:_retry_loop - ✅ AI调用成功（耗时 11.4秒，重试 0 次）
sakura-ai-reviewer  | 2026-05-15 06:18:04 | INFO     | backend.services.ai_reviewer.reviewer:_run_tool_loop - 执行工具 read_file: {"file_path":"backend/webui/translations/zh-CN.yaml","start_line":1200,"end_line":1220}
sakura-ai-reviewer  | 2026-05-15 06:18:04 | INFO     | backend.core.model_context:get_context_window - 使用自定义上下文窗口: 200K tokens
sakura-ai-reviewer  | INFO:     127.0.0.1:60192 - "GET /health HTTP/1.1" 200 OK
sakura-ai-reviewer  | INFO:     127.0.0.1:36312 - "GET /health HTTP/1.1" 200 OK
sakura-ai-reviewer  | INFO:     127.0.0.1:38930 - "GET /health HTTP/1.1" 200 OK
sakura-ai-reviewer  | INFO:     127.0.0.1:58894 - "GET /health HTTP/1.1" 200 OK
sakura-ai-reviewer  | INFO:     127.0.0.1:45310 - "GET /health HTTP/1.1" 200 OK
sakura-ai-reviewer  | 2026-05-15 06:20:27 | INFO     | backend.services.ai_reviewer.api_client:_retry_loop - ✅ AI调用成功（耗时 143.5秒，重试 0 次）
sakura-ai-reviewer  | 2026-05-15 06:20:27 | INFO     | backend.services.ai_reviewer.result_parser:extract_inline_comments - 提取行内评论: backend/services/sakura_memory_service.py:1089 - major
sakura-ai-reviewer  | 2026-05-15 06:20:27 | INFO     | backend.services.ai_reviewer.result_parser:extract_inline_comments - 提取行内评论: backend/core/config.py:973 - major
sakura-ai-reviewer  | 2026-05-15 06:20:27 | INFO     | backend.services.ai_reviewer.result_parser:extract_inline_comments - 提取行内评论: backend/services/sakura_memory_service.py:1020 - major
sakura-ai-reviewer  | 2026-05-15 06:20:27 | INFO     | backend.services.ai_reviewer.result_parser:extract_inline_comments - 提取行内评论: docker/mysql-init/init.sql:107 - major
sakura-ai-reviewer  | 2026-05-15 06:20:27 | INFO     | backend.services.ai_reviewer.result_parser:extract_inline_comments - 提取行内评论: backend/webui/routes/sakura_memory.py:327-329 - major
sakura-ai-reviewer  | 2026-05-15 06:20:27 | INFO     | backend.services.ai_reviewer.result_parser:extract_inline_comments - 提取行内评论: backend/webui/templates/sakura_memory.html:89-156 - major
sakura-ai-reviewer  | 2026-05-15 06:20:27 | INFO     | backend.services.ai_reviewer.result_parser:extract_inline_comments - 提取行内评论: backend/services/sakura_consolidation_agent.py:329 - suggestion
sakura-ai-reviewer  | 2026-05-15 06:20:27 | INFO     | backend.services.ai_reviewer.result_parser:extract_inline_comments - 提取行内评论: backend/webui/routes/sakura_memory.py:461-486 - suggestion
sakura-ai-reviewer  | 2026-05-15 06:20:27 | INFO     | backend.services.ai_reviewer.result_parser:extract_inline_comments - 提取行内评论: backend/services/sakura_knowledge_extractor.py:163-166 - suggestion
sakura-ai-reviewer  | 2026-05-15 06:20:27 | INFO     | backend.services.ai_reviewer.result_parser:extract_inline_comments - 共提取 20 条行内评论
sakura-ai-reviewer  | 2026-05-15 06:20:27 | INFO     | backend.services.ai_reviewer.result_parser:parse_review_result - ✅ 结构化 JSON 解析成功 (策略: deep, decision: comment, JSON 行内: 11, Markdown 行内: 4, 整体评论: 27)
sakura-ai-reviewer  | 2026-05-15 06:20:27 | INFO     | backend.services.ai_reviewer.reviewer:_run_tool_loop - AI审查完成（使用了22轮对话），策略: deep
sakura-ai-reviewer  | 2026-05-15 06:20:27 | INFO     | backend.workers.review_worker:_do - 保存了 27 条整体评论和 15 条行内评论
sakura-ai-reviewer  | 2026-05-15 06:20:27 | INFO     | backend.workers.review_worker:process_review_task - [f3ddfcb6] 删除占位评论...
sakura-ai-reviewer  | 2026-05-15 06:20:29 | INFO     | backend.services.comment_service:delete_placeholder_comment - ✓ 已删除占位评论 (Comment ID: 4457383395)
sakura-ai-reviewer  | 2026-05-15 06:20:29 | INFO     | backend.workers.review_worker:process_review_task - [f3ddfcb6] 执行决策引擎...
sakura-ai-reviewer  | 2026-05-15 06:20:29 | INFO     | backend.services.decision_engine:_load_policy - 审查策略配置加载成功: enabled=True
sakura-ai-reviewer  | 2026-05-15 06:20:29 | INFO     | backend.services.score_extractor:get_score_extractor - ✅ ScoreExtractor单例已初始化
sakura-ai-reviewer  | 2026-05-15 06:20:29 | INFO     | backend.services.decision_engine:make_decision - 决策分析: score=7, critical=1, major=33, minor=2, suggestions=11
sakura-ai-reviewer  | 2026-05-15 06:20:29 | INFO     | backend.services.decision_engine:make_decision - AI 决策: comment → 最终决策: comment
sakura-ai-reviewer  | 2026-05-15 06:20:29 | INFO     | backend.workers.review_worker:_make_and_submit_decision - [f3ddfcb6] 决策引擎结果: decision=comment, reason=功能实现完整，架构方向正确，但存在显著的代码重复（~500行）、缺失i18n翻译键（7+个）、配置键名不匹配、以及若干代码规范问题。无崩溃或安全漏洞，建议修复后合并。
sakura-ai-reviewer  | 2026-05-15 06:20:29 | INFO     | backend.workers.review_worker:_make_and_submit_decision - [f3ddfcb6] 行内评论功能已关闭，跳过 15 条行内评论
sakura-ai-reviewer  | 2026-05-15 06:20:29 | INFO     | backend.api.webhook:handle_github_webhook - 收到GitHub事件: issue_comment
sakura-ai-reviewer  | INFO:     104.22.101.107:0 - "POST /api/webhook/github HTTP/1.1" 200 OK
sakura-ai-reviewer  | 2026-05-15 06:20:30 | INFO     | backend.core.github_app:get_repo_client - ✓ 成功获取仓库 Sakura520222/Sakura-AI-Reviewer 的访问令牌
sakura-ai-reviewer  | 2026-05-15 06:20:33 | INFO     | backend.core.github_app:get_repo_client - ✓ 成功获取仓库 Sakura520222/Sakura-AI-Reviewer 的访问令牌
sakura-ai-reviewer  | 2026-05-15 06:20:37 | INFO     | backend.core.github_app:submit_review_with_inline_comments - ✅ 成功提交Review: Sakura520222/Sakura-AI-Reviewer#305, event=COMMENT, body_length=6346, inline_comments=0
sakura-ai-reviewer  | 2026-05-15 06:20:37 | INFO     | backend.workers.review_worker:_make_and_submit_decision - [f3ddfcb6] ✅ 成功提交Review到GitHub: comment (无行内评论)
sakura-ai-reviewer  | 2026-05-15 06:20:37 | INFO     | backend.workers.review_worker:process_review_task - [f3ddfcb6] 已触发 .sakura/ 反思任务
sakura-ai-reviewer  | 2026-05-15 06:20:38 | INFO     | backend.telegram.notifications:send_review_complete - ✅ 发送审查完成通知: Sakura520222/Sakura-AI-Reviewer#305 → 1 人
sakura-ai-reviewer  | 2026-05-15 06:20:38 | INFO     | backend.workers.review_worker:_send_review_complete_notification - 已发送审查完成通知: Sakura520222/Sakura-AI-Reviewer#305 → 1 人
sakura-ai-reviewer  | 2026-05-15 06:20:38 | INFO     | backend.workers.review_worker:process_review_task - [f3ddfcb6] 审查任务完成: Sakura520222/Sakura-AI-Reviewer#305, decision=comment
sakura-ai-reviewer  | 2026-05-15 06:20:38 | INFO     | backend.api.webhook:handle_github_webhook - 收到GitHub事件: pull_request_review
sakura-ai-reviewer  | 2026-05-15 06:20:38 | INFO     | backend.api.webhook:handle_github_webhook - 忽略事件类型: pull_request_review
sakura-ai-reviewer  | INFO:     104.22.104.97:0 - "POST /api/webhook/github HTTP/1.1" 200 OK
sakura-ai-reviewer  | INFO:     127.0.0.1:40712 - "GET /health HTTP/1.1" 200 OK
sakura-ai-reviewer  | 2026-05-15 06:20:51 | INFO     | backend.services.ai_reviewer.api_client:_retry_loop - ✅ AI调用成功（耗时 12.8秒，重试 0 次）
sakura-ai-reviewer  | 2026-05-15 06:20:53 | INFO     | backend.services.github_write_service:_commit_single_file - Committed .sakura/memory/2026-05-15_PR305_6534a80.md -> f375220f
sakura-ai-reviewer  | 2026-05-15 06:20:53 | INFO     | backend.services.github_write_service:commit_files - Committed 1 file(s) directly to Sakura520222/Sakura-AI-Reviewer:main -> f375220f
sakura-ai-reviewer  | 2026-05-15 06:20:53 | INFO     | backend.services.sakura_memory_service:reflect - 已写入反思: Sakura520222/Sakura-AI-Reviewer PR#305 [首次全量审查] (第239次反思)