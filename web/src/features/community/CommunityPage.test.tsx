import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { expect, it, vi } from "vitest";
import { api } from "../../api/client";
import { LocaleProvider } from "../../app/locale";
import { ConfirmProvider } from "../../components/ui/ConfirmProvider";
import CommunityPage from "./CommunityPage";
vi.mock("../../api/client", async (original) => ({
  ...(await original<object>()),
  api: vi.fn(),
}));
it("separates management and keeps the repository picker out of discovery", async () => {
  const repo = {
    id: 1,
    full_name: "alice/project",
    html_url: "https://github.com/alice/project",
    description: "Example",
    ai_summary: "Project summary text",
    stargazers_count: 10,
    primary_language: "Python",
    topics: ["ai"],
    is_displayed: true,
  };
  vi.mocked(api).mockResolvedValue({
    feature_enabled: true,
    is_admin: true,
    is_super_admin: true,
    credential_status: "authorized",
    member: { status: "active" },
    available_repos: [repo],
    displayed_repos: [repo],
    public_repos: [repo],
    admin_members: [{ user_id: 2, github_username: "bob", status: "active" }],
    admin_member_page: { total: 1, page: 1, total_pages: 1 },
    admin_repositories: [repo],
    admin_repository_page: { total: 1, page: 1, total_pages: 1 },
  });
  render(
    <QueryClientProvider client={new QueryClient()}>
      <LocaleProvider initial="en">
        <ConfirmProvider>
          <MemoryRouter>
            <CommunityPage />
          </MemoryRouter>
        </ConfirmProvider>
      </LocaleProvider>
    </QueryClientProvider>,
  );
  await screen.findByRole("heading", { name: "Community projects" });
  expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
  expect(screen.queryByText("bob")).not.toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: "Disable feature" }),
  ).not.toBeInTheDocument();
  const user = userEvent.setup();
  await user.click(
    screen.getByRole("button", { name: "My shared repositories" }),
  );
  expect(screen.getByRole("dialog")).toBeInTheDocument();
  expect(screen.getByRole("checkbox")).toBeChecked();
  await user.click(screen.getByRole("button", { name: "Close" }));
  await user.click(screen.getByRole("tab", { name: "Management" }));
  expect(screen.getByText("bob")).toBeInTheDocument();
  expect(
    screen.queryByRole("heading", { name: "Community projects" }),
  ).not.toBeInTheDocument();
});

it("supports Enter-key admin search, localized status options, table headers, and conditional summary expansion", async () => {
  const shortRepo = {
    id: 1,
    full_name: "alice/short",
    html_url: "https://github.com/alice/short",
    description: "Short project",
    ai_summary: "Brief one-line summary.",
    stargazers_count: 5,
    primary_language: "TypeScript",
    topics: ["webui"],
    is_displayed: true,
  };
  const longRepo = {
    id: 2,
    full_name: "alice/detailed",
    html_url: "https://github.com/alice/detailed",
    description: "Detailed project",
    ai_summary:
      "Line 1 of detailed AI summary.\nLine 2 of detailed AI summary.\nLine 3 of detailed AI summary.\nLine 4 with extra context that exceeds the preview clamp.",
    stargazers_count: 12,
    primary_language: "Python",
    topics: ["review"],
    is_displayed: true,
  };
  vi.mocked(api).mockClear();
  vi.mocked(api).mockResolvedValue({
    feature_enabled: true,
    is_admin: true,
    is_super_admin: true,
    credential_status: "authorized",
    member: { status: "active" },
    available_repos: [shortRepo, longRepo],
    displayed_repos: [shortRepo, longRepo],
    public_repos: [shortRepo, longRepo],
    admin_members: [{ user_id: 2, github_username: "bob", status: "active" }],
    admin_member_page: { total: 1, page: 1, total_pages: 1 },
    admin_repositories: [shortRepo],
    admin_repository_page: { total: 1, page: 1, total_pages: 1 },
  });
  render(
    <QueryClientProvider client={new QueryClient()}>
      <LocaleProvider initial="en">
        <ConfirmProvider>
          <MemoryRouter>
            <CommunityPage />
          </MemoryRouter>
        </ConfirmProvider>
      </LocaleProvider>
    </QueryClientProvider>,
  );
  await screen.findByRole("heading", { name: "Community projects" });
  expect(
    screen.getAllByRole("button", { name: "Expand summary" }),
  ).toHaveLength(1);

  const user = userEvent.setup();
  await user.click(screen.getByRole("tab", { name: "Management" }));
  expect(
    screen.getByRole("columnheader", { name: "Member" }),
  ).toBeInTheDocument();
  expect(
    screen.getByRole("columnheader", { name: "Repository" }),
  ).toBeInTheDocument();
  const statusSelect = screen.getByRole("combobox", { name: "Member status" });
  expect(statusSelect).toHaveDisplayValue("All statuses");

  const memberSearch = screen.getByRole("textbox", { name: "Search members" });
  await user.type(memberSearch, "bob{Enter}");
  expect(api).toHaveBeenCalledWith(
    expect.stringContaining("member_q=bob"),
    expect.anything(),
  );
});
