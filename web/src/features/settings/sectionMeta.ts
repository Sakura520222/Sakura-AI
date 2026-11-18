import {
  Bot,
  DatabaseBackup,
  GitPullRequest,
  Info,
  MessageSquare,
  Settings2,
  Sparkles,
  Tags,
  UserRound,
  type LucideIcon,
} from "lucide-react";
import type { Category, ConfigField } from "./types";

export interface CategoryMeta {
  id: Category | "about";
  label: readonly [string, string];
  icon: LucideIcon;
  description?: readonly [string, string];
}

export const CATEGORY_ITEMS: readonly CategoryMeta[] = [
  {
    id: "personal",
    label: ["个人偏好", "Personal"],
    icon: UserRound,
    description: [
      "界面语言、外观主题、账号安全与个人通知",
      "UI language, theme appearance, account security and notifications",
    ],
  },
  {
    id: "ai",
    label: ["AI 与模型", "AI & models"],
    icon: Sparkles,
    description: [
      "连接 AI 账号，分配任务模型与回退链，调整能力与调用策略",
      "Connect AI accounts, assign task models & fallbacks, and tune request policy",
    ],
  },
  {
    id: "review",
    label: ["代码审查", "Code review"],
    icon: GitPullRequest,
    description: [
      "触发并发、批准阻断策略、分级提示词与上下文工具",
      "Triggers, approval/blocking policy, review tiers and context tools",
    ],
  },
  {
    id: "issue",
    label: ["Issue 分析", "Issue analysis"],
    icon: MessageSquare,
    description: [
      "自动分析、相似查重、负责人推荐与评论模板",
      "Automatic analysis, duplicate detection, assignee suggestions and templates",
    ],
  },
  {
    id: "agent",
    label: ["Agent", "Agents"],
    icon: Bot,
    description: [
      "任务执行环境、网络与验证策略、搜索抓取工具及插件权限",
      "Task runtime, sandbox & network policy, web tools and plugin permissions",
    ],
  },
  {
    id: "labels",
    label: ["标签与扫描", "Labels & scans"],
    icon: Tags,
    description: [
      "自动标签推荐、GitHub 标签定义、互斥规则与仓库扫描",
      "Automatic label recommendations, GitHub label definitions and repository scans",
    ],
  },
  {
    id: "system",
    label: ["系统", "System"],
    icon: Settings2,
    description: [
      "应用监听、GitHub 接入、通知渠道、计费配额与实例维护",
      "Listener runtime, GitHub access, notification channels, billing quotas and maintenance",
    ],
  },
  {
    id: "backup",
    label: ["备份与恢复", "Backup & restore"],
    icon: DatabaseBackup,
    description: [
      "按范围导出 JSON 备份或从备份文件恢复配置与用户",
      "Export scoped JSON backups or restore configuration and user data",
    ],
  },
  {
    id: "about",
    label: ["关于", "About"],
    icon: Info,
    description: [
      "查看当前版本、项目文档与服务协议",
      "Inspect running version, documentation and legal terms",
    ],
  },
] as const;

export const LEGACY_CATEGORY_ALIASES: Record<string, Category> = {
  general: "review",
  strategies: "review",
};

export interface SectionDescriptor {
  id: string;
  kind: "group" | "panel";
  group?: string;
  panel?:
    | "accounts"
    | "model-capabilities"
    | "plugins"
    | "system-maintenance"
    | "backup";
  title?: readonly [string, string];
  summary?: readonly [string, string];
}

/**
 * Task-oriented section ordering and descriptions per category.
 * Any additional backend group not listed here is appended automatically so
 * new or synthetic test groups remain continuously visible.
 */
export const CATEGORY_SECTION_BLUEPRINTS: Record<
  Category,
  readonly SectionDescriptor[]
> = {
  personal: [
    {
      id: "preferences",
      kind: "group",
      group: "preferences",
      title: ["界面与输出偏好", "Appearance & output preferences"],
      summary: [
        "设置当前账号的界面语言、主题模式、列表每页条数与审查回复语言。",
        "Set UI language, color theme, page size and personal review output language.",
      ],
    },
  ],
  ai: [
    {
      id: "accounts",
      kind: "panel",
      panel: "accounts",
      title: ["1. 连接 AI 账号", "1. Connect AI accounts"],
      summary: [
        "添加供应商账号、API 密钥与可用模型清单，供下方任务绑定与能力设置调用。",
        "Add provider endpoints, credentials and model lists used by role bindings below.",
      ],
    },
    {
      id: "bindings",
      kind: "group",
      group: "bindings",
      title: ["2. 分配任务模型与回退链", "2. Task models & fallback chains"],
      summary: [
        "为主审查、变更摘要与 Agent 指定主模型和按优先级排列的备用模型。",
        "Assign primary models and ordered fallback chains for reviews, summaries and Agents.",
      ],
    },
    {
      id: "model-capabilities",
      kind: "panel",
      panel: "model-capabilities",
      title: ["3. 单模型能力与参数覆盖", "3. Per-model capabilities & parameters"],
      summary: [
        "从已启用账号中选择具体模型，按需调整上下文窗口、工具/视觉能力与推理参数。",
        "Select a model from an enabled account to override context limits, capabilities and reasoning.",
      ],
    },
    {
      id: "ai_strategy",
      kind: "group",
      group: "ai_strategy",
      title: ["4. 调用超时、回退策略与活动采集", "4. Request policy, fallback & capture"],
      summary: [
        "控制请求重试超时、回退候选上限、上下文自动压缩以及运行活动记录范围。",
        "Configure timeouts, retries, fallback candidate limits, compression and activity capture.",
      ],
    },
    {
      id: "rag",
      kind: "group",
      group: "rag",
      title: ["检索增强（RAG）", "Retrieval-augmented generation (RAG)"],
      summary: [
        "启用向量知识检索并管理 ChromaDB 持久化目录。",
        "Enable vector retrieval and manage the ChromaDB storage directory.",
      ],
    },
    {
      id: "embedding",
      kind: "group",
      group: "embedding",
      title: ["嵌入模型服务", "Embedding service"],
      summary: [
        "配置生成代码切片与文档向量所使用的嵌入模型与接口。",
        "Configure the embedding model and endpoint used for vector indexing.",
      ],
    },
    {
      id: "rerank",
      kind: "group",
      group: "rerank",
      title: ["检索结果重排序", "Reranking service"],
      summary: [
        "配置对向量检索候选片段进行二次相关度排序的服务。",
        "Configure secondary relevance reranking for retrieved context chunks.",
      ],
    },
  ],
  review: [
    {
      id: "review_basic",
      kind: "group",
      group: "review_basic",
      title: ["触发、并发与状态检查", "Triggers, concurrency & Check Runs"],
      summary: [
        "管理 PR 自动审查、并发与超时上限、最小触发间隔及 GitHub Check Runs。",
        "Control automatic PR review, concurrency, timeouts and GitHub Check Runs.",
      ],
    },
    {
      id: "review_strategy",
      kind: "group",
      group: "review_strategy",
      title: ["行内代码评论", "Inline diff comments"],
      summary: [
        "控制是否将发现的问题直接评论在 PR 代码差异对应行。",
        "Control whether findings are published directly on PR diff lines.",
      ],
    },
    {
      id: "strategy.review_policy",
      kind: "group",
      group: "strategy.review_policy",
      title: ["评审结论与批准/阻断策略", "Approval & blocking policy"],
      summary: [
        "配置自动批准与请求修改的评分门槛、单仓库覆盖及中英文评论模板。",
        "Configure Approve / Request Changes thresholds, repo overrides and templates.",
      ],
    },
    {
      id: "strategy.strategies",
      kind: "group",
      group: "strategy.strategies",
      title: ["审查分级与提示词", "Review tiers & prompts"],
      summary: [
        "按变更文件数与行数匹配快速、标准、深度或大型审查档位及专属提示词。",
        "Match quick, standard, deep and large review tiers by PR size and customize prompts.",
      ],
    },
    {
      id: "strategy.file_filters",
      kind: "group",
      group: "strategy.file_filters",
      title: ["文件过滤规则", "File filter rules"],
      summary: [
        "配置审查时跳过的目录、扩展名以及识别为源码的后缀列表。",
        "Specify skipped paths, ignored extensions and recognized source code extensions.",
      ],
    },
    {
      id: "pr_summary",
      kind: "group",
      group: "pr_summary",
      title: ["PR 变更摘要", "PR change summary"],
      summary: [
        "控制是否自动生成 Pull Request 变更总结。",
        "Control automatic Pull Request summary generation.",
      ],
    },
    {
      id: "strategy.pr_summary",
      kind: "group",
      group: "strategy.pr_summary",
      title: ["PR 摘要提示词", "PR summary prompts"],
      summary: [
        "自定义生成 PR 摘要时使用的系统提示词与用户模板。",
        "Customize system and user templates used for PR summaries.",
      ],
    },
    {
      id: "pr_dependency_graph",
      kind: "group",
      group: "pr_dependency_graph",
      title: ["PR 依赖关系图", "PR dependency graph"],
      summary: [
        "配置变更模块依赖图开关、节点/文件上限及生成方式。",
        "Configure dependency graph generation mode and node/file limits.",
      ],
    },
    {
      id: "strategy.pr_dependency_graph",
      kind: "group",
      group: "strategy.pr_dependency_graph",
      title: ["PR 依赖图提示词", "PR dependency graph prompts"],
      summary: [
        "自定义 AI 模式下生成 PR 依赖关系图的提示词模板。",
        "Customize prompts used when generating dependency graphs in AI mode.",
      ],
    },
    {
      id: "semantic_issue_linking",
      kind: "group",
      group: "semantic_issue_linking",
      title: ["关联 Issue 识别", "Related Issue linking"],
      summary: [
        "调整语义关联与关闭判定所需的相似度、置信度与数量限制。",
        "Tune similarity and confidence thresholds for linking Issues to PRs.",
      ],
    },
    {
      id: "code_index",
      kind: "group",
      group: "code_index",
      title: ["代码切片索引", "Code chunk indexing"],
      summary: [
        "管理仓库源码切片索引开关及分块大小与重叠参数。",
        "Manage repository code chunk indexing, chunk size and overlap.",
      ],
    },
    {
      id: "strategy.context_enhancement",
      kind: "group",
      group: "strategy.context_enhancement",
      title: ["上下文工具与仓库记忆", "Context tools & repository memory"],
      summary: [
        "配置审查工具限额、文件内容搜索、CI 失败上下文与 .sakura 长期记忆。",
        "Configure file/search/Git tool limits, CI failure context and .sakura memory.",
      ],
    },
  ],
  issue: [
    {
      id: "issue_analysis",
      kind: "group",
      group: "issue_analysis",
      title: ["自动分析、指派与查重", "Automatic analysis, assignment & deduplication"],
      summary: [
        "控制新 Issue 自动分析、相似问题检测、指派建议与并发限制。",
        "Control Issue analysis, duplicate detection, assignee suggestions and concurrency.",
      ],
    },
    {
      id: "strategy.issue_analysis",
      kind: "group",
      group: "strategy.issue_analysis",
      title: ["分类规则、优先级与评论模板", "Categories, priority rules & templates"],
      summary: [
        "定义 Issue 分类关键词、紧急度判定规则及中英文回复模板。",
        "Define Issue categories, priority rules and bilingual comment templates.",
      ],
    },
  ],
  agent: [
    {
      id: "agent_team",
      kind: "group",
      group: "agent_team",
      title: ["任务执行、隔离沙箱与闭环验证", "Execution, sandbox & PR closed loop"],
      summary: [
        "配置 Agent 启用范围、沙箱与网络策略、依赖安装、测试运行及 Draft PR 审查闭环。",
        "Configure Agent allowlists, sandbox/network policy, test execution and Draft PR review.",
      ],
    },
    {
      id: "web_search",
      kind: "group",
      group: "web_search",
      title: ["联网网页搜索", "Web search"],
      summary: [
        "配置外部搜索引擎供应商、API 密钥、返回条数与超时限制。",
        "Configure search provider, API key, result count and timeout.",
      ],
    },
    {
      id: "fetch_url",
      kind: "group",
      group: "fetch_url",
      title: ["网页链接抓取", "URL fetching"],
      summary: [
        "控制网页读取工具开关、域名访问策略、HTTPS 强制与下载体积上限。",
        "Control URL fetching, domain policy, HTTPS enforcement and size limits.",
      ],
    },
    {
      id: "plugins",
      kind: "panel",
      panel: "plugins",
      title: ["插件与运行权限", "Plugins & runtime permissions"],
      summary: [
        "配置 MCP 工具服务、生命周期钩子与工作区读写权限级别。",
        "Configure MCP services, lifecycle hooks and workspace permission profiles.",
      ],
    },
  ],
  labels: [
    {
      id: "label.recommendation",
      kind: "group",
      group: "label.recommendation",
      title: ["自动标签推荐", "Automatic label recommendation"],
      summary: [
        "控制自动打标开关、推荐置信度门槛以及缺失标签自动创建。",
        "Control automatic labeling, confidence threshold and auto-creation.",
      ],
    },
    {
      id: "label.definitions",
      kind: "group",
      group: "label.definitions",
      title: ["GitHub 标签定义", "GitHub label definitions"],
      summary: [
        "管理各标签在 GitHub 同步的颜色与说明，支持增删自定义标签。",
        "Manage GitHub label colors and descriptions, or add custom labels.",
      ],
    },
    {
      id: "label.conflict_rules",
      kind: "group",
      group: "label.conflict_rules",
      title: ["标签互斥规则", "Label conflict rules"],
      summary: [
        "配置不能同时应用在同一 PR 或 Issue 上的互斥标签列表。",
        "Configure mutually exclusive label groups.",
      ],
    },
    {
      id: "repo_scan",
      kind: "group",
      group: "repo_scan",
      title: ["仓库扫描策略", "Repository scanning"],
      summary: [
        "控制周期性代码扫描开关、执行间隔、冷却时间与自动建单严重度。",
        "Control scheduled repository scans, cooldowns and Issue creation severity.",
      ],
    },
    {
      id: "strategy.scan",
      kind: "group",
      group: "strategy.scan",
      title: ["仓库扫描提示词", "Repository scan prompt"],
      summary: [
        "自定义全仓质量与安全扫描所使用的系统提示词。",
        "Customize the system prompt used for full-repository scans.",
      ],
    },
  ],
  system: [
    {
      id: "application",
      kind: "group",
      group: "application",
      title: ["应用监听与运行基础", "Application listener & runtime"],
      summary: [
        "配置站点域名、HTTP 监听地址与端口、时区、日志级别及签名密钥。",
        "Configure public domain, bind host/port, timezone, log level and signing secrets.",
      ],
    },
    {
      id: "i18n",
      kind: "group",
      group: "i18n",
      title: ["默认语言与全局输出", "Default language & output"],
      summary: [
        "设置实例默认界面语言与全局 AI 回复语言。",
        "Set instance default UI language and global AI output language.",
      ],
    },
    {
      id: "database",
      kind: "group",
      group: "database",
      title: ["数据库与缓存连接", "Database & Redis connections"],
      summary: [
        "查看由部署配置管理的数据库与 Redis 连接；连接测试位于底部系统维护区。",
        "View deployment-owned database and Redis connections; probe them in System maintenance below.",
      ],
    },
    {
      id: "github_app",
      kind: "group",
      group: "github_app",
      title: ["主 GitHub App 接入", "Primary GitHub App"],
      summary: [
        "配置接收 Webhook 与执行仓库审查操作的主 GitHub App 凭据。",
        "Configure the primary GitHub App ID, private key and webhook secret.",
      ],
    },
    {
      id: "github_oauth",
      kind: "group",
      group: "github_oauth",
      title: ["GitHub OAuth 登录", "GitHub OAuth sign-in"],
      summary: [
        "配置用户登录所使用的 GitHub OAuth Client ID、密钥与回调地址。",
        "Configure GitHub OAuth credentials and redirect URLs for user sign-in.",
      ],
    },
    {
      id: "star_aid_app",
      kind: "group",
      group: "star_aid_app",
      title: ["用户授权 GitHub App", "User authorization GitHub App"],
      summary: [
        "配置用于个人仓库授权与互助功能的独立 GitHub App。",
        "Configure the user-scoped GitHub App used for repository authorization.",
      ],
    },
    {
      id: "notifications",
      kind: "group",
      group: "notifications",
      title: ["通知投递与 Webhook 策略", "Notification delivery & Webhook policy"],
      summary: [
        "管理通知并发、重试退避、外部 Webhook 安全限制与验证码有效期。",
        "Manage notification concurrency, retry backoff, Webhook policy and verification limits.",
      ],
    },
    {
      id: "email",
      kind: "group",
      group: "email",
      title: ["邮件通知（SMTP）", "Email notifications (SMTP)"],
      summary: [
        "配置发送邮件通知与验证邮件所需的 SMTP 连接与发件人信息。",
        "Configure SMTP connection, encryption, credentials and sender address.",
      ],
    },
    {
      id: "telegram",
      kind: "group",
      group: "telegram",
      title: ["Telegram 通知机器人", "Telegram notification bot"],
      summary: [
        "配置可选的私人 Telegram 通知绑定机器人与绑定有效期。",
        "Configure the optional Telegram notification binding bot.",
      ],
    },
    {
      id: "payment",
      kind: "group",
      group: "payment",
      title: ["计费销售与支付网关", "Billing sales & payment gateways"],
      summary: [
        "控制套餐销售入口，并配置 Stripe、Paddle、支付宝、NOWPayments 与 TRON 网关。",
        "Control sales entry points and configure Stripe, Paddle, Alipay, NOWPayments and TRON.",
      ],
    },
    {
      id: "registration_quota",
      kind: "group",
      group: "registration_quota",
      title: ["自注册配额倍率", "Self-registration quota multiplier"],
      summary: [
        "调整自注册新用户相对基础额度的初始倍率。",
        "Adjust initial quota multiplier for self-registered users.",
      ],
    },
    {
      id: "init_quota",
      kind: "group",
      group: "init_quota",
      title: ["初始用户与管理员额度", "Initial user & admin quotas"],
      summary: [
        "设置新账号默认获得的日/周/月审查、Issue 与 Agent 初始配额。",
        "Set initial daily, weekly and monthly quotas for new users and admins.",
      ],
    },
    {
      id: "star_aid",
      kind: "group",
      group: "star_aid",
      title: ["仓库互助全局策略", "Star community global policy"],
      summary: [
        "配置仓库互助总开关、自动互助调度间隔、每日上限与仓库摘要语言。",
        "Configure global Star community switch, scheduler intervals, daily limits and summaries.",
      ],
    },
    {
      id: "maintenance",
      kind: "panel",
      panel: "system-maintenance",
      title: ["系统维护与连接诊断", "System maintenance & diagnostics"],
      summary: [
        "执行数据库/Redis 连接测试、查询 GitHub App Slug、安全重启或重置数据库。",
        "Test database/Redis connections, look up App slugs, restart the service or reset the database.",
      ],
    },
  ],
  backup: [
    {
      id: "backup",
      kind: "panel",
      panel: "backup",
      title: ["导出与恢复备份", "Export & restore backups"],
      summary: [
        "导出当前配置或用户安全数据，或从备份文件恢复（自动保护部署连接配置）。",
        "Export configuration or user backups, or restore from a file while preserving deployment connections.",
      ],
    },
  ],
};

export interface ResolvedSection {
  id: string;
  anchorId: string;
  kind: "group" | "panel";
  group?: string;
  panel?: SectionDescriptor["panel"];
  title: string;
  summary?: string;
  fields: ConfigField[];
}

export function sectionAnchorId(id: string): string {
  return `section-${id}`;
}

export function resolveCategorySections(
  category: Category,
  fields: ConfigField[],
  groupsMap: Record<string, string>,
  translate: (zh: string, en: string) => string,
  showFields = true,
): ResolvedSection[] {
  const blueprints = CATEGORY_SECTION_BLUEPRINTS[category] ?? [];
  const byGroup = new Map<string, ConfigField[]>();
  for (const field of fields) {
    const bucket = byGroup.get(field.group);
    if (bucket) bucket.push(field);
    else byGroup.set(field.group, [field]);
  }
  const seenGroups = new Set<string>();
  const resolved: ResolvedSection[] = [];

  for (const item of blueprints) {
    if (item.kind === "panel") {
      resolved.push({
        id: item.id,
        anchorId: sectionAnchorId(item.id),
        kind: "panel",
        panel: item.panel,
        title: item.title ? translate(item.title[0], item.title[1]) : item.id,
        summary: item.summary
          ? translate(item.summary[0], item.summary[1])
          : undefined,
        fields: [],
      });
      continue;
    }
    if (!showFields || !item.group) continue;
    const groupFields = byGroup.get(item.group);
    if (!groupFields?.length) continue;
    seenGroups.add(item.group);
    resolved.push({
      id: item.id,
      anchorId: sectionAnchorId(item.id),
      kind: "group",
      group: item.group,
      title: item.title
        ? translate(item.title[0], item.title[1])
        : (groupsMap[item.group] ?? item.group),
      summary: item.summary
        ? translate(item.summary[0], item.summary[1])
        : undefined,
      fields: groupFields,
    });
  }

  if (showFields) {
    for (const [group, groupFields] of byGroup.entries()) {
      if (seenGroups.has(group) || !groupFields.length) continue;
      resolved.push({
        id: group,
        anchorId: sectionAnchorId(group),
        kind: "group",
        group,
        title: groupsMap[group] ?? group,
        fields: groupFields,
      });
    }
  }

  return resolved;
}

export function lookupSectionTitle(
  category: Category,
  group: string,
  groupsMap: Record<string, string>,
  translate: (zh: string, en: string) => string,
): string {
  if (group === "model")
    return translate("3. 单模型能力与参数覆盖", "3. Per-model capabilities & parameters");
  const match = (CATEGORY_SECTION_BLUEPRINTS[category] ?? []).find(
    (item) => item.group === group || item.id === group,
  );
  if (match?.title) return translate(match.title[0], match.title[1]);
  return groupsMap[group] ?? group;
}

export interface SearchableWorkflowItem {
  id: string;
  category: Category;
  to: string;
  section: readonly [string, string];
  title: readonly [string, string];
  description: readonly [string, string];
  keywords: string;
  adminOnly: boolean;
}

/** Value-free navigation targets for dedicated settings workflows (no secrets or user data). */
export const WORKFLOW_SEARCH_ITEMS: readonly SearchableWorkflowItem[] = [
  {
    id: "workflow/personal-security",
    category: "personal",
    to: "/settings/personal?tab=security",
    section: ["账号安全", "Security"],
    title: ["两步验证与通行密钥（2FA / Passkey）", "Two-factor authentication & Passkeys"],
    description: [
      "设置 TOTP 身份验证器、恢复码或设备指纹/安全密钥登录。",
      "Configure TOTP authenticator apps, recovery codes and WebAuthn passkeys.",
    ],
    keywords: "2fa mfa totp passkey webauthn authenticator 验证码 两步验证 通行密钥 恢复码 安全",
    adminOnly: false,
  },
  {
    id: "workflow/personal-notifications",
    category: "personal",
    to: "/settings/personal?tab=notifications",
    section: ["通知与绑定", "Notifications"],
    title: ["个人通知渠道与订阅偏好", "Personal delivery channels & subscriptions"],
    description: [
      "绑定个人邮箱、Telegram 或 Webhook 接收渠道，并设置事件通知规则。",
      "Bind personal email, Telegram or Webhook channels and configure event subscriptions.",
    ],
    keywords: "notification email telegram webhook inbox 通知 订阅 绑定 邮箱 电报 渠道",
    adminOnly: false,
  },
  {
    id: "workflow/ai-accounts",
    category: "ai",
    to: "/settings/ai#section-accounts",
    section: ["1. 连接 AI 账号", "1. Connect AI accounts"],
    title: ["AI 供应商账号管理", "AI provider accounts"],
    description: [
      "添加、测试、编辑或停用 OpenAI、Anthropic、Gemini 及自定义兼容接口账号。",
      "Add, test, edit or disable OpenAI, Anthropic, Gemini and custom AI accounts.",
    ],
    keywords: "ai account provider openai anthropic claude gemini deepseek api key 供应商 账号 密钥 连接测试",
    adminOnly: true,
  },
  {
    id: "workflow/model-capabilities",
    category: "ai",
    to: "/settings/ai#section-model-capabilities",
    section: ["3. 单模型能力与参数覆盖", "3. Per-model capabilities & parameters"],
    title: ["单模型上下文窗口、视觉工具与推理参数", "Per-model context window, capabilities & reasoning"],
    description: [
      "按已启用账号中的具体模型覆盖 Token 上限、流式用量、思考模式与采样参数。",
      "Override context window, max output tokens, tool/vision capabilities and thinking mode per model.",
    ],
    keywords: "model capability context window max tokens vision thinking temperature 模型能力 上下文 思考 视觉",
    adminOnly: true,
  },
  {
    id: "workflow/agent-plugins",
    category: "agent",
    to: "/settings/agent#plugins",
    section: ["插件与运行权限", "Plugins & runtime permissions"],
    title: ["MCP 服务、钩子与 Agent 运行权限", "MCP servers, hooks & runtime permission profile"],
    description: [
      "管理 Agent 可调用的 MCP 工具服务、生命周期钩子与只读/工作区写入权限。",
      "Manage MCP servers, lifecycle hooks and read-only or workspace-write permission profiles.",
    ],
    keywords: "mcp plugin hook permission profile 插件 钩子 运行权限 工作区",
    adminOnly: true,
  },
  {
    id: "workflow/system-maintenance",
    category: "system",
    to: "/settings/system#section-maintenance",
    section: ["系统维护与连接诊断", "System maintenance & diagnostics"],
    title: ["连接测试、重启应用与重置数据库", "Connection probes, application restart & database reset"],
    description: [
      "测试数据库与 Redis 连通性、查找 GitHub App Slug、重启当前实例或清空重置数据库。",
      "Probe active database/Redis connections, look up GitHub App slugs, restart Sakura AI or reset the database.",
    ],
    keywords: "restart reboot test connection database redis slug reset 重启 测试连接 重置数据库 维护",
    adminOnly: true,
  },
  {
    id: "workflow/backup-restore",
    category: "backup",
    to: "/settings/backup#section-backup",
    section: ["导出与恢复备份", "Export & restore backups"],
    title: ["导出配置备份与导入恢复", "Export configuration backup & import restore"],
    description: [
      "按分类下载 JSON 备份文件，或上传备份文件恢复配置与用户数据。",
      "Download scoped JSON backups or upload a backup file to restore configuration and users.",
    ],
    keywords: "backup export import restore json 备份 导出 导入 恢复 迁移",
    adminOnly: true,
  },
];
