import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";
import { api } from "../../api/client";
import { LocaleProvider } from "../../app/locale";
import SettingsPage from "./SettingsPage";
import type { ConfigField, SettingsResponse } from "./types";

const confirm = vi.hoisted(() => vi.fn());
const currentUser = vi.hoisted(() => ({
  id: 1,
  username: "admin",
  role: "super_admin",
}));
vi.mock("../../components/ui/ConfirmProvider", () => ({
  useConfirm: () => confirm,
}));
vi.mock("../../app/auth", () => ({
  useAuth: () => ({ user: currentUser }),
}));
vi.mock("./AccountsPanel", () => ({ AccountsPanel: () => null }));
vi.mock("../../api/client", async (original) => ({
  ...(await original<object>()),
  api: vi.fn(),
}));

function field(id: string, group: string, advanced = false): ConfigField {
  return {
    id,
    key: id,
    path: [],
    category: "ai",
    group,
    type: "integer",
    label: id,
    description: "",
    editorSchema: null,
    optionLabels: {},
    scope: "global",
    permission: "super_admin",
    sensitive: false,
    writable: true,
    resettable: true,
    requiresRestart: false,
    advanced,
    constraints: { ge: 0, le: 20 },
    value: 2,
    default: 2,
    inheritedValue: 2,
    source: "database",
    version: "a".repeat(64),
    hasValue: true,
    overridden: true,
    effect: "live",
    submission: "automatic",
  };
}
const response = (fields: ConfigField[]): SettingsResponse => ({
  fields,
  messages: {},
  groups: { first: "First", second: "Second" },
  syncIntervalMs: 5000,
  csrf: "csrf",
});

it.each(["/settings/review", "/settings/agent?q=runtime"])(
  "asks before losing a plugin draft on navigation to %s and retains shared drafts",
  async (destination) => {
    vi.mocked(api).mockImplementation(async (path) => {
      if (path.startsWith("/api/v1/webui/plugins?"))
        return {
          plugins: {},
          profile: "default",
          profiles: ["default", "restricted"],
          messages: {},
          schema: { type: "object" },
        };
      return response([
        { ...field("global/retries", "first"), category: "agent" },
      ]);
    });
    const { router } = mount("/settings/agent#plugins");
    const settingsInput = await screen.findByRole("spinbutton", {
      name: "global/retries",
    });
    const profile = await screen.findByLabelText("Runtime permissions");
    fireEvent.change(settingsInput, { target: { value: "3" } });
    fireEvent.change(profile, { target: { value: "restricted" } });

    await act(async () => {
      await router.navigate("/settings/agent#section-first");
    });
    expect(confirm).not.toHaveBeenCalled();
    expect(profile).toHaveValue("restricted");

    confirm.mockResolvedValueOnce(false);
    await act(async () => {
      await router.navigate(destination);
    });
    expect(confirm).toHaveBeenCalledWith(
      expect.objectContaining({ title: "Discard panel changes?" }),
    );
    expect(router.state.location.pathname).toBe("/settings/agent");
    expect(router.state.location.search).toBe("");
    expect(profile).toHaveValue("restricted");

    await act(async () => {
      await router.navigate(destination);
    });
    await waitFor(() => {
      const { pathname, search } = router.state.location;
      expect(pathname + search).toBe(destination);
    });
    await act(async () => {
      await router.navigate("/settings/agent#plugins");
    });
    expect(await screen.findByLabelText("Runtime permissions")).toHaveValue(
      "default",
    );
    expect(
      await screen.findByRole("spinbutton", { name: "global/retries" }),
    ).toHaveValue(3);
  },
);
const accounts = [
  {
    id: "acct-one",
    name: "One",
    provider_id: "provider-one",
    enabled: true,
    default_model: "same-model",
    models: ["same-model", "other-model"],
  },
  {
    id: "acct-two",
    name: "Two",
    provider_id: "provider-two",
    enabled: true,
    default_model: "same-model",
    models: ["same-model"],
  },
  {
    id: "disabled",
    name: "Disabled",
    provider_id: "provider-one",
    enabled: false,
    default_model: "same-model",
    models: ["same-model"],
  },
  {
    id: "empty",
    name: "Empty",
    provider_id: "provider-one",
    enabled: true,
    default_model: "",
    models: [],
  },
];

function mount(initialEntry = "/settings/ai") {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const router = createMemoryRouter(
    [
      { path: "/settings/:category", element: <SettingsPage /> },
      { path: "/reviews", element: <div>Reviews page</div> },
    ],
    { initialEntries: [initialEntry] },
  );
  render(
    <QueryClientProvider client={client}>
      <LocaleProvider initial="en">
        <RouterProvider router={router} />
      </LocaleProvider>
    </QueryClientProvider>,
  );
  return { client, router };
}
beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  currentUser.id = 1;
  currentUser.username = "admin";
  currentUser.role = "super_admin";
  confirm.mockReset();
  confirm.mockResolvedValue(true);
  vi.mocked(api).mockImplementation(async (path) => {
    if (path === "/api/v1/config/ai/accounts") return { data: { accounts } };
    if (path === "/api/v1/config/ai/catalog")
      return { data: { providers: [] } };
    if (path.includes("&provider="))
      return response([
        field("model/provider-one/same-model/context_window_tokens", "first"),
      ]);
    return response([
      field("global/one", "first"),
      field("global/two", "second", true),
    ]);
  });
});

it("shows every settings group and advanced field continuously", async () => {
  mount();
  expect(
    await screen.findByRole("spinbutton", { name: "global/one" }),
  ).toBeVisible();
  expect(screen.getByRole("spinbutton", { name: "global/two" })).toBeVisible();
  expect(screen.queryByRole("combobox", { name: "Settings group" })).toBeNull();
  expect(screen.getByRole("heading", { name: "First" })).toBeVisible();
  expect(screen.getByRole("heading", { name: "Second" })).toBeVisible();
});

it("selects configured account models and reads capabilities by provider identity", async () => {
  mount();
  const account = await screen.findByRole("combobox", { name: "AI account" });
  fireEvent.change(account, { target: { value: "acct-one" } });
  const model = screen.getByRole("combobox", { name: "Model" });
  expect(model).toHaveValue("same-model");
  await screen.findByRole("spinbutton", { name: /model\/provider-one/ });
  expect(api).toHaveBeenCalledWith(
    expect.stringContaining("provider=provider-one&model=same-model"),
    expect.objectContaining({ signal: expect.any(AbortSignal) }),
  );
  expect(
    screen.getByRole("option", { name: /One.*provider-one/ }),
  ).toBeEnabled();
  expect(
    screen.getByRole("option", { name: /Two.*provider-two/ }),
  ).toBeEnabled();
  expect(screen.getByRole("option", { name: /Disabled/ })).toBeDisabled();
  expect(screen.getByRole("option", { name: /Empty/ })).toBeDisabled();
});

it("keeps unsaved model changes when switching is declined and handles account removal", async () => {
  const { client } = mount();
  const account = await screen.findByRole("combobox", { name: "AI account" });
  fireEvent.change(account, { target: { value: "acct-one" } });
  const input = await screen.findByRole("spinbutton", {
    name: /model\/provider-one/,
  });
  fireEvent.change(input, { target: { value: "7" } });
  confirm.mockResolvedValueOnce(false);
  fireEvent.change(screen.getByRole("combobox", { name: "Model" }), {
    target: { value: "other-model" },
  });
  await waitFor(() => expect(confirm).toHaveBeenCalled());
  expect(screen.getByRole("combobox", { name: "Model" })).toHaveValue(
    "same-model",
  );
  expect(input).toHaveValue(7);
  await act(async () => {
    client.setQueryData(["ai-accounts"], {
      data: { accounts: accounts.slice(1) },
    });
  });
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "selected account or model is unavailable",
  );
  expect(
    screen.queryByRole("spinbutton", { name: /model\/provider-one/ }),
  ).toBeNull();
  fireEvent.click(
    screen.getByRole("button", { name: "Discard model changes" }),
  );
  expect(screen.queryByText(/unsaved/i)).toBeNull();
});

it("preserves drafts across categories and global search while guarding leaving settings", async () => {
  const aiField: ConfigField = {
    ...field("global/ai_api_max_retries", "ai_strategy"),
    key: "ai_api_max_retries",
    category: "ai",
    label: "Max API retries",
    description: "Maximum retries per candidate before switching fallback.",
    searchText: "ai_api_max_retries Max API retries fallback retries",
  };
  const reviewField: ConfigField = {
    ...field("global/max_concurrent_reviews", "review"),
    key: "max_concurrent_reviews",
    category: "review",
    label: "Max concurrent reviews",
    description: "Number of pull requests reviewed simultaneously.",
    searchText: "max_concurrent_reviews Max concurrent reviews concurrency",
  };
  vi.mocked(api).mockImplementation(async (path) => {
    if (path === "/api/v1/config/ai/accounts") return { data: { accounts } };
    if (path === "/api/v1/config/ai/catalog")
      return { data: { providers: [] } };
    if (path.includes("search=true")) return response([aiField, reviewField]);
    if (path.includes("category=review")) return response([reviewField]);
    return response([aiField]);
  });

  const { router } = mount("/settings/ai");
  const retriesInput = await screen.findByRole("spinbutton", {
    name: "Max API retries",
  });
  expect(
    screen.getByText(
      "Maximum retries per candidate before switching fallback.",
    ),
  ).toBeVisible();

  fireEvent.change(retriesInput, { target: { value: "5" } });
  expect(screen.getByText(/1 unsaved/)).toBeVisible();
  expect(confirm).not.toHaveBeenCalled();

  // Navigate to Review category inside Settings: no confirmation prompt, draft preserved!
  fireEvent.click(screen.getByRole("link", { name: "Code review" }));
  const reviewInput = await screen.findByRole("spinbutton", {
    name: "Max concurrent reviews",
  });
  expect(confirm).not.toHaveBeenCalled();
  expect(screen.getByText(/1 unsaved/)).toBeVisible();

  fireEvent.change(reviewInput, { target: { value: "8" } });
  expect(screen.getByText(/2 unsaved/)).toBeVisible();

  // Search globally, inspect breadcrumb + match badge, jump to AI field, and keep both drafts!
  const searchBox = screen.getByRole("textbox", {
    name: "Search all settings",
  });
  fireEvent.change(searchBox, { target: { value: "ai_api_max_retries" } });
  expect(await screen.findByText(/AI & models ›/)).toBeVisible();
  expect(screen.getByText("Key: ai_api_max_retries")).toBeVisible();
  fireEvent.click(screen.getByRole("link", { name: /Max API retries/i }));

  const jumpedInput = await screen.findByRole("spinbutton", {
    name: "Max API retries",
  });
  expect(jumpedInput).toHaveValue(5);
  expect(
    screen.getByRole("button", {
      name: /Back to results for “ai_api_max_retries”/,
    }),
  ).toBeVisible();

  // Attempting to leave /settings/* triggers the blocker confirmation
  confirm.mockResolvedValueOnce(false);
  await act(async () => {
    void router.navigate("/reviews");
  });
  await waitFor(() => expect(confirm).toHaveBeenCalledTimes(1));
  expect(screen.queryByText("Reviews page")).toBeNull();
  expect(screen.getByText(/2 unsaved/)).toBeVisible();
});

it("reviews pending diffs, masks secret drafts without storage leaks, reverts single items, and confirms sensitive batch saves", async () => {
  const normalField: ConfigField = {
    ...field("global/max_concurrent_reviews", "review"),
    key: "max_concurrent_reviews",
    category: "review",
    label: "Max concurrent reviews",
    description: "Concurrent PR review workers.",
    requiresRestart: true,
    effect: "restart_required",
  };
  const secretField: ConfigField = {
    ...field("global/webhook_secret", "security"),
    key: "webhook_secret",
    category: "review",
    type: "string",
    label: "Webhook signing secret",
    description: "Verifies GitHub webhook signatures.",
    sensitive: true,
    constraints: {},
    value: "",
    default: "",
    inheritedValue: "",
    hasValue: true,
  };
  vi.mocked(api).mockImplementation(async (path, init) => {
    if (path === "/api/v1/config-center" && init?.method === "PATCH") {
      const payload = JSON.parse(String(init.body)) as {
        field: string;
        value: unknown;
        confirmed?: boolean;
      };
      if (payload.field === secretField.id) {
        expect(payload.confirmed).toBe(true);
        return {
          field: {
            ...secretField,
            value: "",
            hasValue: true,
            version: "b".repeat(64),
          },
        };
      }
      return {
        field: {
          ...normalField,
          value: payload.value as number,
          version: "b".repeat(64),
        },
      };
    }
    return response([normalField, secretField]);
  });

  mount("/settings/review");
  const numInput = await screen.findByRole("spinbutton", {
    name: "Max concurrent reviews",
  });
  const secretInput = screen.getByLabelText("Webhook signing secret", {
    selector: "input",
  });

  fireEvent.change(numInput, { target: { value: "6" } });
  fireEvent.change(secretInput, {
    target: { value: "super-secret-fixture-token-999" },
  });

  // Never persist secrets or drafts into localStorage or sessionStorage
  expect(JSON.stringify(localStorage)).not.toContain(
    "super-secret-fixture-token-999",
  );
  expect(JSON.stringify(sessionStorage)).not.toContain(
    "super-secret-fixture-token-999",
  );

  fireEvent.click(screen.getByRole("button", { name: "Review changes" }));
  const dialog = await screen.findByRole("dialog", {
    name: "Pending changes & save results",
  });
  expect(dialog).toHaveTextContent(
    "Configured credential → Will update (hidden)",
  );
  expect(dialog).not.toHaveTextContent("super-secret-fixture-token-999");

  // Revert normalField from inside ChangesDialog, leaving only secretField dirty
  fireEvent.click(
    screen.getByRole("button", { name: "Revert: Max concurrent reviews" }),
  );
  expect(numInput).toHaveValue(2);

  // Saving batch with sensitive credential requires confirmation
  confirm.mockResolvedValueOnce(true);
  fireEvent.click(screen.getByRole("button", { name: "Save changes (1)" }));
  await waitFor(() => expect(confirm).toHaveBeenCalledTimes(1));
  await waitFor(() =>
    expect(api).toHaveBeenCalledWith(
      "/api/v1/config-center",
      expect.objectContaining({ method: "PATCH" }),
    ),
  );
});
