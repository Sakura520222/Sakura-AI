import { useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery } from "@tanstack/react-query";
import { api } from "../../api/client";
import { useLocale } from "../../app/locale";
import { useConfirm } from "../../components/ui/ConfirmProvider";
import {
  Disclosure,
  EmptyState,
  ErrorState,
  LoadingState,
  Section,
} from "../../components/workflow/Workflow";
import { MutationError } from "../billing/BillingCommon";

interface Collection {
  name: string;
  doc_count: number;
  metadata: Record<string, unknown>;
}
const base = "/api/v1/console/storage";
export default function StoragePage() {
  const { t } = useLocale();
  const confirm = useConfirm();
  const { name: selected = "" } = useParams();
  const navigate = useNavigate();
  const setSelected = (name: string) =>
    navigate(
      name ? `/admin/storage/${encodeURIComponent(name)}` : "/admin/storage",
    );
  const collections = useQuery({
    queryKey: ["storage"],
    queryFn: ({ signal }) => api<{ items: Collection[] }>(base, { signal }),
  });
  const mutation = useMutation({
    mutationFn: ({ name, action }: { name: string; action: string }) =>
      api(`${base}/${encodeURIComponent(name)}/${action}`, { method: "POST" }),
    onSuccess: () => {
      setSelected("");
      void collections.refetch();
    },
  });
  return (
    <>
      <Section title={t("向量存储", "Vector storage")}>
        <MutationError error={mutation.error} />
        {collections.isPending ? (
          <LoadingState />
        ) : collections.isError ? (
          <ErrorState retry={() => void collections.refetch()} />
        ) : !collections.data.items.length ? (
          <EmptyState title={t("暂无集合", "No collections")} />
        ) : (
          <div className="wf-table-wrap">
            <table className="wf-table">
              <thead>
                <tr>
                  <th>{t("集合", "Collection")}</th>
                  <th>{t("文档数", "Documents")}</th>
                  <th>{t("操作", "Actions")}</th>
                </tr>
              </thead>
              <tbody>
                {collections.data.items.map((item) => (
                  <tr key={item.name}>
                    <td>
                      <button
                        className="wf-button wf-button-quiet"
                        onClick={() => setSelected(item.name)}
                      >
                        {item.name}
                      </button>
                    </td>
                    <td>{item.doc_count}</td>
                    <td>
                      <div className="wf-actions">
                        {["clear", "delete"].map((action) => (
                          <button
                            key={action}
                            className="button danger-text"
                            disabled={mutation.isPending}
                            onClick={async () => {
                              if (
                                await confirm({
                                  title:
                                    action === "clear"
                                      ? t("清空集合？", "Clear collection?")
                                      : t("删除集合？", "Delete collection?"),
                                  description: t(
                                    "将移除集合 {0} 中的索引内容。",
                                    "This removes indexed content in {0}.",
                                    [item.name],
                                  ),
                                  destructive: true,
                                })
                              )
                                mutation.mutate({ name: item.name, action });
                            }}
                          >
                            {action === "clear"
                              ? t("清空", "Clear")
                              : t("删除", "Delete")}
                          </button>
                        ))}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Section>
      {selected && (
        <Documents
          key={selected}
          name={selected}
          onChange={() => void collections.refetch()}
        />
      )}
    </>
  );
}
function Documents({ name, onChange }: { name: string; onChange: () => void }) {
  const { t } = useLocale();
  const confirm = useConfirm();
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<string[]>([]);
  const query = useQuery({
    queryKey: ["storage-documents", name, offset],
    queryFn: ({ signal }) =>
      api<{
        total: number;
        limit: number;
        documents: { id: string; content: string; metadata: unknown }[];
      }>(`${base}/${encodeURIComponent(name)}?offset=${offset}`, { signal }),
  });
  const mutation = useMutation({
    mutationFn: () =>
      api(`${base}/${encodeURIComponent(name)}/documents/delete`, {
        method: "POST",
        body: JSON.stringify({ ids: selected }),
      }),
    onSuccess: () => {
      setSelected([]);
      void query.refetch();
      onChange();
    },
  });
  return (
    <Section
      title={name}
      aside={
        <button
          className="button danger-text"
          disabled={!selected.length || mutation.isPending}
          onClick={async () => {
            if (
              await confirm({
                title: t(
                  "删除选中的 {0} 个文档？",
                  "Delete {0} selected documents?",
                  [selected.length],
                ),
                description: name,
                destructive: true,
              })
            )
              mutation.mutate();
          }}
        >
          {t("删除选中", "Delete selected")}
        </button>
      }
    >
      <MutationError error={mutation.error} />
      {query.isPending ? (
        <LoadingState />
      ) : query.isError ? (
        <ErrorState retry={() => void query.refetch()} />
      ) : (
        <>
          {query.data.documents.map((document) => (
            <article className="agent-message" key={document.id}>
              <label>
                <input
                  type="checkbox"
                  checked={selected.includes(document.id)}
                  onChange={() =>
                    setSelected((current) =>
                      current.includes(document.id)
                        ? current.filter((id) => id !== document.id)
                        : [...current, document.id],
                    )
                  }
                />{" "}
                <code>{document.id}</code>
              </label>
              <Disclosure title={t("查看内容", "View content")}>
                <pre className="agent-pre">{document.content}</pre>
                <pre className="agent-pre">
                  {JSON.stringify(document.metadata, null, 2)}
                </pre>
              </Disclosure>
            </article>
          ))}
          <div className="wf-pagination">
            <span>
              {query.data.total} {t("个文档", "documents")}
            </span>
            <div>
              <button
                className="button"
                disabled={offset === 0}
                onClick={() => {
                  setOffset(Math.max(0, offset - query.data.limit));
                  setSelected([]);
                }}
              >
                {t("上一页", "Previous")}
              </button>
              <button
                className="button"
                disabled={offset + query.data.limit >= query.data.total}
                onClick={() => {
                  setOffset(offset + query.data.limit);
                  setSelected([]);
                }}
              >
                {t("下一页", "Next")}
              </button>
            </div>
          </div>
        </>
      )}
    </Section>
  );
}
