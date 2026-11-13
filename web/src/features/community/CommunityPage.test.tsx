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
