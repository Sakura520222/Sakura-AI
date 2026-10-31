import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { api } from "../../api/client";
import { LocaleProvider } from "../../app/locale";
import { ConfirmProvider } from "../../components/ui/ConfirmProvider";
import BillingPage from "./BillingPage";
import AdminPage from "../admin/AdminPage";
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
function renderPage(children: ReactNode, path: string) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <LocaleProvider initial="en">
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
