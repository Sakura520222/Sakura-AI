import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";
import { api } from "../../api/client";
import { useAuth } from "../../app/auth";
import { LocaleProvider } from "../../app/locale";
import { ConfirmProvider } from "../ui/ConfirmProvider";
import AppShell from "./AppShell";

vi.mock("../../api/client", async (original) => ({
  ...(await original<object>()),
  api: vi.fn(),
}));
vi.mock("../../app/auth", () => ({ useAuth: vi.fn() }));
vi.mock("../../app/theme", () => ({
  useTheme: () => ({ theme: "light", setTheme: vi.fn() }),
}));

function mount(superAdmin = true) {
  vi.mocked(useAuth).mockReturnValue({
    user: {
      id: 1,
      username: "admin",
      display_name: "Admin",
      avatar_url: null,
      role: superAdmin ? "super_admin" : "user",
    },
    version: "3.2.5",
    permissions: { super_admin: superAdmin, admin: superAdmin },
    links: { logout: "/logout", login: "/login" },
  } as ReturnType<typeof useAuth>);
  return render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <MemoryRouter>
        <LocaleProvider initial="en">
          <ConfirmProvider>
            <AppShell />
          </ConfirmProvider>
        </LocaleProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.mocked(api).mockReset();
  vi.mocked(api).mockImplementation(async (path) => {
    if (path === "/api/v1/version/info")
      return {
        current_version: "3.2.6",
        build: { channel: "development" },
        update_available: true,
      };
    if (path === "/health")
      return { status: "healthy", startup_time: "before" };
    if (path.startsWith("/api/v1/webui/system/restart"))
      return { success: true, restarting: true };
    throw new Error(path);
  });
});

it("shows current running version and update availability separately with a Star icon", async () => {
  mount();
  expect(await screen.findByText("3.2.6")).toBeVisible();
  expect(screen.getByText("Update available")).toBeVisible();
  const community = screen.getByRole("link", { name: "Star community" });
  expect(community.querySelector(".lucide-star")).not.toBeNull();
});

it("keeps the version visible without privileged management actions for ordinary users", () => {
  mount(false);
  expect(screen.getByText("3.2.5")).toBeVisible();
  expect(
    screen.queryByRole("link", { name: "Updates" }),
  ).not.toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: "Restart application" }),
  ).not.toBeInTheDocument();
});

it("requires confirmation before using the existing restart endpoint", async () => {
  const user = userEvent.setup();
  mount();
  await user.click(screen.getByRole("button", { name: "Restart application" }));
  const dialog = screen.getByRole("dialog", { name: "Restart Sakura AI?" });
  expect(
    vi.mocked(api).mock.calls.filter(([path]) => path.includes("/restart")),
  ).toHaveLength(0);
  await user.click(within(dialog).getByRole("button", { name: "Restart" }));
  expect(await screen.findByText("Application restarting")).toBeVisible();
  expect(
    vi.mocked(api).mock.calls.find(([path]) => path.includes("/restart"))?.[1]
      ?.method,
  ).toBe("POST");
});
