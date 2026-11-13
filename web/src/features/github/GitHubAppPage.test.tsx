import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { api } from "../../api/client";
import { LocaleProvider } from "../../app/locale";
import GitHubAppPage from "./GitHubAppPage";

const identity = vi.hoisted(() => ({ user: { id: 1, username: "alice" } }));
vi.mock("../../app/auth", () => ({ useAuth: () => identity }));
vi.mock("../../api/client", () => ({ api: vi.fn() }));
const authorization = (username: string) => ({
  authorization: {
    status: "connected",
    github_username: username,
    installations: [],
    error_code: null,
  },
  authorization_url: null,
  install_url: null,
  repository_count: 0,
});
beforeEach(() => {
  vi.mocked(api).mockReset();
  identity.user = { id: 1, username: "alice" };
});

it("never renders another account authorization when the authenticated identity changes", async () => {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  });
  vi.mocked(api)
    .mockResolvedValueOnce(authorization("alice"))
    .mockResolvedValueOnce(authorization("bob"));
  const view = () => (
    <QueryClientProvider client={client}>
      <LocaleProvider initial="en">
        <GitHubAppPage />
      </LocaleProvider>
    </QueryClientProvider>
  );
  const { rerender } = render(view());
  expect(await screen.findByText(/@alice/)).toBeInTheDocument();
  identity.user = { id: 2, username: "bob" };
  rerender(view());
  expect(screen.queryByText(/@alice/)).not.toBeInTheDocument();
  expect(await screen.findByText(/@bob/)).toBeInTheDocument();
  expect(api).toHaveBeenCalledTimes(2);
  client.clear();
});

it("aborts pending authorization discovery when the user changes", async () => {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  let firstSignal: AbortSignal | undefined;
  vi.mocked(api)
    .mockImplementationOnce((_url, options) => {
      firstSignal = options?.signal as AbortSignal;
      return new Promise(() => {});
    })
    .mockResolvedValueOnce(authorization("bob"));
  const view = () => (
    <QueryClientProvider client={client}>
      <LocaleProvider initial="en">
        <GitHubAppPage />
      </LocaleProvider>
    </QueryClientProvider>
  );
  const { rerender } = render(view());
  await waitFor(() => expect(firstSignal).toBeDefined());
  identity.user = { id: 2, username: "bob" };
  rerender(view());
  expect(await screen.findByText(/@bob/)).toBeInTheDocument();
  expect(firstSignal?.aborted).toBe(true);
  client.clear();
});
