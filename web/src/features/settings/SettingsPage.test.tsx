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
vi.mock("../../components/ui/ConfirmProvider", () => ({
  useConfirm: () => confirm,
}));
vi.mock("../../app/auth", () => ({
  useAuth: () => ({ user: { role: "super_admin" } }),
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

function mount() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const router = createMemoryRouter(
    [{ path: "/settings/:category", element: <SettingsPage /> }],
    { initialEntries: ["/settings/ai"] },
  );
  render(
    <QueryClientProvider client={client}>
      <LocaleProvider initial="en">
        <RouterProvider router={router} />
      </LocaleProvider>
    </QueryClientProvider>,
  );
  return client;
}
beforeEach(() => {
  localStorage.clear();
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
  const client = mount();
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
});
