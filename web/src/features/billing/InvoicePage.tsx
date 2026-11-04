import { Link, useParams, useSearchParams } from "react-router-dom";
import { useMutation, useQuery } from "@tanstack/react-query";
import { api } from "../../api/client";
import { useLocale } from "../../app/locale";
import { useConfirm } from "../../components/ui/ConfirmProvider";
import {
  EmptyState,
  ErrorState,
  LoadingState,
  Section,
  StatusBadge,
  Timestamp,
} from "../../components/workflow/Workflow";
import { MutationError } from "./BillingCommon";
import "../public/public.css";

interface Invoice {
  id: number;
  order_no: string;
  status: string;
  provider: string;
  expires_at: string | null;
  payment: null | {
    pay_address: string;
    pay_amount: string;
    pay_currency: string;
  };
}
export default function InvoicePage() {
  const { t } = useLocale();
  const confirm = useConfirm();
  const { orderNo } = useParams();
  const [params] = useSearchParams();
  const number = orderNo || params.get("order_no");
  const query = useQuery({
    queryKey: ["invoice", number],
    queryFn: ({ signal }) =>
      api<Invoice>(`/api/v1/billing/invoice/${encodeURIComponent(number!)}`, {
        signal,
      }),
    enabled: !!number,
    refetchInterval: (state) =>
      state.state.data?.status === "pending" ? 10000 : false,
  });
  const poll = useQuery({
    queryKey: ["invoice-status", number],
    queryFn: ({ signal }) =>
      api<{ status: string }>(
        `/api/v1/billing/invoice-status?order_no=${encodeURIComponent(number!)}`,
        { method: "POST", signal },
      ),
    enabled: !!query.data?.payment,
    refetchInterval: (state) =>
      ["completed", "expired", "failed"].includes(
        state.state.data?.status || "",
      )
        ? false
        : 10000,
  });
  const cancel = useMutation({
    mutationFn: () =>
      api(`/api/v1/billing/orders/${query.data!.id}/cancel`, {
        method: "POST",
      }),
    onSuccess: () => void query.refetch(),
  });
  if (!number)
    return (
      <EmptyState
        title={t(
          "请从订单列表查看支付状态",
          "Open your order to view payment status",
        )}
        action={
          <Link className="button" to="/billing/orders">
            {t("查看订单", "View orders")}
          </Link>
        }
      />
    );
  if (query.isPending) return <LoadingState />;
  if (query.isError) return <ErrorState retry={() => void query.refetch()} />;
  const invoice = query.data;
  return (
    <Section
      title={t("支付状态", "Payment status")}
      aside={<StatusBadge value={invoice.status} />}
    >
      <p>{invoice.order_no}</p>
      {invoice.payment?.pay_address && (
        <>
          <p>
            {t(
              "请使用指定网络和准确金额转账。",
              "Transfer the exact amount using the specified network.",
            )}
          </p>
          <p className="invoice-amount">
            {invoice.payment.pay_amount}{" "}
            <small>{invoice.payment.pay_currency.toUpperCase()}</small>
          </p>
          <code className="invoice-address">{invoice.payment.pay_address}</code>
          <p>
            {t("有效期至", "Expires at")}{" "}
            <Timestamp value={invoice.expires_at} />
          </p>
          <p role="status">
            {t("链上状态", "Network status")} ·{" "}
            {poll.data?.status || t("等待付款", "Awaiting payment")}
          </p>
          {poll.isError && (
            <p className="muted">
              {t(
                "暂时无法刷新链上状态，请稍后查看订单。",
                "Unable to refresh network status. Check your order again shortly.",
              )}
            </p>
          )}
        </>
      )}
      <MutationError error={cancel.error} />
      <div className="wf-actions">
        <Link className="button primary" to={`/billing/orders/${invoice.id}`}>
          {t("查看订单", "View order")}
        </Link>
        <button
          className="button"
          disabled={query.isFetching}
          onClick={() => void query.refetch()}
        >
          {t("刷新状态", "Refresh status")}
        </button>
        {invoice.status === "pending" && (
          <button
            className="button danger-text"
            disabled={cancel.isPending}
            onClick={async () => {
              if (
                await confirm({
                  title: t("取消订单？", "Cancel order?"),
                  description: invoice.order_no,
                  destructive: true,
                })
              )
                cancel.mutate();
            }}
          >
            {t("取消订单", "Cancel order")}
          </button>
        )}
      </div>
    </Section>
  );
}
