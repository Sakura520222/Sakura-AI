import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { LocaleProvider } from "../../app/locale";
import { ConfirmProvider } from "../ui/ConfirmProvider";
import { api } from "../../api/client";
import { Markdown, safeExternalUrl } from "./Workflow";
import ReviewsPage from "../../features/reviews/ReviewsPage";
import IssuesPage from "../../features/issues/IssuesPage";
import RepositoriesPage from "../../features/repositories/RepositoriesPage";

vi.mock("../../api/client", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  api: vi.fn(),
}));
const identity = vi.hoisted(() => ({ superAdmin: false }));
vi.mock("../../app/auth", () => ({
  useAuth: () => ({
    permissions: { admin: true, super_admin: identity.superAdmin },
  }),
}));

function LocationProbe() {
  return <output data-testid="location">{useLocation().search}</output>;
}
function renderPage(children: ReactNode, path = "/") {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <LocaleProvider initial="en">
        <ConfirmProvider>
          <MemoryRouter initialEntries={[path]}>
            {children}
            <LocationProbe />
          </MemoryRouter>
        </ConfirmProvider>
      </LocaleProvider>
    </QueryClientProvider>,
  );
}
function result(data: unknown) {
  return { success: true, message: "ok", data };
}

beforeEach(() => {
  vi.mocked(api).mockReset();
  localStorage.clear();
  identity.superAdmin = false;
});

describe("workflow behavior", () => {
  it("keeps a zero review score, URL filters and pagination meaningful", async () => {
    const user = userEvent.setup();
    vi.mocked(api).mockResolvedValue(
      result({
        items: [
          {
            id: 1,
            pr_id: 12,
            title: "Review with zero score",
            repo_owner: "owner",
            repo_name: "repo",
            status: "completed",
            decision: "request_changes",
            overall_score: 0,
          },
        ],
        page: 1,
        total_pages: 2,
        total: 21,
        per_page: 20,
      }),
    );
    renderPage(
      <Routes>
        <Route path="/reviews" element={<ReviewsPage />} />
      </Routes>,
      "/reviews?search=parser&status=failed",
    );
    expect(
      await screen.findByRole("link", { name: "Review with zero score" }),
    ).toHaveAttribute("href", "/reviews/1");
    expect(screen.getByText("0")).toBeInTheDocument();
    expect(vi.mocked(api).mock.calls[0][0]).toContain(
      "search=parser&status=failed",
    );
    await user.click(screen.getByRole("button", { name: "Next page" }));
    expect(screen.getByTestId("location")).toHaveTextContent(
      "search=parser&status=failed&page=2",
    );
  });

  it("replaces stale pagination when a new filter is submitted", async () => {
    const user = userEvent.setup();
    vi.mocked(api).mockResolvedValue(
      result({ items: [], page: 1, total_pages: 0, total: 0, per_page: 20 }),
    );
    renderPage(
      <Routes>
        <Route path="/reviews" element={<ReviewsPage />} />
      </Routes>,
      "/reviews?page=5",
    );
    await user.type(
      screen.getByRole("textbox", {
        name: "Search title, repository or author",
      }),
      "owner/repo",
    );
    await user.click(screen.getByRole("button", { name: "Apply" }));
    expect(screen.getByTestId("location")).toHaveTextContent(
      "search=owner%2Frepo",
    );
    expect(screen.getByTestId("location")).not.toHaveTextContent("page=5");
  });

  it("filters review comments by exact file and severity", async () => {
    const user = userEvent.setup();
    vi.mocked(api).mockResolvedValue(
      result({
        id: 5,
        pr_id: 12,
        title: "Review",
        repo_owner: "owner",
        repo_name: "repo",
        comments: [
          {
            id: 1,
            file_path: "src/parser.py",
            line_number: 0,
            severity: "major",
            content: "Guard parser input",
          },
          {
            id: 2,
            file_path: "src/server.py",
            line_number: 10,
            severity: "minor",
            content: "Handle a disconnected client",
          },
        ],
      }),
    );
    renderPage(
      <Routes>
        <Route path="/reviews/:id" element={<ReviewsPage />} />
      </Routes>,
      "/reviews/5",
    );
    expect(await screen.findByText("Guard parser input")).toBeInTheDocument();
    expect(screen.getByText(":0")).toBeInTheDocument();
    await user.selectOptions(
      screen.getByRole("combobox", { name: "File" }),
      "src/parser.py",
    );
    expect(
      screen.queryByText("Handle a disconnected client"),
    ).not.toBeInTheDocument();
    await user.selectOptions(
      screen.getByRole("combobox", { name: "Severity" }),
      "minor",
    );
    expect(screen.queryByText("Guard parser input")).not.toBeInTheDocument();
    expect(screen.getByText("No matching feedback")).toBeInTheDocument();
  });

  it("does not offer reanalysis for a closed issue", async () => {
    vi.mocked(api).mockResolvedValue(
      result({
        id: 7,
        issue_number: 22,
        title: "Closed issue",
        issue_state: "closed",
        suggested_labels: [],
        suggested_assignees: [],
        related_prs: [],
      }),
    );
    renderPage(
      <Routes>
        <Route path="/issues/:id" element={<IssuesPage />} />
      </Routes>,
      "/issues/7",
    );
    expect(
      await screen.findByRole("button", { name: "Analyze again" }),
    ).toBeDisabled();
    expect(vi.mocked(api).mock.calls.every(([, init]) => !init?.method)).toBe(
      true,
    );
  });

  it("requires confirmation before submitting a new issue analysis and follows its durable task", async () => {
    const user = userEvent.setup();
    vi.mocked(api).mockImplementation(async (path, init) => {
      if (init?.method === "POST") return result({ task_id: "task-22" });
      if (path.includes("/tasks/"))
        return result({
          task_id: "task-22",
          issue_analysis_id: 8,
          status: "pending",
          dispatch_status: "processing",
        });
      return result({
        id: 7,
        issue_number: 22,
        title: "Open issue",
        issue_state: "open",
        suggested_labels: [],
        suggested_assignees: [],
        related_prs: [],
      });
    });
    renderPage(
      <Routes>
        <Route path="/issues/:id" element={<IssuesPage />} />
      </Routes>,
      "/issues/7",
    );
    await user.click(
      await screen.findByRole("button", { name: "Analyze again" }),
    );
    expect(
      vi.mocked(api).mock.calls.some(([, init]) => init?.method === "POST"),
    ).toBe(false);
    await user.click(screen.getByRole("button", { name: "Start analysis" }));
    expect(
      await screen.findByRole("link", { name: "View record" }),
    ).toHaveAttribute("href", "/issues/8");
    expect(
      screen.getByRole("button", { name: "Analyze again" }),
    ).toBeDisabled();
    expect(
      vi.mocked(api).mock.calls.filter(([, init]) => init?.method === "POST"),
    ).toHaveLength(1);
  });

  it("gates repository scans and submits indexing only after confirmation", async () => {
    const user = userEvent.setup();
    vi.mocked(api).mockImplementation(async (_path, init) =>
      init?.method === "POST"
        ? result(null)
        : result([
            {
              installation_id: 1,
              account_login: "owner",
              repos: [
                {
                  full_name: "owner/repo",
                  name: "repo",
                  private: false,
                  pr_count: 0,
                  issue_count: 0,
                  html_url: "https://github.com/owner/repo",
                },
              ],
            },
          ]),
    );
    renderPage(
      <Routes>
        <Route
          path="/repositories/:owner/:name"
          element={<RepositoriesPage />}
        />
      </Routes>,
      "/repositories/owner/repo",
    );
    await screen.findByRole("heading", { name: "owner/repo" });
    expect(
      screen.queryByRole("heading", { name: "Repository scan" }),
    ).not.toBeInTheDocument();
    await user.click(screen.getAllByRole("button", { name: "Start" })[0]);
    expect(
      vi.mocked(api).mock.calls.some(([, init]) => init?.method === "POST"),
    ).toBe(false);
    await user.click(screen.getAllByRole("button", { name: "Start" }).at(-1)!);
    await waitFor(() =>
      expect(
        vi
          .mocked(api)
          .mock.calls.some(
            ([path, init]) =>
              path === "/api/v1/repos/owner/repo/index-docs" &&
              init?.method === "POST",
          ),
      ).toBe(true),
    );
  });
});

describe("untrusted repository content", () => {
  it("does not render raw HTML, script URLs or external tracking images", () => {
    const { container } = render(
      <Markdown>
        {
          "<script>alert(1)</script>\n\n![tracker](https://tracker.invalid/pixel)\n\n[bad](javascript:alert%281%29)\n\n[good](https://github.com)"
        }
      </Markdown>,
    );
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector('[href^="javascript:"]')).toBeNull();
    expect(screen.getByRole("link", { name: "good" })).toHaveAttribute(
      "rel",
      "noopener noreferrer",
    );
  });
  it("accepts only credential-free HTTPS destinations", () => {
    expect(safeExternalUrl("javascript:alert(1)")).toBeUndefined();
    expect(
      safeExternalUrl("https://user:password@example.com"),
    ).toBeUndefined();
    expect(safeExternalUrl("https://github.com/owner/repo")).toBe(
      "https://github.com/owner/repo",
    );
  });
});
