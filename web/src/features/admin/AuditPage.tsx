import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { api } from "../../api/client";
import { useLocale } from "../../app/locale";
import {
  EmptyState,
  ErrorState,
  Filters,
  LoadingState,
  Pagination,
  Section,
  Timestamp,
} from "../../components/workflow/Workflow";
import type { Envelope, PageData } from "../../components/workflow/types";
import { Field } from "../billing/BillingCommon";

interface ActionLog {
  id: number;
  admin_id: number;
  action: string;
  target_type: string;
  target_id: string;
  detail: string | null;
  created_at: string;
}
export default function AuditPage() {
  const { t } = useLocale();
  const [params] = useSearchParams();
  const query = useQuery({
    queryKey: ["audit", params.toString()],
    queryFn: ({ signal }) =>
      api<Envelope<PageData<ActionLog>>>(`/api/v1/logs/actions?${params}`, {
        signal,
      }),
  });
  return (
    <>
      <Filters search={false} placeholder="">
        <Field label={t("操作类型", "Action type")}>
          <input
            name="action"
            defaultValue={params.get("action") ?? ""}
            placeholder="user_toggle"
          />
        </Field>
        <label className="wf-date-label">
          {t("从", "From")}
          <input
            className="wf-date-input"
            name="start_date"
            type="date"
            defaultValue={params.get("start_date") ?? ""}
          />
        </label>
        <label className="wf-date-label">
          {t("至", "To")}
          <input
            className="wf-date-input"
            name="end_date"
            type="date"
            defaultValue={params.get("end_date") ?? ""}
          />
        </label>
      </Filters>
      <Section title={t("审计日志", "Audit logs")}>
        {query.isPending ? (
          <LoadingState />
        ) : query.isError ? (
          <ErrorState retry={() => void query.refetch()} />
        ) : (
          query.data && (
            <>
              {query.data.data.items.length ? (
                <div className="wf-table-wrap">
                  <table className="wf-table">
                    <thead>
                      <tr>
                        <th>{t("操作", "Action")}</th>
                        <th>{t("操作者", "Actor")}</th>
                        <th>{t("目标", "Target")}</th>
                        <th>{t("时间", "Date")}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {query.data.data.items.map((log) => (
                        <tr key={log.id}>
                          <td>
                            {log.action}
                            {log.detail && (
                              <details>
                                <summary className="wf-muted">
                                  {t("查看详情", "Details")}
                                </summary>
                                <pre
                                  style={{
                                    whiteSpace: "pre-wrap",
                                    overflowWrap: "anywhere",
                                    maxWidth: 500,
                                  }}
                                >
                                  {log.detail}
                                </pre>
                              </details>
                            )}
                          </td>
                          <td>#{log.admin_id}</td>
                          <td>
                            {log.target_type} · {log.target_id}
                          </td>
                          <td>
                            <Timestamp value={log.created_at} />
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <EmptyState title={t("暂无操作记录", "No audit records")} />
              )}
              <Pagination data={query.data.data} />
            </>
          )
        )}
      </Section>
    </>
  );
}
