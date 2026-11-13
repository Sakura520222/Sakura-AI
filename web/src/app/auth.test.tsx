import {
  QueryClient,
  QueryClientProvider,
  useQuery,
} from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { api } from "../api/client";
import { AuthProvider, type Bootstrap, useAuth } from "./auth";

vi.mock("../api/client", async (original) => ({
  ...(await original<object>()),
  api: vi.fn(),
}));
const bootstrap = (id: number): Bootstrap => ({
  user: {
    id,
    username: id === 1 ? "alice" : "bob",
    display_name: "Member",
    role: "user",
    avatar_url: null,
  },
  preferences: { language: "en", theme: "light", items_per_page: 20 },
  permissions: { admin: false, super_admin: false, manage_settings: false },
  features: {
    payment_enabled: false,
    agent_enabled: false,
    github_app_configured: true,
  },
  csrf: "csrf",
  version: "dev",
  timezone: "UTC",
  links: {
    login: "/app/login",
    logout: "/api/v1/webui/logout",
    mfa: "/app/security",
  },
});
function PrivatePage() {
  const auth = useAuth();
  const query = useQuery({
    queryKey: ["private-data"],
    queryFn: () => api<string>("/private"),
  });
  return (
    <div>
      {auth.user.username}: {query.data}
    </div>
  );
}
beforeEach(() => {
  vi.mocked(api).mockReset();
  window.matchMedia = vi
    .fn()
    .mockReturnValue({
      matches: false,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    });
});

it("clears existing private queries before rendering a different authenticated user", async () => {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
  vi.mocked(api)
    .mockResolvedValueOnce(bootstrap(1))
    .mockResolvedValueOnce("alice private")
    .mockResolvedValueOnce("bob private");
  render(
    <QueryClientProvider client={client}>
      <AuthProvider>
        <PrivatePage />
      </AuthProvider>
    </QueryClientProvider>,
  );
  expect(await screen.findByText("alice: alice private")).toBeInTheDocument();
  act(() => {
    client.setQueryData(["bootstrap"], bootstrap(2));
  });
  expect(screen.queryByText("bob: alice private")).not.toBeInTheDocument();
  expect(await screen.findByText("bob: bob private")).toBeInTheDocument();
  client.clear();
});

it("cancels pending private fetches and removes their cache when a session expires", async () => {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
  vi.mocked(api)
    .mockResolvedValueOnce(bootstrap(1))
    .mockResolvedValueOnce("alice private");
  render(
    <QueryClientProvider client={client}>
      <AuthProvider>
        <PrivatePage />
      </AuthProvider>
    </QueryClientProvider>,
  );
  await screen.findByText("alice: alice private");
  let signal: AbortSignal | undefined;
  const pending = client
    .fetchQuery({
      queryKey: ["pending-private"],
      queryFn: (ctx) => {
        signal = ctx.signal;
        return new Promise(() => {});
      },
    })
    .catch(() => {});
  await waitFor(() => expect(signal).toBeDefined());
  act(() => {
    window.dispatchEvent(new Event("sakura:session-expired"));
  });
  expect(
    await screen.findByRole("link", { name: /Sign in/ }),
  ).toBeInTheDocument();
  expect(signal?.aborted).toBe(true);
  expect(client.getQueryData(["private-data"])).toBeUndefined();
  expect(client.getQueryData(["pending-private"])).toBeUndefined();
  await pending;
  client.clear();
});
