import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { api } from "../../api/client";
import { LocaleProvider } from "../../app/locale";
import { ConfirmProvider } from "../../components/ui/ConfirmProvider";
import BillingPage from "./BillingPage";
import AdminPage from "../admin/AdminPage";
import PlansAdmin from "../admin/PlansAdmin";
import { Money } from "./BillingCommon";
import RefundsAdmin from "../admin/RefundsAdmin";
import PricingAdmin from "../admin/PricingAdmin";
import {
  exactCredits,
  moneyToMinor,
  parseIntegerInput,
  validateCreditInput,
} from "./billingTypes";

vi.mock("../../api/client", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  api: vi.fn(),
}));
const identity = vi.hoisted(() => ({
  superAdmin: false,
  admin: false,
  payments: false,
}));
vi.mock("../../app/auth", () => ({
  useAuth: () => ({
    user: { id: 1 },
    permissions: { admin: identity.admin, super_admin: identity.superAdmin },
    features: { payment_enabled: identity.payments },
  }),
}));
function renderPage(
  children: ReactNode,
  path: string,
  language: "en" | "zh-CN" = "en",
) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <LocaleProvider initial={language}>
        <ConfirmProvider>
          <MemoryRouter initialEntries={[path]}>{children}</MemoryRouter>
        </ConfirmProvider>
      </LocaleProvider>
    </QueryClientProvider>,
  );
}
beforeEach(() => {
  vi.mocked(api).mockReset();
  localStorage.clear();
  identity.superAdmin = false;
  identity.admin = false;
  identity.payments = false;
});

describe("exact financial values", () => {
  it("formats Credits without rounding the last microcredit", () => {
    expect(exactCredits("9007199254.000001")).toBe("9007199254.000001");
    expect(exactCredits("-0.010000")).toBe("-0.01");
    expect(exactCredits("0.000000")).toBe("0");
    expect(exactCredits(null)).toBe("—");
  });
  it("converts decimal money only using the supplied currency exponent", () => {
    expect(moneyToMinor("123.45", 2)).toBe(12345);
    expect(moneyToMinor("123", 0)).toBe(123);
    expect(moneyToMinor("0.000001", 6)).toBe(1);
    expect(() => moneyToMinor("1.23", 0)).toThrow();
    expect(() => moneyToMinor("1", -1)).toThrow();
    expect(() => moneyToMinor("9007199254740992", 0)).toThrow();
    expect(() => moneyToMinor("NaN", 2)).toThrow();
  });
  it("does not coerce empty, fractional, negative or imprecise integers", () => {
    expect(() => parseIntegerInput("")).toThrow();
    expect(() => parseIntegerInput("1.1")).toThrow();
    expect(() => parseIntegerInput("-1")).toThrow();
    expect(() => parseIntegerInput("9007199254740992")).toThrow();
    expect(parseIntegerInput("0")).toBe(0);
    expect(validateCreditInput("0.000001")).toBe("0.000001");
    expect(() => validateCreditInput("0.0000001")).toThrow();
  });
});

describe("billing and administration boundaries", () => {
  it("keeps wallet available with sales disabled and sends an exact quoted threshold", async () => {
    const user = userEvent.setup();
    vi.mocked(api).mockResolvedValue({
      balance: "9007199254.000001",
      available: "9007199254.000001",
      reserved: "0",
      low_balance_threshold: "0",
      low_balance: false,
    });
    renderPage(
      <Routes>
        <Route path="/billing/*" element={<BillingPage />} />
      </Routes>,
      "/billing",
    );
    expect(
      (await screen.findAllByText("9007199254.000001", { selector: "strong" }))
        .length,
    ).toBeGreaterThan(0);
    expect(
      screen.queryByRole("link", { name: "Plans" }),
    ).not.toBeInTheDocument();
    const input = screen.getByRole("textbox", {
      name: "Alert threshold (Credits)",
    });
    await user.clear(input);
    await user.type(input, "0.000001");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(
        vi
          .mocked(api)
          .mock.calls.some(
            ([path, init]) =>
              path.endsWith("/threshold") &&
              init?.body === '{"credits":"0.000001"}',
          ),
      ).toBe(true),
    );
  });
  it("does not fetch admin data without the matching role", () => {
    renderPage(
      <Routes>
        <Route path="/admin/*" element={<AdminPage />} />
      </Routes>,
      "/admin/pricing",
    );
    expect(
      screen.getByText("Administrator access required"),
    ).toBeInTheDocument();
    expect(api).not.toHaveBeenCalled();
  });
  it("does not expose super-admin billing tools to an ordinary administrator", () => {
    identity.admin = true;
    renderPage(
      <Routes>
        <Route path="/admin/*" element={<AdminPage />} />
      </Routes>,
      "/admin/pricing",
    );
    expect(
      screen.getByText("Super administrator access required"),
    ).toBeInTheDocument();
    expect(api).not.toHaveBeenCalled();
  });
  it("keeps an unknown refund pending instead of reporting approval or retrying it", async () => {
    const user = userEvent.setup();
    const row = {
      id: 9,
      order_id: 3,
      order_no: "ORDER-3",
      status: "pending",
      user_id: 1,
      amount_cents: 123,
      formatted_amount: "1.23",
      currency: "USD",
      requested_at: null,
      reason: "Unused",
    };
    vi.mocked(api).mockImplementation(async (_path, init) =>
      init?.method === "POST"
        ? row
        : { items: [row], total: 1, offset: 0, limit: 20 },
    );
    renderPage(<RefundsAdmin />, "/admin/refunds");
    await user.click(
      await screen.findByRole("button", { name: "Approve refund" }),
    );
    expect(
      vi.mocked(api).mock.calls.filter(([, init]) => init?.method === "POST"),
    ).toHaveLength(0);
    await user.click(screen.getByRole("button", { name: "Confirm" }));
    expect(
      await screen.findByText(/Awaiting reconciliation/),
    ).toBeInTheDocument();
    expect(
      vi.mocked(api).mock.calls.filter(([, init]) => init?.method === "POST"),
    ).toHaveLength(1);
  });
  it("discovers models only for the selected configured account and bypasses cached discovery on refresh", async () => {
    const user = userEvent.setup();
    vi.mocked(api).mockImplementation(async (path) => {
      if (path.endsWith("/pricing-context"))
        return {
          accounts: [
            {
              id: "account-a",
              name: "Account A",
              provider_id: "provider-a",
              models: ["saved-model"],
              default_model: "saved-model",
              enabled: true,
            },
          ],
          auxiliary: [],
          call_kinds: ["chat"],
          units: ["tokens"],
          currencies: ["USD"],
        };
      if (path.includes("/models"))
        return {
          success: true,
          data: { models: ["saved-model"], discovery_failed: true },
        };
      if (path.endsWith("/admin/plans")) return [];
      return { items: [], offset: 0, limit: 20, total: 0 };
    });
    renderPage(<PricingAdmin />, "/admin/pricing");
    await user.selectOptions(
      await screen.findByRole("combobox", { name: "Billing source" }),
      "account:account-a",
    );
    await screen.findByText("Discovery failed; saved models remain available");
    expect(screen.getByRole("combobox", { name: "Model" })).toHaveValue(
      "saved-model",
    );
    await user.click(screen.getByRole("button", { name: "Refresh models" }));
    await waitFor(() =>
      expect(
        vi
          .mocked(api)
          .mock.calls.some(
            ([path]) =>
              path ===
              "/api/v1/billing/admin/pricing/accounts/account-a/models?refresh=true",
          ),
      ).toBe(true),
    );
  });
});

describe("pricing editor", () => {
  const stored = {
    unit: "tokens",
    currency: "USD",
    settlement_currency: "CNY",
    fx_rate: "7",
    markup: "1.25",
    credits_per_currency_unit: "10",
    input_price: "2",
    output_price: "3",
  };
  const precision = "0.000000000000000000000000000000001";

  function openEditor(withPaymentEvent = false) {
    const publish = vi
      .fn<(config: Record<string, unknown>) => Promise<unknown>>()
      .mockResolvedValue({});
    vi.mocked(api).mockImplementation(async (path, init) => {
      if (path.endsWith("/pricing-context"))
        return {
          accounts: [
            {
              id: "a",
              name: "Account A",
              provider_id: "openai",
              models: ["m"],
              default_model: "m",
              enabled: true,
            },
          ],
          auxiliary: [],
          call_kinds: ["chat"],
          units: ["tokens", "documents"],
          currencies: ["USD", "CNY"],
        };
      if (path.endsWith("/models"))
        return {
          success: true,
          data: { models: ["m"], discovery_failed: false },
        };
      if (path.includes("/admin/pricing?"))
        return {
          items: [
            {
              id: 7,
              version: 1,
              account_id: "a",
              provider_id: "openai",
              model_id: "m",
              call_kind: "chat",
              config: stored,
              created_at: "2026-01-01T00:00:00Z",
            },
          ],
          total: 1,
          offset: 0,
          limit: 20,
        };
      if (path === "/api/v1/billing/admin/pricing" && init?.method === "POST")
        return publish(JSON.parse(String(init.body)).config);
      if (path.endsWith("/admin/plans")) return [];
      if (withPaymentEvent && path.includes("/admin/payment-events?"))
        return {
          items: [
            {
              id: 8,
              event_key: "currency-choice",
              provider: "stripe",
              status: "pending_reconciliation",
              pending_reason: "unknown_order",
              type: "refund",
              amount_cents: null,
              currency: null,
              formatted_amount: null,
              order_id: null,
              order_no: null,
              received_at: "2026-01-01T00:00:00Z",
            },
          ],
          total: 1,
          offset: 0,
          limit: 20,
        };
      return { items: [], total: 0, offset: 0, limit: 20 };
    });
    renderPage(<PricingAdmin />, "/admin/pricing");
    return publish;
  }

  it("leaves commercial fields blank until a published version is loaded", async () => {
    const user = userEvent.setup();
    const publish = openEditor();
    for (const label of [
      "Provider currency",
      "Settlement currency",
      "FX rate",
      "Markup multiplier",
      "Credits per settlement currency unit",
    ]) {
      expect(await screen.findByLabelText(label)).toHaveValue("");
    }
    await user.click(
      screen.getByRole("button", { name: "Use for new version" }),
    );
    expect(screen.getByLabelText("Model")).toHaveValue("m");
    expect(screen.getByLabelText("FX rate")).toHaveValue("7");
    expect(publish).not.toHaveBeenCalled();
  });

  it("uses the same currency catalog for pricing and optional reconciliation evidence", async () => {
    const user = userEvent.setup();
    openEditor(true);
    await user.click(
      await screen.findByText("Submit reconciliation evidence", {
        selector: "summary",
      }),
    );
    for (const name of [
      "Provider currency",
      "Settlement currency",
      "Checkout currency",
      "Refund currency",
    ]) {
      const select = screen.getByRole("combobox", {
        name,
      }) as HTMLSelectElement;
      expect(Array.from(select.options, (option) => option.value)).toEqual([
        "",
        "USD",
        "CNY",
      ]);
      expect(select.required).toBe(
        ["Provider currency", "Settlement currency"].includes(name),
      );
    }
  });

  it("round-trips the edited decimal strings and leaves unknown optional capabilities absent", async () => {
    const user = userEvent.setup();
    const publish = openEditor();
    await user.click(
      await screen.findByRole("button", { name: "Use for new version" }),
    );
    fireEvent.change(screen.getByLabelText("Input / million tokens"), {
      target: { value: precision },
    });
    await user.click(
      screen.getByRole("checkbox", { name: "Advanced JSON editor" }),
    );
    const json = screen.getByLabelText(
      "Pricing configuration (quote decimal amounts)",
    ) as HTMLTextAreaElement;
    expect(JSON.parse(json.value)).toEqual({
      ...stored,
      input_price: precision,
    });
    await user.click(
      screen.getByRole("checkbox", { name: "Advanced JSON editor" }),
    );
    expect(screen.getByLabelText("Input / million tokens")).toHaveValue(
      precision,
    );
    expect(publish).not.toHaveBeenCalled();
  });

  it("keeps an invalid numeric JSON draft visible without publishing or coercing it", async () => {
    const user = userEvent.setup();
    const publish = openEditor();
    await user.click(
      await screen.findByRole("button", { name: "Use for new version" }),
    );
    const toggle = screen.getByRole("checkbox", {
      name: "Advanced JSON editor",
    });
    await user.click(toggle);
    const json = screen.getByLabelText(
      "Pricing configuration (quote decimal amounts)",
    );
    const invalid = JSON.stringify({ ...stored, fx_rate: 1.1 });
    fireEvent.change(json, { target: { value: invalid } });
    await user.click(toggle);
    expect(toggle).toBeChecked();
    expect(json).toHaveValue(invalid);
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(publish).not.toHaveBeenCalled();
  });

  it("requires a native unit price and omits token-only fields for documents", async () => {
    const user = userEvent.setup();
    const publish = openEditor();
    await user.click(
      await screen.findByRole("button", { name: "Use for new version" }),
    );
    await user.selectOptions(screen.getByLabelText("Usage unit"), "documents");
    expect(
      screen.queryByLabelText("Input / million tokens"),
    ).not.toBeInTheDocument();
    const native = screen.getByLabelText("Price per unit");
    expect(native).toBeRequired();
    expect(native).toHaveValue("");
    expect(screen.getByLabelText("Reported usage meter")).toHaveValue(
      "documents",
    );
    fireEvent.change(native, { target: { value: precision } });
    await user.click(
      screen.getByRole("button", { name: "Publish new version" }),
    );
    await user.click(
      within(screen.getByRole("dialog")).getByRole("button", {
        name: "Publish",
      }),
    );
    const {
      input_price: _input,
      output_price: _output,
      ...commercial
    } = stored;
    expect(publish).toHaveBeenCalledExactlyOnceWith({
      ...commercial,
      unit: "documents",
      meter: "documents",
      unit_price: precision,
    });
  });

  it("preserves entered prices after a failed publish and retries with the same config", async () => {
    const user = userEvent.setup();
    const publish = openEditor();
    publish.mockRejectedValueOnce(new Error("unavailable"));
    await user.click(
      await screen.findByRole("button", { name: "Use for new version" }),
    );
    const input = screen.getByLabelText("Input / million tokens");
    fireEvent.change(input, { target: { value: precision } });
    const submit = screen.getByRole("button", { name: "Publish new version" });
    await user.click(submit);
    await user.click(
      within(screen.getByRole("dialog")).getByRole("button", {
        name: "Publish",
      }),
    );
    await screen.findByRole("alert");
    expect(input).toHaveValue(precision);
    expect(submit).toBeEnabled();
    await user.click(submit);
    await user.click(
      within(screen.getByRole("dialog")).getByRole("button", {
        name: "Publish",
      }),
    );
    await waitFor(() => expect(publish).toHaveBeenCalledTimes(2));
    expect(publish.mock.calls).toEqual([
      [{ ...stored, input_price: precision }],
      [{ ...stored, input_price: precision }],
    ]);
  });
});

describe("plan displays", () => {
  function servePlan(currency: string, description: string | null = null) {
    const write = vi.fn();
    const plan = {
      id: 1,
      name: "Plus",
      plan_type: "one_time",
      is_active: true,
      price_cents: 500,
      currency,
      formatted_price: currency.trim().toUpperCase() === "USD" ? "5.00" : null,
      credit_grant: "1000.000000",
      concurrency_limit: 3,
      description,
      rate_limits: {},
    };
    vi.mocked(api).mockImplementation(async (path, init) => {
      if (path.endsWith("/billing/context"))
        return {
          payment_enabled: true,
          providers: [],
          currencies: ["USD", "CNY", "USDT"],
          currency_minor_exponents: { USD: 2, CNY: 2, USDT: 6 },
        };
      if (path.endsWith("/plans/1") && init?.method === "PUT") {
        write(JSON.parse(String(init.body)));
        return plan;
      }
      if (path.endsWith("/plans")) return [plan];
      throw new Error("Unexpected request: " + path);
    });
    return write;
  }

  it.each([
    ["usd", "USD"],
    [" usd ", "USD"],
    ["usdt", "USDT"],
    [" BTC ", ""],
  ])(
    "shows the stored currency %j without rewriting it during a name change",
    async (raw, expected) => {
      const user = userEvent.setup();
      const write = servePlan(raw);
      renderPage(<PlansAdmin />, "/admin/plans");
      await user.click(await screen.findByRole("button", { name: "Edit" }));
      const editor = within(screen.getByRole("dialog"));
      const select = (await editor.findByRole("combobox", {
        name: "Currency",
      })) as HTMLSelectElement;
      expect(select.value).toBe(expected);
      expect(Array.from(select.options, (option) => option.value)).toEqual([
        "",
        "USD",
        "CNY",
        "USDT",
      ]);
      if (!expected) {
        expect(select.selectedOptions[0].disabled).toBe(true);
        expect(select.selectedOptions[0].textContent).toContain(raw);
      }
      fireEvent.change(editor.getByLabelText("Plan name"), {
        target: { value: "Renamed" },
      });
      await user.click(editor.getByRole("button", { name: "Save plan" }));
      await waitFor(() => expect(write).toHaveBeenCalledOnce());
      const payload = write.mock.calls[0][0];
      expect(payload.name).toBe("Renamed");
      expect(payload).not.toHaveProperty("currency");
      expect(payload).not.toHaveProperty("price_cents");
    },
  );

  it.each([
    [
      "en",
      "Concurrent task limit: 3",
      "Per-user business execution limit",
      "Queued and running executions count",
      "Repo Scan",
      "Customer description",
    ],
    [
      "zh-CN",
      "同时任务上限：3",
      "每用户业务执行上限",
      "排队和运行均计入",
      "仓库扫描",
      null,
    ],
  ] as const)(
    "keeps customer copy and administrator limit guidance separate in %s",
    async (language, limit, label, help, scans, description) => {
      const user = userEvent.setup();
      identity.payments = true;
      servePlan("USD", description);
      const view = renderPage(
        <Routes>
          <Route path="/billing/*" element={<BillingPage />} />
        </Routes>,
        "/billing/plans",
        language,
      );
      const card = (
        await screen.findByRole("heading", { name: "Plus" })
      ).closest("section")!;
      expect(card).toHaveTextContent(limit);
      expect(card).toHaveTextContent("USD 5.00");
      expect(card).toHaveTextContent("1000 Credits");
      expect(card).not.toHaveTextContent(label);
      expect(card).not.toHaveTextContent(help);
      if (description) expect(card).toHaveTextContent(description);
      view.unmount();

      renderPage(<PlansAdmin />, "/admin/plans", language);
      await user.click(
        await screen.findByRole("button", {
          name: language === "en" ? "Edit" : "编辑",
        }),
      );
      const dialog = screen.getByRole("dialog");
      await within(dialog).findByText(label);
      expect(dialog).toHaveTextContent(help);
      expect(dialog).toHaveTextContent(scans);
    },
  );

  it("shows the original integer when the historical currency scale is unknown", () => {
    renderPage(
      <>
        <Money currency="ZZZ" minor={321} amount={null} />
        <Money currency="ZZZ" minor={654} amount={null} />
      </>,
      "/billing/orders",
    );
    expect(screen.getByText("ZZZ 321 · unknown units")).toBeInTheDocument();
    expect(screen.getByText("ZZZ 654 · unknown units")).toBeInTheDocument();
  });
});
