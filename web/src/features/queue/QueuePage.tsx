import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { RotateCcw, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { api } from "../../api/client";
import { useLocale } from "../../app/locale";
import { PermissionGuard } from "../../components/common/PermissionGuard";
import { useConfirm } from "../../components/ui/ConfirmProvider";
import {
  Disclosure,
  EmptyState,
  ErrorState,
  FilterSelect,
  Filters,
  LoadingState,
  PageHeader,
  Pagination,
  RefreshButton,
  StatusBadge,
  Timestamp,
} from "../../components/workflow/Workflow";
import type { Envelope, PageData } from "../../components/workflow/types";
import "../scans/scans.css";

interface QueueItem {
  id: number;
  pr_id: number;
  repo_name: string;
  action: string;
  priority: number;
  status: string;
  retry_count: number;
  max_retries: number;
  error_message: string | null;
  created_at: string;
  updated_at: string;
}
export default function QueuePage() {
  return (
    <PermissionGuard permission="admin">
      <QueueManager />
    </PermissionGuard>
  );
}

function QueueManager() {
  const { t } = useLocale();
  const [params] = useSearchParams();
  const { id } = useParams();
  const confirm = useConfirm();
  const client = useQueryClient();
  const list = useQuery({
    queryKey: ["queue", params.toString()],
    queryFn: ({ signal }) =>
      api<Envelope<PageData<QueueItem>>>(`/api/v1/queue/items?${params}`, {
        signal,
      }),
    enabled: !id,
    refetchInterval: 10_000,
  });
  const detail = useQuery({
    queryKey: ["queue-item", id],
    queryFn: ({ signal }) =>
      api<Envelope<QueueItem>>(`/api/v1/queue/items/${id}`, { signal }),
    enabled: !!id,
    refetchInterval: 10_000,
  });
  const stats = useQuery({
    queryKey: ["queue-stats"],
    queryFn: ({ signal }) =>
      api<Envelope<Record<string, number>>>("/api/v1/queue/stats", { signal }),
    refetchInterval: 10_000,
  });
  const action = useMutation({
    mutationFn: async ({ path, method }: { path: string; method: string }) => {
      const result = await api<Envelope<unknown>>(path, { method });
      if (!result.success) throw new Error("Queue operation declined");
      return result;
    },
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["queue"] });
      void client.invalidateQueries({ queryKey: ["queue-item"] });
      void client.invalidateQueries({ queryKey: ["queue-stats"] });
      toast.success(t("队列已更新", "Queue updated"));
    },
    onError: () =>
      toast.error(
        t(
          "操作未完成，请刷新队列状态后重试。",
          "The action did not complete. Refresh the queue and try again.",
        ),
      ),
  });
  async function purge(status: string) {
    if (
      await confirm({
        title:
          status === "completed"
            ? t("清理已完成记录？", "Clear completed records?")
            : t("清理失败记录？", "Clear failed records?"),
        description: t(
          "只删除所选状态的队列记录，不会删除已生成的审查结果。",
          "This deletes queue records in the selected state. Generated review results are retained.",
        ),
        destructive: true,
      })
    )
      action.mutate({
        path: `/api/v1/queue/purge?status=${status}`,
        method: "POST",
      });
  }
  const loading = id ? detail.isPending : list.isPending;
  const failed = id ? detail.isError : list.isError;
  const items = id
    ? detail.data
      ? [detail.data.data]
      : []
    : (list.data?.data.items ?? []);
  return (
    <div>
      <PageHeader
        title={t("审查队列", "Review queue")}
        description={t(
          "查看待处理任务，重试失败记录并整理队列历史。",
          "Inspect pending work, retry failures and manage queue history.",
        )}
        actions={
          <RefreshButton
            busy={list.isFetching || detail.isFetching}
            onClick={() => {
              if (id) void detail.refetch();
              else void list.refetch();
              void stats.refetch();
            }}
          />
        }
      />
      <div className="scan-links">
        <Link to="/reviews">{t("审查结果", "Review results")}</Link>
        {id && (
          <Link to="/queue">{t("全部队列记录", "All queue records")}</Link>
        )}
      </div>
      {stats.data && (
        <div className="scan-stats">
          {[
            ["total", t("总计", "Total")],
            ["pending", t("等待中", "Pending")],
            ["processing", t("处理中", "Processing")],
            ["failed", t("失败", "Failed")],
          ].map(([key, label]) => (
            <span key={key}>
              {label} <strong>{stats.data.data[key] ?? 0}</strong>
            </span>
          ))}
        </div>
      )}
      {!id && (
        <Filters placeholder={t("搜索队列仓库", "Search queued repositories")}>
          <FilterSelect
            name="status"
            label={t("全部状态", "All statuses")}
            options={[
              ["pending", t("等待中", "Pending")],
              ["processing", t("处理中", "Processing")],
              ["completed", t("已完成", "Completed")],
              ["failed", t("失败", "Failed")],
            ]}
          />
        </Filters>
      )}
      {loading ? (
        <LoadingState />
      ) : failed ? (
        <ErrorState
          retry={() => {
            if (id) void detail.refetch();
            else void list.refetch();
          }}
        />
      ) : !items.length ? (
        <EmptyState
          title={t("没有符合条件的队列记录", "No matching queue records")}
        />
      ) : (
        <>
          <div className="wf-table-wrap">
            <table className="wf-table">
              <thead>
                <tr>
                  <th>{t("任务", "Task")}</th>
                  <th>{t("状态", "Status")}</th>
                  <th>{t("重试", "Retries")}</th>
                  <th>{t("更新时间", "Updated")}</th>
                  <th>{t("操作", "Actions")}</th>
                </tr>
              </thead>
              <tbody>
                {items.map((item) => (
                  <tr key={item.id}>
                    <td>
                      <Link
                        to={`/queue/${item.id}`}
                        className="wf-record-title"
                      >
                        {item.repo_name} #{item.pr_id}
                      </Link>
                      <div className="wf-record-meta">
                        {item.action} ·{" "}
                        {t("优先级 {0}", "Priority {0}", [item.priority])}
                      </div>
                      {item.error_message && (
                        <Disclosure title={t("错误详情", "Error details")}>
                          <p className="scan-error">{item.error_message}</p>
                        </Disclosure>
                      )}
                    </td>
                    <td>
                      <StatusBadge value={item.status} />
                    </td>
                    <td>
                      {item.retry_count} / {item.max_retries}
                    </td>
                    <td>
                      <Timestamp value={item.updated_at} />
                    </td>
                    <td>
                      <div className="wf-actions">
                        {item.status === "failed" && (
                          <button
                            className="button"
                            disabled={action.isPending}
                            onClick={async () => {
                              if (
                                await confirm({
                                  title: t(
                                    "重试队列任务？",
                                    "Retry queued task?",
                                  ),
                                  description: t(
                                    "该任务将重新进入待处理队列。",
                                    "The task will be returned to the pending queue.",
                                  ),
                                })
                              )
                                action.mutate({
                                  path: `/api/v1/queue/items/${item.id}/retry`,
                                  method: "POST",
                                });
                            }}
                          >
                            <RotateCcw size={14} />
                            {t("重试", "Retry")}
                          </button>
                        )}
                        <button
                          className="icon-button danger-text"
                          disabled={action.isPending}
                          aria-label={t(
                            "删除队列记录 {0}",
                            "Delete queue record {0}",
                            [item.id],
                          )}
                          onClick={async () => {
                            if (
                              await confirm({
                                title: t(
                                  "删除队列记录？",
                                  "Delete queue record?",
                                ),
                                description: `${item.repo_name} #${item.pr_id}`,
                                destructive: true,
                              })
                            )
                              action.mutate({
                                path: `/api/v1/queue/items/${item.id}`,
                                method: "DELETE",
                              });
                          }}
                        >
                          <Trash2 size={16} />
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {!id && list.data && (
            <Pagination data={list.data.data} busy={list.isFetching} />
          )}
        </>
      )}
      {!id && (
        <Disclosure title={t("清理队列历史", "Clean queue history")}>
          <div className="wf-actions">
            <button
              className="button"
              disabled={action.isPending}
              onClick={() => void purge("completed")}
            >
              {t("清理已完成记录", "Clear completed records")}
            </button>
            <button
              className="button danger-text"
              disabled={action.isPending}
              onClick={() => void purge("failed")}
            >
              {t("清理失败记录", "Clear failed records")}
            </button>
          </div>
        </Disclosure>
      )}
    </div>
  );
}
