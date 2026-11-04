import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { api } from "../../api/client";
import { useLocale } from "../../app/locale";
import { useConfirm } from "../../components/ui/ConfirmProvider";
import {
  EmptyState,
  ErrorState,
  Filters,
  FilterSelect,
  LoadingState,
  Section,
  StatusBadge,
  Timestamp,
} from "../../components/workflow/Workflow";
import {
  Field,
  Money,
  MutationError,
  OffsetPagination,
  useBillingPage,
} from "../billing/BillingCommon";
import type { OffsetPage, RefundRequest } from "../billing/billingTypes";

export default function RefundsAdmin() {
  const { t } = useLocale();
  const { params, offset, limit } = useBillingPage();
  const qs = new URLSearchParams({
    offset: String(offset),
    limit: String(limit),
  });
  if (params.get("status")) qs.set("status", params.get("status")!);
  const query = useQuery({
    queryKey: ["refund-requests", qs.toString()],
    queryFn: ({ signal }) =>
      api<OffsetPage<RefundRequest>>(
        `/api/v1/billing/admin/refund-requests?${qs}`,
        { signal },
      ),
  });
  return (
    <>
      <Filters search={false} placeholder="">
        <FilterSelect
          name="status"
          label={t("全部状态", "All statuses")}
          options={[
            ["pending", t("待审核", "Pending")],
            ["approved", t("已批准", "Approved")],
            ["rejected", t("已拒绝", "Rejected")],
            ["failed", t("失败", "Failed")],
          ]}
        />
      </Filters>
      {query.isPending ? (
        <LoadingState />
      ) : query.isError ? (
        <ErrorState retry={() => void query.refetch()} />
      ) : (
        query.data && (
          <>
            {query.data.items.length ? (
              query.data.items.map((row) => (
                <RefundCard
                  key={row.id}
                  row={row}
                  onChange={() => void query.refetch()}
                />
              ))
            ) : (
              <EmptyState title={t("没有退款申请", "No refund requests")} />
            )}
            <OffsetPagination data={query.data} />
          </>
        )
      )}
    </>
  );
}
function RefundCard({
  row,
  onChange,
}: {
  row: RefundRequest;
  onChange: () => void;
}) {
  const { t } = useLocale();
  const confirm = useConfirm();
  const [note, setNote] = useState("");
  const mutation = useMutation({
    mutationFn: (decision: string) =>
      api<RefundRequest>(
        `/api/v1/billing/admin/refund-requests/${row.id}/${decision}`,
        { method: "POST", body: JSON.stringify({ review_note: note }) },
      ),
    retry: false,
    onSuccess: onChange,
  });
  async function decide(decision: string) {
    if (
      await confirm({
        title:
          decision === "approve"
            ? t("批准并执行退款？", "Approve and issue refund?")
            : t("拒绝退款申请？", "Reject refund request?"),
        description:
          decision === "approve"
            ? t(
                "批准会触发实际退款。结果未知时将保留待核对状态，请勿重复发起另一笔退款。",
                "Approval issues a real refund. An unknown outcome remains pending reconciliation; do not initiate a separate refund.",
              )
            : t(
                "申请人将看到此审核结果。",
                "The requester will see this decision.",
              ),
        destructive: true,
      })
    )
      mutation.mutate(decision);
  }
  return (
    <Section
      title={row.order_no || `#${row.order_id}`}
      aside={<StatusBadge value={row.status} />}
    >
      <div className="billing-form">
        <div className="wf-detail-meta">
          <Money
            amount={row.formatted_amount}
            currency={row.currency}
            minor={row.amount_cents}
          />
          <span>
            {t("用户", "User")} #{row.user_id}
          </span>
          <Timestamp value={row.requested_at} />
        </div>
        <p>{row.reason || t("未填写原因", "No reason provided")}</p>
        {row.review_note && <p>{row.review_note}</p>}
        <MutationError error={mutation.error} />
        {mutation.data && (
          <p className="wf-notice" role="status">
            {t("当前退款状态", "Current refund status")}:{" "}
            <StatusBadge value={mutation.data.status} />
            {mutation.data.status === "pending" &&
              ` · ${t("结果待核对", "Awaiting reconciliation")}`}
          </p>
        )}
        {["pending", "failed"].includes(row.status) && (
          <>
            <Field label={t("审核说明", "Review note")}>
              <textarea
                value={note}
                onChange={(event) => setNote(event.target.value)}
                maxLength={2000}
              />
            </Field>
            <div className="wf-actions">
              <button
                className="wf-button wf-button-primary"
                disabled={mutation.isPending}
                onClick={() => void decide("approve")}
              >
                {t("批准退款", "Approve refund")}
              </button>
              <button
                className="wf-button"
                disabled={mutation.isPending}
                onClick={() => void decide("reject")}
              >
                {t("拒绝", "Reject")}
              </button>
            </div>
          </>
        )}
      </div>
    </Section>
  );
}
