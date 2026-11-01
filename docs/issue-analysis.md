# Issue 分析与 PR 关联

> Issue 分析如何处理正文、评论和图片，怎样判断 Issue 关系，以及如何配置 PR 与 Issue 的语义关联。

← [文档索引](README.md) · [README](https://github.com/Sakura520222/Sakura-AI#readme)

## Issue 分析

Issue 分析会整理分类、优先级、标签建议、重复项和可能关联的 PR。按配置可自动应用高置信度标签、指派仓库协作者、优化标题并发布分析评论。调查深度会根据内容有效性、信息完整度、影响和复杂度调整：广告或垃圾内容、信息不足的报告和简单问答会简要处理；复杂或高风险的问题会保留基于证据的调查。

输出使用 `<SAKURA_ISSUE_ANALYSIS>` 协议，字段和格式异常时会尝试多轮修复；无法修复时安全降级，不把无效输出当作成功结果。

默认中英文评论先给结论和处理建议，折叠元信息，并隐藏空建议。已有自定义模板继续可用，模板必须保留 `{summary}` 和 `{feasibility}`，其他受支持的变量可以省略。

### 图片输入

Issue 正文和评论中的截图会使用 GitHub 凭据安全下载。图片超过 5 MiB 时会优先压缩，目标约为 500 KiB，再作为多模态输入交给 AI。

图片分析要求同时满足两项条件：当前模型的高级配置标记支持图片输入，并且在 WebUI 配置中心启用图片多模态开关。旧 `/config` 入口仍兼容，并会转到对应的配置页面。

### Issue 关系预分析

主分析前，系统先检查开放 Issue 的 `duplicate`、`related` 或 `none` 关系。只有没有验证出开放重复项时，才继续检查关闭 Issue 的历史关系：`previously_resolved`、`regression`、`duplicate_closed`、`previously_rejected` 和 `related`。

不同工作流中的独立事件不能仅凭相似标题判为重复。关系结论需要置信度、原因、相同点、差异和可追溯的原文引用。主分析模型不能覆盖关系验证结果。关系保存在 `issue_relations`，并通过 API、评论和详情页展示；旧字段 `duplicate_of` 仍保留以兼容现有调用方。

首次同步共享 Issue 语料时会纳入开放和关闭的 Issue。后续同步标题、正文、状态、标签和关闭原因，不要求先完成 AI 分析。机器生成的正文区块不会成为召回依据。候选被取回后，系统会重新读取 GitHub 当前事实。

关系预分析使用重新读取的当前 Issue 正文、状态、标签、关闭原因和更新时间，不会改写调用方传入的数据或主分析使用的历史正文。启用评论后，当前 Issue 和候选 Issue 都会按配置惰性读取最新评论样本，并记录评论 ID、正文、创建和更新时间、总数及截断范围。被省略的讨论不会作为修复或拒绝的证据。

模型分析后，系统会再次读取当前 Issue 和已接受的候选；进入历史关系阶段时，也会复查先前接受的开放关系。空结果和 `none` 结论同样要求来源保持一致。来源在分析期间发生变化时返回 `stale_source`；缺失、不可用或格式异常时返回 `source_unavailable`。这两类结果都不产生重复结论，保留已使用的 token，并继续 Issue 主分析。来源比较只保证在结果返回时成立，GitHub 不提供覆盖随后远程发布的原子条件写入。

关系检查是辅助流程。读取、检索、重排或验证失败会保留可观察的失败状态，不会阻止主分析；取消则会继续向上传播。关系验证使用 `summary` 模型角色，并遵守共享软期限。

旧记录的 Issue 状态为 `NULL` 时，重新分析入口会先在数据库确认仓库归属，再读取 GitHub 当前状态。只有确认 Issue 仍开放才会入队；已关闭返回 `409`，来源不可用或格式异常时返回明确的安全错误。入队数据保留历史标题、正文和作者，关系阶段另行读取最新来源。短仓库名与完整仓库名的历史记录按相同所有者作用域计算分析版本。

### 关系分析配置

以下配置可在 WebUI 配置中心动态修改，默认值来自 Settings。旧 `/config` 入口仍兼容。

| 配置项 | 默认值 | 用途与边界 |
| --- | --- | --- |
| `issue_detect_duplicates` | `true` | 启用开放和历史 Issue 关系预分析 |
| `issue_include_comments` | `true` | 在主分析及关系分析中使用评论；关系模型读取有界的最新评论样本 |
| `issue_relation_max_candidates` | `5` | 每个开放或历史阶段的候选数，允许 `1` 至 `200`；读取来源和检索前校验 |
| `issue_relation_max_input_tokens` | `64000` | 每个开放或历史阶段的完整请求估算输入上限，包含正文、评论、JSON、语言和系统提示；还受全部摘要候选的上下文、输出及协议预留限制 |
| `issue_relation_candidate_max_comments` | `20` | 启用 `issue_include_comments` 时，当前 Issue 和每个候选惰性读取的最新评论数上限；配置名沿用既有名称 |
| `issue_relation_candidate_comment_max_chars` | `4000` | 当前 Issue 和候选 Issue 的每条评论原文字符上限；截断会明确标记 |
| `issue_relation_similarity_threshold` | `0.75` | Issue 关系候选的余弦相似度阈值 |
| `issue_relation_confidence_threshold` | `0.85` | 普通关系和历史关系的验证阈值 |
| `issue_duplicate_confidence_threshold` | `0.95` | 开放 Issue 重复关系的验证阈值 |
| `enable_semantic_issue_linking` | `true` | 启用 PR 与 Issue 语义关联 |
| `semantic_issue_similarity_threshold` | `0.65` | PR 候选余弦阈值；必须是 `0` 至 `1` 之间的有限数值，检索前校验 |
| `semantic_issue_max_links` | `5` | PR 候选数，允许 `1` 至 `200`；Settings 和运行时检索前校验范围一致 |
| `pr_issue_related_confidence_threshold` | `0.85` | PR 普通关联的验证阈值；必须是 `0` 至 `1` 之间的有限数值 |
| `pr_issue_closing_confidence_threshold` | `0.95` | PR 完整修复关联的验证阈值；必须是 `0` 至 `1` 之间的有限数值 |
| `pr_issue_max_files` | `128` | PR 关系验证时惰性读取的文件数上限；超限或补丁不完整时保留现有关联 |
| `pr_issue_max_input_tokens` | `64000` | 完整验证请求的估算输入上限，包含 PR 描述、Issue、JSON 和系统提示；还受所有 `summary` 候选模型的上下文、输出预算和协议预留限制 |
| `issue_corpus_freshness_seconds` | `60` | 共享语料的最短同步间隔；`0` 表示每次检索前都同步 |
| `issue_corpus_batch_size` | `100` | 语料嵌入和写入的批次上限 |
| `issue_candidate_pool_multiplier` | `3` | 允许 `1` 至 `10`；语料同步前校验。固定召回池为候选上限乘以该值，并受语料总量限制；排除项不会扩大查询或触发补页 |
| `rerank_score_threshold` | `0.6` | PR/Issue 关系重排阈值；必须是 `0` 至 `1` 之间的有限数值。启用重排时，无效阈值会在请求提供商前被拒绝 |

每个关系流程最多接收 200 个目标候选，候选池倍数最高为 10，因此初始召回池最多 2,000 条。Settings、动态配置和运行时使用相同范围；非法值会被拒绝，不会静默截断。

输入预算会估算完整请求。超过上限或无法确认预算时，关系阶段不会给出重复结论，会保留已用 token 并继续主分析。PR 关联的置信度阈值或语义召回阈值无效时，同步会明确失败，即使没有候选也一样；数据库中的既有关联和 PR 机器生成区块不会因此清空。启用严格重排时，无效阈值会在调用提供商前失败。严格重排失败会保留已有 PR 关联，Issue 关系阶段记录失败，主分析继续运行。显式关闭重排以及普通 RAG 的非严格降级行为不变。

### PR 与 Issue 语义关联

PR 关联先从共享 Issue 语料召回开放候选，再用独立关系验证分别检查 PR 人工描述、实际补丁和 Issue 要求。完整修复输出 `Closes`，部分关联输出 `Related to`。验证成功后，系统会替换或清空旧的机器关联区块，同时保留人工添加的引用。检索、重排或验证失败时不会推断新关联，也会保留已有数据库关联和 PR 链接。

默认余弦召回阈值为 `0.65`，用于从较短的 PR 描述中找回正文较长的 Issue。候选仍须经过重排和独立验证；降低召回阈值本身不会建立或关闭关联。

每次成功检索都会写一条 INFO 摘要，空结果也会记录。字段含义如下：

| 字段 | 含义 |
| --- | --- |
| `recalled` | 向量候选池返回的数量 |
| `eligible` | 通过身份和排除项检查的数量 |
| `cosine_matches` | 达到余弦阈值的数量 |
| `hydrated` | 当前 GitHub 来源仍有效的数量 |
| `candidates` | 重排后的候选数量 |
| `max_cosine` | 余弦过滤前 eligible 候选的最高分；没有可比较候选时为 `none` |

摘要不包含查询内容、Issue 标题或正文，可用于区分语料为空、余弦筛选无结果和重排后无候选。

已有 `app_config` 值和显式环境配置优先于新默认值。升级不会自动把已保存的 `semantic_issue_similarity_threshold` 从 `0.8` 改成 `0.65`。已有部署可在 WebUI 配置中心将其设为 `0.65`，再结合 INFO 摘要校准。

### PR 补丁验证

PR 关系验证会按 unified diff 的 hunk 解析真实增加和删除行，并核对 hunk 两侧跨度与 GitHub 提供的行数。hunk 内以 `++` 或 `--` 开头的源码仍按其实际变更方向计数和引用。

GitHub 明确报告增加和删除行均为零的文件，可以没有补丁，或只有纯元数据补丁，例如纯重命名或权限变化。此类文件仍计入快照预算，但不能作为代码修复证据。GitHub 报告有代码行变更时，补丁必须完整且行数匹配；缺失、截断、格式异常或行数不符都会使关联同步失败，并保留已有链接。

### 调用与存储边界

调用 `IssueService.detect_duplicates` 时必须传入正整数 `current_issue_number`，关系流程才能读取并复核真实 Issue 来源。省略该参数或传入 `None` 等无效编号会直接报错，不会返回看似成功的空列表。有效编号的调用方式和重复项结果格式保持不变。

MySQL 自动结构迁移会幂等地将 `issue_analyses.issue_relations` 和 `issue_analyses.analysis_detail` 扩展为 `LONGTEXT`，以保存完整关系证据和分析 JSON。安装 PR-Issue 唯一索引前，数据库会按仓库、PR、Issue 和关联类型分组去重，每组保留 ID 最大的记录及其证据。不同仓库范围和关联类型分别保留；迁移不需要把整张表加载到 Python，也不逐条删除重复记录。

共享语料写入和 PR 关联同步的串行化范围是随附部署的单进程、单事件循环。多个独立进程或实例同时写入同一语料时，需要额外协调。

## English summary

Sakura AI adjusts Issue investigation depth to the report's completeness, impact and complexity. Depending on configuration, it can apply high-confidence labels, assign repository collaborators, improve titles and publish analysis comments. Output follows the `<SAKURA_ISSUE_ANALYSIS>` protocol with multi-round repair and safe degradation for invalid results. Comments default to concise Chinese or English conclusions and recommended actions. Custom templates remain supported; `{summary}` and `{feasibility}` are required, while other supported variables are optional. Screenshots in Issue bodies and comments are downloaded using GitHub credentials. Images over 5 MiB are compressed toward 500 KiB. Image analysis requires both the model's image-input capability and the WebUI configuration-center switch. The legacy `/config` entry remains compatible and redirects to the corresponding settings page.

Before the main analysis, the relationship workflow checks open Issues for `duplicate`, `related` or `none`. It checks closed Issues for `previously_resolved`, `regression`, `duplicate_closed`, `previously_rejected` or `related` only when no open duplicate has been verified. Similar titles alone cannot make independent workflow events duplicates. Decisions require confidence, a reason, similarities, differences and traceable source quotes. The main model cannot override verification. Results are stored in `issue_relations` and exposed through the API, comments and detail pages; `duplicate_of` remains for compatibility.

The shared corpus initially includes open and closed Issues. Later syncs update titles, bodies, state, labels and closure reason without requiring prior AI analysis. Generated body sections are excluded from recall. The workflow hydrates current GitHub facts, rechecks the current Issue and accepted candidates after inference, and rechecks accepted open relations before the historical phase. With comments enabled, it lazily samples the newest comments for the current Issue and candidates, subject to the limits below. It retains comment IDs, bodies, timestamps, total counts and truncation ranges. Omitted discussion is not evidence of a fix or rejection.

A changed source returns `stale_source`; a missing, unavailable or malformed source returns `source_unavailable`. Neither result admits a duplicate, and both retain token usage while allowing main Issue analysis to continue. The source comparison holds at the return boundary; GitHub does not provide an atomic conditional write covering later remote publication. Relationship checks use the `summary` model role and a shared soft deadline. Auxiliary failures remain observable and do not stop the main analysis; cancellation propagates.

PR-Issue linking recalls open candidates from the shared corpus, then independently verifies the human PR description, actual patch and Issue requirements. Complete fixes emit `Closes`; partial relationships emit `Related to`. A successful verification replaces or clears the generated link block while preserving human references. Retrieval, reranking or validation failures infer no new link and preserve existing database links and PR references. The default PR cosine threshold is `0.65`; candidates still need reranking and independent verification. An upgrade does not change a saved `0.8` value automatically. Lowering the recall threshold alone does not establish or close a relationship.

Invalid PR confidence or semantic-recall thresholds fail synchronization even when the candidate set is empty, without clearing existing database links or the generated PR block. Candidate counts and pool multipliers are validated consistently, never silently clamped. Strict reranking rejects an invalid threshold before contacting the provider. A strict reranking failure preserves existing PR links, records the Issue relationship failure and lets main analysis continue. Explicitly disabled reranking and ordinary RAG's non-strict fallback retain their existing behavior.

A successful retrieval, including an empty result, emits an INFO summary. `recalled` counts vector-pool rows, `eligible` counts rows passing identity and exclusion checks, `cosine_matches` counts rows meeting the cosine threshold, `hydrated` counts valid current GitHub sources, `candidates` counts reranked results, and `max_cosine` is the highest eligible score before cosine filtering (`none` when no score is available). The log contains no query, Issue title or body. Existing `app_config` values and explicit environment settings override new defaults. Configure `semantic_issue_similarity_threshold` to `0.65` in the WebUI configuration center to adopt the current recall default.

Patch verification parses real added and deleted lines from unified-diff hunks and checks hunk spans against GitHub line counts. Source lines beginning with `++` or `--` inside a hunk still count according to their change direction. Files GitHub reports with zero additions and deletions may have no patch or a metadata-only patch, such as a rename or permission change. They count toward the snapshot budget but cannot prove a code fix. When GitHub reports changed lines, a missing, truncated, malformed or inconsistent patch fails synchronization and preserves existing links.

Calls to `IssueService.detect_duplicates` must provide a positive integer `current_issue_number`; missing or invalid values raise an error rather than returning a success-shaped empty list. MySQL schema migration idempotently expands `issue_analyses.issue_relations` and `issue_analyses.analysis_detail` to `LONGTEXT`. Before installing the PR-Issue unique index, duplicate rows are grouped by repository, PR, Issue and link type, retaining the greatest-ID row and its evidence. Corpus writes and PR link synchronization are serialized only within the shipped single process and event loop; independent processes or instances need additional coordination.

### Relationship settings

Settings defaults can be changed in the WebUI configuration center. The legacy `/config` entry remains compatible.

| Setting | Default | Purpose and limits |
| --- | --- | --- |
| `issue_detect_duplicates` | `true` | Enable open and historical Issue relationship pre-analysis |
| `issue_include_comments` | `true` | Use comments in main analysis and bounded newest-comment samples for relationship analysis |
| `issue_relation_max_candidates` | `5` | Candidates per open or historical phase; `1` to `200`, validated before source reads and retrieval |
| `issue_relation_max_input_tokens` | `64000` | Estimated full request input limit per phase, including bodies, comments, JSON, language and system prompt; summary-candidate context, output and protocol reserves also apply |
| `issue_relation_candidate_max_comments` | `20` | Lazy newest-comment limit for the current Issue and each candidate when comments are enabled; existing setting name retained |
| `issue_relation_candidate_comment_max_chars` | `4000` | Source character limit per comment; truncation is explicit and omitted discussion cannot prove a fix or rejection |
| `issue_relation_similarity_threshold` | `0.75` | Issue relationship candidate cosine threshold |
| `issue_relation_confidence_threshold` | `0.85` | Related and historical relationship verification threshold |
| `issue_duplicate_confidence_threshold` | `0.95` | Open duplicate verification threshold |
| `enable_semantic_issue_linking` | `true` | Enable semantic PR-Issue linking |
| `semantic_issue_similarity_threshold` | `0.65` | PR candidate cosine threshold; finite value from `0` to `1`, checked before retrieval |
| `semantic_issue_max_links` | `5` | PR candidate limit, `1` to `200`, checked in Settings and before runtime retrieval |
| `pr_issue_related_confidence_threshold` | `0.85` | PR related-link verification threshold; finite value from `0` to `1` |
| `pr_issue_closing_confidence_threshold` | `0.95` | PR complete-fix verification threshold; finite value from `0` to `1` |
| `pr_issue_max_files` | `128` | Lazy file-read limit for PR verification; exceeding it or an incomplete patch preserves existing links |
| `pr_issue_max_input_tokens` | `64000` | Estimated full verification request input limit, including description, Issues, JSON and system prompt; context, output and protocol reserves for all `summary` candidates also apply |
| `issue_corpus_freshness_seconds` | `60` | Minimum shared-corpus sync interval; `0` syncs before every retrieval |
| `issue_corpus_batch_size` | `100` | Corpus embedding and write batch limit |
| `issue_candidate_pool_multiplier` | `3` | `1` to `10`, checked before corpus sync; pool size is candidate limit × multiplier, capped by corpus size; exclusions do not enlarge the query or trigger paging refill |
| `rerank_score_threshold` | `0.6` | PR/Issue reranking threshold; finite value from `0` to `1`; invalid values are rejected before a reranker provider request |
