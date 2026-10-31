import { useState } from "react";
import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import {
  Link,
  useNavigate,
  useParams,
  useSearchParams,
} from "react-router-dom";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import {
  ArrowLeft,
  ArrowUpRight,
  Bot,
  GitBranch,
  Plus,
  RotateCcw,
  Send,
  Square,
  Trash2,
} from "lucide-react";
import { toast } from "sonner";
import { api } from "../../api/client";
import type { components } from "../../api/generated";
import { useAuth } from "../../app/auth";
import { useLocale } from "../../app/locale";
import { Dialog } from "../../components/ui/Dialog";
import { useConfirm } from "../../components/ui/ConfirmProvider";
import {
  Disclosure,
  EmptyState,
  ErrorState,
  Filters,
  FilterSelect,
  LoadingState,
  Markdown,
  PageHeader,
  Pagination,
  RefreshButton,
  safeExternalUrl,
  Section,
  StatusBadge,
  Timestamp,
} from "../../components/workflow/Workflow";
import "./agent.css";

type Task = components["schemas"]["AgentTaskDetail"];
type TaskPage = components["schemas"]["AgentTaskListResponse"];
type MutationResult = {
  success: boolean;
  task_id?: number;
  message?: string;
  error?: string;
};
const active = [
  "queued",
  "planning",
  "cloning",
  "editing",
  "self_reviewing",
  "validating",
  "pushing",
  "pr_opened",
  "external_reviewing",
  "iterating",
  "waiting_human",
];
export const taskSchema = z.object({
  issue_ref: z.string().trim().min(1),
  title: z.string(),
  summary: z.string(),
  base_branch: z.string(),
  branch_name: z.string(),
  priority: z.string(),
  status: z.string(),
});
type TaskForm = z.infer<typeof taskSchema>;

async function command(
  path: string,
  init: RequestInit,
): Promise<MutationResult> {
  const response = await api<MutationResult>(path, init);
  if (!response.success)
    throw new Error(response.message || response.error || "Request declined");
  return response;
}

export default function AgentPage() {
  const { id } = useParams();
  return id ? <AgentDetail key={id} id={id} /> : <AgentList />;
}

function AgentList() {
  const { t } = useLocale();
  const { permissions } = useAuth();
  const [params] = useSearchParams();
  const [creating, setCreating] = useState(false);
  const request = new URLSearchParams(params);
  if (request.has("search")) {
    request.set("q", request.get("search")!);
    request.delete("search");
  }
  const query = useQuery({
    queryKey: ["agent-tasks", request.toString()],
    queryFn: ({ signal }) =>
      api<TaskPage>(`/api/v1/agent/tasks?${request}`, { signal }),
    refetchInterval: 15_000,
  });
  return (
    <div>
      <PageHeader
        title="Agent"
        description={t(
          "把 Issue 交给 Agent，跟进计划、修改与审查。",
          "Delegate an issue and follow its plan, changes, and reviews.",
        )}
        actions={
          <>
            <RefreshButton
              busy={query.isFetching}
              onClick={() => void query.refetch()}
            />
            <button
              className="button primary"
              onClick={() => setCreating(true)}
            >
              <Plus size={17} />
              {t("新建任务", "New task")}
            </button>
          </>
        }
      />
      {permissions.super_admin && (
        <div className="agent-links">
          <Link to="/agent/candidates">{t("候选任务", "Candidate tasks")}</Link>
          <Link to="/agent/skills">{t("技能", "Skills")}</Link>
          <Link to="/agent/plugins">{t("插件", "Plugins")}</Link>
          <Link to="/agent/workspaces">{t("工作区", "Workspaces")}</Link>
        </div>
      )}
      <Filters
        placeholder={t("搜索任务或仓库", "Search tasks or repositories")}
      >
        <FilterSelect
          name="status"
          label={t("全部状态", "All statuses")}
          options={[
            ["active", t("运行中", "Active")],
            ["waiting_human", t("等待反馈", "Needs feedback")],
            ["completed", t("已完成", "Completed")],
            ["failed", t("失败", "Failed")],
            ["cancelled", t("已取消", "Cancelled")],
            ["candidate", t("候选", "Candidate")],
          ]}
        />
        <FilterSelect
          name="sort"
          label={t("最新创建", "Newest")}
          options={[
            ["updated", t("最近更新", "Recently updated")],
            ["score", t("候选评分", "Candidate score")],
          ]}
        />
      </Filters>
      {query.isPending ? (
        <LoadingState />
      ) : query.isError ? (
        <ErrorState retry={() => void query.refetch()} />
      ) : !query.data.data.items.length ? (
        <EmptyState
          title={t("还没有任务", "No tasks yet")}
          description={t(
            "从一个 GitHub Issue 创建任务，或调整当前筛选条件。",
            "Create a task from a GitHub issue, or adjust your filters.",
          )}
        />
      ) : (
        <>
          <div className="wf-table-wrap">
            <table className="wf-table">
              <thead>
                <tr>
                  <th>{t("任务", "Task")}</th>
                  <th>{t("状态", "Status")}</th>
                  <th>{t("进度", "Progress")}</th>
                  <th>{t("最近更新", "Updated")}</th>
                </tr>
              </thead>
              <tbody>
                {query.data.data.items.map((task) => (
                  <tr key={task.id}>
                    <td>
                      <Link
                        className="wf-record-title"
                        to={`/agent/${task.id}`}
                      >
                        {task.title}
                      </Link>
                      <div className="wf-record-meta">
                        {task.repo_full_name} · #
                        {task.source_issue_number ?? task.id}
                      </div>
                    </td>
                    <td>
                      <StatusBadge value={task.status} />
                    </td>
                    <td>
                      {task.iteration_count} / {task.max_iterations}{" "}
                      <span className="muted">{t("轮", "rounds")}</span>
                    </td>
                    <td>
                      <Timestamp value={task.updated_at} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <Pagination data={query.data.data} busy={query.isFetching} />
        </>
      )}
      <CreateTask open={creating} onOpenChange={setCreating} />
    </div>
  );
}

function CreateTask({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { t } = useLocale();
  const navigate = useNavigate();
  const client = useQueryClient();
  const form = useForm<TaskForm>({
    resolver: zodResolver(taskSchema),
    defaultValues: {
      issue_ref: "",
      title: "",
      summary: "",
      base_branch: "",
      branch_name: "",
      priority: "",
      status: "queued",
    },
  });
  const [preview, setPreview] = useState<{
    source: string;
    content: string;
  } | null>(null);
  const create = useMutation({
    mutationFn: (values: TaskForm) =>
      command("/api/v1/agent/tasks", {
        method: "POST",
        body: JSON.stringify(values),
      }),
    onSuccess: (result) => {
      void client.invalidateQueries({ queryKey: ["agent-tasks"] });
      onOpenChange(false);
      form.reset();
      setPreview(null);
      if (result.task_id) navigate(`/agent/${result.task_id}`);
    },
    onError: () =>
      toast.error(
        t(
          "创建失败，请检查 Issue 权限与配置。",
          "Could not create this task. Check issue access and configuration.",
        ),
      ),
  });
  const loadPreview = useMutation({
    mutationFn: async () => {
      const values = form.getValues();
      const result = await api<
        MutationResult & {
          draft?: Partial<TaskForm>;
          submission_context?: unknown;
        }
      >("/api/v1/agent/tasks/preview-from-issue", {
        method: "POST",
        body: JSON.stringify({
          issue_ref: values.issue_ref,
          draft: Object.fromEntries(
            Object.entries({
              title: values.title,
              summary: values.summary,
              base_branch: values.base_branch,
              branch_name: values.branch_name,
              priority: values.priority,
              status: values.status,
            }).filter(([, value]) => value.trim()),
          ),
        }),
      });
      if (!result.success) throw new Error("Preview unavailable");
      setPreview({
        source: values.issue_ref,
        content: JSON.stringify(result.submission_context, null, 2),
      });
      if (result.draft)
        for (const key of [
          "title",
          "summary",
          "base_branch",
          "branch_name",
          "priority",
        ] as const)
          if (!values[key] && typeof result.draft[key] === "string")
            form.setValue(key, result.draft[key]!);
    },
    onError: () =>
      toast.error(
        t(
          "无法读取 Issue 预览，请检查地址和访问权限。",
          "Could not preview this issue. Check the reference and access.",
        ),
      ),
  });
  return (
    <Dialog
      open={open}
      onOpenChange={(value) => {
        if (!create.isPending) onOpenChange(value);
      }}
      title={t("新建 Agent 任务", "New Agent task")}
      description={t(
        "选择 Issue 并说明任务目标，任务会按当前配置运行。",
        "Choose an issue and describe the goal. The task will use your current configuration.",
      )}
    >
      <form
        className="agent-form"
        onSubmit={form.handleSubmit((values) => create.mutate(values))}
      >
        <label>
          GitHub Issue
          <input
            {...form.register("issue_ref")}
            placeholder="owner/repository#123"
            aria-invalid={!!form.formState.errors.issue_ref}
          />
        </label>
        {form.formState.errors.issue_ref && (
          <p className="danger-text" role="alert">
            {t("请输入 Issue 地址或引用。", "Enter an issue URL or reference.")}
          </p>
        )}
        <label>
          {t("任务标题（可选）", "Title (optional)")}
          <input {...form.register("title")} />
        </label>
        <label>
          {t("任务目标（可选）", "Goal (optional)")}
          <textarea rows={4} {...form.register("summary")} />
        </label>
        <Disclosure title={t("分支与执行选项", "Branch and execution options")}>
          <div className="agent-form">
            <label>
              {t("基础分支", "Base branch")}
              <input {...form.register("base_branch")} />
            </label>
            <label>
              {t("工作分支", "Working branch")}
              <input {...form.register("branch_name")} />
            </label>
            <label>
              {t("优先级", "Priority")}
              <select {...form.register("priority")}>
                <option value="">{t("默认", "Default")}</option>
                {["critical", "high", "medium", "low"].map((item) => (
                  <option key={item}>{item}</option>
                ))}
              </select>
            </label>
            <label>
              {t("执行方式", "Execution")}
              <select {...form.register("status")}>
                <option value="queued">
                  {t("创建并排队运行", "Create and queue")}
                </option>
                <option value="candidate">
                  {t("保存为候选任务", "Save as a candidate")}
                </option>
              </select>
            </label>
          </div>
        </Disclosure>
        {preview && preview.source === form.watch("issue_ref") && (
          <Disclosure title={t("提交内容预览", "Submission preview")}>
            <pre className="agent-pre">{preview.content}</pre>
          </Disclosure>
        )}
        <div className="confirm-actions">
          <button
            type="button"
            className="button"
            disabled={loadPreview.isPending || !form.watch("issue_ref").trim()}
            onClick={() => loadPreview.mutate()}
          >
            {loadPreview.isPending
              ? t("读取中…", "Loading…")
              : t("读取 Issue 与预览", "Load issue & preview")}
          </button>
          <button className="button primary" disabled={create.isPending}>
            {create.isPending
              ? t("创建中…", "Creating…")
              : t("创建任务", "Create task")}
          </button>
        </div>
      </form>
    </Dialog>
  );
}

function AgentDetail({ id }: { id: string }) {
  const { t } = useLocale();
  const client = useQueryClient();
  const navigate = useNavigate();
  const confirm = useConfirm();
  const query = useQuery({
    queryKey: ["agent-task", id],
    queryFn: ({ signal }) =>
      api<components["schemas"]["AgentTaskResponse"]>(
        `/api/v1/agent/tasks/${id}`,
        { signal },
      ),
    refetchInterval: (q) =>
      q.state.data && active.includes(q.state.data.data.status) ? 5000 : false,
  });
  const mutation = useMutation({
    mutationFn: (action: string) =>
      command(
        `/api/v1/agent/tasks/${id}${action === "delete" ? "" : `/${action}`}`,
        { method: action === "delete" ? "DELETE" : "POST" },
      ),
    onSuccess: (_, action) => {
      void client.invalidateQueries({ queryKey: ["agent-task", id] });
      void client.invalidateQueries({ queryKey: ["agent-tasks"] });
      if (action === "delete") navigate("/agent");
      else toast.success(t("操作已提交", "Action submitted"));
    },
    onError: () =>
      toast.error(
        t(
          "操作未完成，请刷新状态后重试。",
          "The action could not complete. Refresh the task and try again.",
        ),
      ),
  });
  async function perform(action: string, label: string) {
    if (
      await confirm({
        title: label,
        description:
          action === "delete"
            ? t(
                "将删除此任务及其记录。此操作无法撤销。",
                "This permanently deletes the task and its records.",
              )
            : t(
                "操作会改变当前任务的运行状态。",
                "This will change the task execution state.",
              ),
        confirmLabel: label,
        destructive: action === "cancel" || action === "delete",
      })
    )
      mutation.mutate(action);
  }
  if (query.isPending) return <LoadingState />;
  if (query.isError) return <ErrorState retry={() => void query.refetch()} />;
  const task: Task = query.data.data;
  const pr = safeExternalUrl(task.pr_url);
  return (
    <div>
      <Link className="agent-back" to="/agent">
        <ArrowLeft size={16} />
        {t("所有任务", "All tasks")}
      </Link>
      <PageHeader
        title={task.title}
        description={`${task.repo_full_name} · #${task.id}`}
        actions={
          <RefreshButton
            busy={query.isFetching}
            onClick={() => void query.refetch()}
          />
        }
      />
      <div className="agent-task-strip">
        <StatusBadge value={task.status} />
        <span>
          <GitBranch size={16} />
          {task.branch_name || task.base_branch || "—"}
        </span>
        <span>
          {t("迭代", "Iteration")} {task.iteration_count} /{" "}
          {task.max_iterations}
        </span>
        {pr && (
          <a className="button" href={pr} target="_blank" rel="noreferrer">
            Pull request <ArrowUpRight size={15} />
          </a>
        )}
      </div>
      <div className="agent-actions">
        {["failed", "cancelled", "abandoned", "queued"].includes(
          task.status,
        ) && (
          <button
            className="button"
            disabled={mutation.isPending}
            onClick={() => void perform("retry", t("重新运行", "Retry task"))}
          >
            <RotateCcw size={15} />
            {t("重新运行", "Retry")}
          </button>
        )}
        {["failed", "cancelled"].includes(task.status) && task.branch_name && (
          <button
            className="button"
            disabled={mutation.isPending}
            onClick={() =>
              void perform(
                "resume",
                t("从检查点继续", "Resume from checkpoint"),
              )
            }
          >
            {t("继续运行", "Resume")}
          </button>
        )}
        {active.includes(task.status) && task.status !== "pushing" && (
          <button
            className="button danger-text"
            disabled={mutation.isPending}
            onClick={() => void perform("cancel", t("取消任务", "Cancel task"))}
          >
            <Square size={14} />
            {t("取消任务", "Cancel")}
          </button>
        )}
        {(!active.includes(task.status) ||
          ["pr_opened", "waiting_human"].includes(task.status)) && (
          <button
            className="button danger-text"
            disabled={mutation.isPending}
            onClick={() => void perform("delete", t("删除任务", "Delete task"))}
          >
            <Trash2 size={15} />
            {t("删除", "Delete")}
          </button>
        )}
      </div>
      {task.error_message && (
        <div className="agent-error" role="alert">
          {task.error_message}
        </div>
      )}
      {task.summary && (
        <Disclosure title={t("任务目标", "Task goal")}>
          <Markdown>{task.summary}</Markdown>
        </Disclosure>
      )}
      <AgentConversation id={id} />
      <Section title={t("迭代记录", "Iterations")}>
        {task.iterations.length ? (
          task.iterations.map((iteration) => (
            <Disclosure
              key={iteration.id}
              title={`${t("第", "Round")} ${iteration.iteration_number} · ${iteration.decision || t("处理中", "In progress")}`}
            >
              <div className="agent-iteration">
                {iteration.fullstack_plan && (
                  <>
                    <h3>{t("计划", "Plan")}</h3>
                    <Markdown>{iteration.fullstack_plan}</Markdown>
                  </>
                )}
                {iteration.fullstack_result && (
                  <>
                    <h3>{t("实现", "Implementation")}</h3>
                    <Markdown>{iteration.fullstack_result}</Markdown>
                  </>
                )}
                {iteration.professional_review && (
                  <>
                    <h3>{t("审查", "Review")}</h3>
                    <Markdown>{iteration.professional_review}</Markdown>
                  </>
                )}
                {iteration.test_output && (
                  <Disclosure title={t("验证输出", "Validation output")}>
                    <pre className="agent-pre">
                      {iteration.test_command}
                      {"\n"}
                      {iteration.test_output}
                    </pre>
                  </Disclosure>
                )}
                {iteration.patch_files.map((file) => (
                  <div className="agent-file" key={file.id}>
                    <code>{file.file_path}</code>
                    <span>
                      +{file.additions ?? 0} / −{file.deletions ?? 0}
                    </span>
                    {file.diff_summary && <p>{file.diff_summary}</p>}
                  </div>
                ))}
              </div>
            </Disclosure>
          ))
        ) : (
          <p className="muted">
            {t(
              "任务开始后将在这里显示迭代记录。",
              "Iterations appear here once execution begins.",
            )}
          </p>
        )}
      </Section>
      {!!task.feedback.length && (
        <Section title={t("审查与反馈", "Reviews and feedback")}>
          {task.feedback.map((item) => (
            <article className="agent-message" key={item.id}>
              <header>
                <strong>{item.author || item.source}</strong>
                <Timestamp value={item.created_at} />
              </header>
              <Markdown>{item.content}</Markdown>
            </article>
          ))}
        </Section>
      )}
    </div>
  );
}

interface StreamData {
  messages: {
    id: number;
    role: string;
    content: string | null;
    created_at: string | null;
    session_id: number;
  }[];
  tool_calls: {
    id: number;
    name: string;
    status: string;
    arguments_json: string | null;
    error_message: string | null;
  }[];
  sessions: { id: number; role_name: string; status: string }[];
  prompts: {
    id: number;
    content: string;
    status: string;
    submitted_by: string;
    created_at: string;
  }[];
  has_more: boolean;
  task_status: string;
  can_send_prompt: boolean;
  prompt_disabled_reason: string | null;
}

function AgentConversation({ id }: { id: string }) {
  const { t } = useLocale();
  const client = useQueryClient();
  const [prompt, setPrompt] = useState("");
  const query = useInfiniteQuery({
    queryKey: ["agent-conversation", id],
    initialPageParam: 0,
    queryFn: ({ pageParam, signal }) =>
      api<StreamData>(
        `/api/v1/agent/tasks/${id}/stream-data?after_id=${pageParam}&limit=100`,
        { signal },
      ),
    getNextPageParam: (last) =>
      last.has_more && last.messages.length
        ? last.messages.at(-1)!.id
        : undefined,
    refetchInterval: (q) =>
      q.state.data?.pages.some((page) => active.includes(page.task_status))
        ? 5000
        : false,
  });
  const latest = query.data?.pages.at(-1);
  const submit = useMutation({
    mutationFn: (content: string) =>
      command(`/api/v1/agent/tasks/${id}/prompts`, {
        method: "POST",
        body: JSON.stringify({ content }),
      }),
    onSuccess: () => {
      setPrompt("");
      void client.invalidateQueries({ queryKey: ["agent-conversation", id] });
      void client.invalidateQueries({ queryKey: ["agent-task", id] });
    },
    onError: () =>
      toast.error(
        t(
          "反馈未提交，请检查任务状态后重试。",
          "Feedback was not submitted. Check the task status and try again.",
        ),
      ),
  });
  const messages = [
    ...new Map(
      query.data?.pages
        .flatMap((page) => page.messages)
        .map((message) => [message.id, message]) ?? [],
    ).values(),
  ];
  return (
    <Section
      title={t("任务对话", "Task conversation")}
      aside={<Link to="/activity">{t("运行活动", "Activity")} ↗</Link>}
    >
      {query.isPending ? (
        <LoadingState />
      ) : query.isError ? (
        <ErrorState retry={() => void query.refetch()} />
      ) : (
        <div className="agent-conversation">
          {messages.length ? (
            messages.map((message) => (
              <article className="agent-message" key={message.id}>
                <header>
                  <strong>
                    <Bot size={15} />
                    {message.role}
                  </strong>
                  <Timestamp value={message.created_at} />
                </header>
                {message.content ? (
                  <Markdown>{message.content}</Markdown>
                ) : (
                  <span className="muted">{t("工具调用", "Tool call")}</span>
                )}
              </article>
            ))
          ) : (
            <p className="muted">
              {t("等待 Agent 开始工作。", "Waiting for the Agent to start.")}
            </p>
          )}
        </div>
      )}
      {query.hasNextPage && (
        <button
          className="button"
          disabled={query.isFetchingNextPage}
          onClick={() => void query.fetchNextPage()}
        >
          {t("加载更多对话", "Load more conversation")}
        </button>
      )}
      {!!latest?.tool_calls.length && (
        <Disclosure
          title={t("工具调用 · {0}", "Tool calls · {0}", [
            latest.tool_calls.length,
          ])}
        >
          {latest.tool_calls.map((tool) => (
            <div className="agent-tool" key={tool.id}>
              <strong>{tool.name}</strong>
              <StatusBadge value={tool.status} />
              <details>
                <summary>{t("参数与结果", "Arguments and result")}</summary>
                <pre className="agent-pre">
                  {tool.arguments_json}
                  {tool.error_message && `\n${tool.error_message}`}
                </pre>
              </details>
            </div>
          ))}
        </Disclosure>
      )}
      {!!latest?.prompts.length && (
        <Disclosure title={t("你的反馈", "Your feedback")}>
          {latest.prompts.map((item) => (
            <div className="agent-message" key={item.id}>
              <header>
                <strong>{item.submitted_by}</strong>
                <StatusBadge value={item.status} />
              </header>
              <p>{item.content}</p>
            </div>
          ))}
        </Disclosure>
      )}
      <form
        className="agent-prompt"
        onSubmit={(event) => {
          event.preventDefault();
          if (prompt.trim() && latest?.can_send_prompt)
            submit.mutate(prompt.trim());
        }}
      >
        <label htmlFor="agent-prompt">
          {t("补充说明或反馈", "Add guidance or feedback")}
        </label>
        <textarea
          id="agent-prompt"
          rows={3}
          value={prompt}
          onChange={(event) => setPrompt(event.target.value)}
          disabled={!latest?.can_send_prompt || submit.isPending}
          placeholder={t(
            "说明你希望 Agent 调整的地方…",
            "Describe what you would like the Agent to adjust…",
          )}
        />
        <div>
          {!latest?.can_send_prompt && (
            <span className="muted">
              {t(
                "当前任务状态不接受新反馈。",
                "This task is not accepting feedback in its current state.",
              )}
            </span>
          )}
          <button
            className="button primary"
            disabled={
              !prompt.trim() || !latest?.can_send_prompt || submit.isPending
            }
          >
            <Send size={15} />
            {t("发送", "Send")}
          </button>
        </div>
      </form>
    </Section>
  );
}
