import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api/client";
import { useLocale } from "../../app/locale";
import { PermissionGuard } from "../../components/common/PermissionGuard";
import { Dialog } from "../../components/ui/Dialog";
import {
  Disclosure,
  EmptyState,
  Markdown,
  PageHeader,
  Section,
  StatusBadge,
} from "../../components/workflow/Workflow";
import { MutationError } from "../billing/BillingCommon";
import "./agent.css";

interface Candidate {
  source_type: string;
  source_id: number;
  source_issue_number: number | null;
  repo_full_name: string;
  title: string;
  summary: string;
  priority: string;
  candidate_score: number;
  filter_reason: string;
}
interface Result {
  success: boolean;
  message?: string;
  task_id?: number;
  candidates?: Candidate[];
}
async function command(path: string, body: unknown) {
  const result = await api<Result>(path, {
    method: "POST",
    body: JSON.stringify(body),
  });
  if (!result.success) throw new Error(result.message || "Action declined");
  return result;
}
export default function CandidatesPage() {
  return (
    <PermissionGuard permission="super_admin">
      <Candidates />
    </PermissionGuard>
  );
}
function Candidates() {
  const { t } = useLocale();
  const navigate = useNavigate();
  const client = useQueryClient();
  const [requirement, setRequirement] = useState("");
  const [usedRequirement, setUsedRequirement] = useState("");
  const [selected, setSelected] = useState<Candidate | null>(null);
  const preview = useMutation({
    mutationFn: () =>
      command("/api/v1/agent/candidates", {
        ai_filter_requirement: requirement,
      }),
    onSuccess: () => setUsedRequirement(requirement),
  });
  const create = useMutation({
    mutationFn: (body: unknown) =>
      command("/api/v1/agent/tasks/from-candidate", body),
    onSuccess: (result) => {
      setSelected(null);
      void client.invalidateQueries({ queryKey: ["agent-tasks"] });
      if (result.task_id) navigate(`/agent/${result.task_id}`);
    },
  });
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selected) return;
    const values = Object.fromEntries(new FormData(event.currentTarget));
    create.mutate({
      ...values,
      source_type: selected.source_type,
      source_id: selected.source_id,
      ai_filter_requirement: usedRequirement,
    });
  }
  return (
    <div>
      <Link to="/agent">← Agent</Link>
      <PageHeader
        title={t("候选任务", "Candidate tasks")}
        description={t(
          "从 Issue 分析和扫描发现中选择任务，确认目标后交给 Agent。",
          "Choose a task from issue analyses and scan findings, then confirm the goal for the Agent.",
        )}
      />
      <form
        className="agent-form"
        onSubmit={(event) => {
          event.preventDefault();
          preview.mutate();
        }}
      >
        <label>
          {t("AI 筛选要求（可选）", "AI selection criteria (optional)")}
          <textarea
            rows={3}
            maxLength={1200}
            value={requirement}
            onChange={(event) => setRequirement(event.target.value)}
          />
        </label>
        <button className="button primary" disabled={preview.isPending}>
          {preview.isPending
            ? t("正在筛选…", "Selecting…")
            : t("获取候选任务", "Find candidates")}
        </button>
      </form>
      <MutationError error={preview.error} />
      {preview.data &&
        (!preview.data.candidates?.length ? (
          <EmptyState
            title={t("暂无符合条件的候选任务", "No matching candidates")}
          />
        ) : (
          preview.data.candidates.map((candidate) => (
            <Section
              key={`${candidate.source_type}/${candidate.source_id}`}
              title={candidate.title}
              aside={
                <button
                  className="button"
                  onClick={() => {
                    create.reset();
                    setSelected(candidate);
                  }}
                >
                  {t("创建任务", "Create task")}
                </button>
              }
            >
              <p>
                {candidate.repo_full_name} · {candidate.source_type} ·{" "}
                {candidate.candidate_score}
              </p>
              <StatusBadge value={candidate.priority} />
              {candidate.filter_reason && <p>{candidate.filter_reason}</p>}
              <Disclosure title={t("来源内容", "Source details")}>
                <Markdown>{candidate.summary}</Markdown>
              </Disclosure>
            </Section>
          ))
        ))}
      <Dialog
        open={!!selected}
        onOpenChange={(open) => {
          if (!open && !create.isPending) setSelected(null);
        }}
        title={t("创建 Agent 任务", "Create Agent task")}
      >
        {selected && (
          <form className="agent-form" onSubmit={submit}>
            <p>{selected.repo_full_name}</p>
            <label>
              {t("任务标题", "Task title")}
              <input name="title" defaultValue={selected.title} required />
            </label>
            <label>
              {t("任务目标", "Task goal")}
              <textarea
                name="summary"
                rows={5}
                placeholder={t(
                  "说明要完成的目标；留空使用默认目标。",
                  "Describe the goal; leave blank to use the default.",
                )}
              />
            </label>
            <label>
              {t("基础分支", "Base branch")}
              <input name="base_branch" />
            </label>
            <label>
              {t("工作分支", "Working branch")}
              <input name="branch_name" />
            </label>
            <label>
              {t("优先级", "Priority")}
              <select name="priority" defaultValue={selected.priority}>
                {["critical", "high", "medium", "low"].map((value) => (
                  <option key={value}>{value}</option>
                ))}
              </select>
            </label>
            <label>
              {t("执行方式", "Execution")}
              <select name="status">
                <option value="queued">
                  {t("创建并排队运行", "Create and queue")}
                </option>
                <option value="candidate">
                  {t("保存为候选任务", "Save as a candidate")}
                </option>
              </select>
            </label>
            <MutationError error={create.error} />
            <button className="button primary" disabled={create.isPending}>
              {t("确认创建", "Confirm creation")}
            </button>
          </form>
        )}
      </Dialog>
    </div>
  );
}
