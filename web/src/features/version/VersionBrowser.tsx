import { useState } from "react";
import type { UseQueryResult } from "@tanstack/react-query";
import { Check, ChevronLeft, ChevronRight, Search } from "lucide-react";
import { useLocale } from "../../app/locale";
import { Dialog } from "../../components/ui/Dialog";
import {
  EmptyState,
  ErrorState,
  LoadingState,
  Markdown,
  RefreshButton,
  Timestamp,
} from "../../components/workflow/Workflow";
import {
  targetForImage,
  type Catalog,
  type ImageEntry,
  type Target,
} from "./state";

export interface ReleaseRecord {
  tag_name?: string;
  version?: string;
  name?: string;
  body?: string;
  published_at?: string;
}

const PAGE_SIZE = 6;

export default function VersionBrowser({
  catalog,
  releases,
  selected,
  selectionDisabled,
  onSelect,
}: {
  catalog: UseQueryResult<Catalog, Error>;
  releases: UseQueryResult<{ releases: ReleaseRecord[] }, Error>;
  selected: Target | null;
  selectionDisabled: boolean;
  onSelect: (image: ImageEntry) => Promise<void>;
}) {
  const { t } = useLocale();
  const [view, setView] = useState<"catalog" | "releases">("catalog");
  const [channel, setChannel] = useState("stable");
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(1);
  const [imageDetail, setImageDetail] = useState<ImageEntry | null>(null);
  const [releaseDetail, setReleaseDetail] = useState<ReleaseRecord | null>(
    null,
  );
  const term = search.trim().toLowerCase();
  const images = (catalog.data?.images ?? []).filter(
    (image) =>
      (channel === "other"
        ? !["stable", "development"].includes(image.channel || "")
        : image.channel === channel) &&
      [
        image.version,
        image.canonical_tag,
        image.revision,
        image.digest,
        ...image.tags,
      ]
        .join(" ")
        .toLowerCase()
        .includes(term),
  );
  const history = (releases.data?.releases ?? []).filter((release) =>
    [release.name, release.tag_name, release.version]
      .join(" ")
      .toLowerCase()
      .includes(term),
  );
  const total = view === "catalog" ? images.length : history.length;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const currentPage = Math.min(page, pages);
  const offset = (currentPage - 1) * PAGE_SIZE;
  const query = view === "catalog" ? catalog : releases;
  const title = (image: ImageEntry) =>
    image.version ||
    image.canonical_tag ||
    image.tags[0] ||
    t("未命名版本", "Unnamed version");
  const releaseTitle = (release: ReleaseRecord) =>
    release.name ||
    release.tag_name ||
    release.version ||
    t("发布说明", "Release notes");
  function changeView(next: typeof view) {
    setView(next);
    setPage(1);
    setSearch("");
  }

  return (
    <section
      className="wf-section version-browser"
      aria-label={t("版本与发布记录", "Versions and release history")}
    >
      <div className="version-browser-heading">
        <div
          className="version-browser-tabs"
          role="group"
          aria-label={t("版本视图", "Version view")}
        >
          <button
            aria-pressed={view === "catalog"}
            onClick={() => changeView("catalog")}
          >
            {t("可用版本", "Available versions")}
          </button>
          <button
            aria-pressed={view === "releases"}
            onClick={() => changeView("releases")}
          >
            {t("发布记录", "Release history")}
          </button>
        </div>
        <RefreshButton
          busy={query.isFetching}
          onClick={() => void query.refetch()}
        />
      </div>
      <div className="version-browser-filters">
        {view === "catalog" && (
          <select
            aria-label={t("更新通道", "Update channel")}
            value={channel}
            onChange={(event) => {
              setChannel(event.target.value);
              setPage(1);
            }}
          >
            <option value="stable">{t("稳定版", "Stable")}</option>
            <option value="development">{t("开发版", "Development")}</option>
            <option value="other">{t("其他镜像", "Other images")}</option>
          </select>
        )}
        <label className="version-search">
          <Search size={16} />
          <input
            type="search"
            aria-label={t("搜索版本", "Search versions")}
            placeholder={
              view === "catalog"
                ? t(
                    "搜索版本、标签或提交…",
                    "Search versions, tags or commits…",
                  )
                : t("搜索发布记录…", "Search releases…")
            }
            value={search}
            onChange={(event) => {
              setSearch(event.target.value);
              setPage(1);
            }}
          />
        </label>
      </div>
      {view === "catalog" && catalog.data?.stale && (
        <p className="version-notice">
          {t(
            "当前显示的是缓存目录，刷新成功前无法选择更新目标。",
            "This is a cached catalog. Refresh it successfully before selecting an update target.",
          )}
        </p>
      )}
      {query.isPending ? (
        <LoadingState />
      ) : query.isError ? (
        <ErrorState retry={() => void query.refetch()} />
      ) : (
        <>
          <div className="version-browser-list">
            {!total ? (
              <EmptyState
                title={t("没有匹配的版本", "No matching versions")}
                description={t(
                  "可以更换通道、清除搜索或刷新列表。",
                  "Change the channel, clear the search or refresh the list.",
                )}
              />
            ) : view === "catalog" ? (
              images.slice(offset, offset + PAGE_SIZE).map((image, index) => {
                const chosen =
                  !!selected &&
                  selected.digest === image.digest &&
                  selected.tag === image.canonical_tag;
                return (
                  <article
                    className={`version-row${chosen ? " selected" : ""}`}
                    key={`${image.digest}-${image.canonical_tag}-${index}`}
                  >
                    <div className="version-row-copy">
                      <div className="version-row-title">
                        <strong title={title(image)}>{title(image)}</strong>
                        {image.revision && (
                          <code>{image.revision.slice(0, 8)}</code>
                        )}
                        {chosen && (
                          <span className="version-selected-label">
                            {t("已选择", "Selected")}
                          </span>
                        )}
                      </div>
                      <span
                        className="version-row-meta"
                        title={image.canonical_tag || image.tags.join(", ")}
                      >
                        {image.canonical_tag || image.tags.join(", ")}
                      </span>
                    </div>
                    <div className="version-row-actions">
                      <button
                        className="button"
                        aria-label={t("查看 {0} 详情", "View details for {0}", [
                          title(image),
                        ])}
                        onClick={() => setImageDetail(image)}
                      >
                        {t("详情", "Details")}
                      </button>
                      <button
                        className="button"
                        disabled={
                          chosen ||
                          selectionDisabled ||
                          !targetForImage(image) ||
                          catalog.data?.stale
                        }
                        onClick={() => void onSelect(image)}
                      >
                        {chosen ? (
                          <>
                            <Check size={15} />
                            {t("已选择", "Selected")}
                          </>
                        ) : (
                          t("选择", "Select")
                        )}
                      </button>
                    </div>
                  </article>
                );
              })
            ) : (
              history
                .slice(offset, offset + PAGE_SIZE)
                .map((release, index) => (
                  <article
                    className="version-row"
                    key={release.tag_name || `${offset}-${index}`}
                  >
                    <div className="version-row-copy">
                      <strong
                        className="version-release-name"
                        title={releaseTitle(release)}
                      >
                        {releaseTitle(release)}
                      </strong>
                      <span className="version-row-meta">
                        {release.published_at ? (
                          <Timestamp value={release.published_at} />
                        ) : (
                          release.tag_name || release.version || "—"
                        )}
                      </span>
                    </div>
                    <button
                      className="button"
                      onClick={() => setReleaseDetail(release)}
                    >
                      {t("查看说明", "Read notes")}
                    </button>
                  </article>
                ))
            )}
          </div>
          <nav
            className="version-pagination"
            aria-label={t("版本分页", "Version pages")}
          >
            <span>{t("共 {0} 项", "{0} items", [total])}</span>
            <div>
              <button
                className="button"
                disabled={currentPage <= 1}
                onClick={() => setPage(currentPage - 1)}
                aria-label={t("上一页", "Previous page")}
              >
                <ChevronLeft size={16} />
              </button>
              <span aria-live="polite">
                {currentPage} / {pages}
              </span>
              <button
                className="button"
                disabled={currentPage >= pages}
                onClick={() => setPage(currentPage + 1)}
                aria-label={t("下一页", "Next page")}
              >
                <ChevronRight size={16} />
              </button>
            </div>
          </nav>
        </>
      )}
      <Dialog
        open={!!imageDetail}
        onOpenChange={(open) => {
          if (!open) setImageDetail(null);
        }}
        title={t("版本详情", "Version details")}
      >
        {imageDetail && (
          <dl className="version-detail-grid">
            <dt>{t("版本", "Version")}</dt>
            <dd>{title(imageDetail)}</dd>
            <dt>{t("通道", "Channel")}</dt>
            <dd>{imageDetail.channel || "—"}</dd>
            <dt>{t("镜像标签", "Image tag")}</dt>
            <dd>
              <code>{imageDetail.canonical_tag || "—"}</code>
            </dd>
            <dt>{t("镜像摘要", "Image digest")}</dt>
            <dd>
              <code>{imageDetail.digest || "—"}</code>
            </dd>
            <dt>{t("源码提交", "Source revision")}</dt>
            <dd>
              <code>{imageDetail.revision || "—"}</code>
            </dd>
            <dt>{t("全部标签", "All tags")}</dt>
            <dd>
              {imageDetail.tags.map((tag) => (
                <code className="version-tag" key={tag}>
                  {tag}
                </code>
              ))}
            </dd>
            {imageDetail.reason && (
              <>
                <dt>{t("状态说明", "Status details")}</dt>
                <dd>{imageDetail.reason}</dd>
              </>
            )}
          </dl>
        )}
      </Dialog>
      <Dialog
        open={!!releaseDetail}
        onOpenChange={(open) => {
          if (!open) setReleaseDetail(null);
        }}
        title={
          releaseDetail
            ? releaseTitle(releaseDetail)
            : t("发布说明", "Release notes")
        }
      >
        {releaseDetail && (
          <>
            {releaseDetail.published_at && (
              <Timestamp value={releaseDetail.published_at} />
            )}
            <Markdown>
              {releaseDetail.body ||
                t("此版本没有发布说明。", "This release has no notes.")}
            </Markdown>
          </>
        )}
      </Dialog>
    </section>
  );
}
