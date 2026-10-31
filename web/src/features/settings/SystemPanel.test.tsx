import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { api, ApiError } from "../../api/client";
import { LocaleProvider } from "../../app/locale";
import { SystemPanel } from "./SystemPanel";
import { SettingsStore } from "./state";

vi.mock("../../api/client", async (original) => ({
  ...(await original<object>()),
  api: vi.fn(),
}));

it.each([
  new ApiError(503, {}),
  new TypeError("network"),
  new SyntaxError("invalid JSON"),
])(
  "keeps a pending reset guarded and releases the dialog after %s",
  async (failure) => {
    localStorage.clear();
    const user = userEvent.setup();
    let reject!: (error: Error) => void;
    vi.mocked(api).mockImplementation(async (path) => {
      if (path === "/api/v1/webui/system")
        return { database_reset_confirmation: "RESET" };
      if (path === "/api/v1/webui/system/reset-database")
        return new Promise<never>((_, fail) => {
          reject = fail;
        });
      throw new Error(`Unexpected request: ${path}`);
    });
    const client = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false },
      },
    });
    render(
      <QueryClientProvider client={client}>
        <LocaleProvider initial="en">
          <SystemPanel store={new SettingsStore()} onApplySlug={() => {}} />
        </LocaleProvider>
      </QueryClientProvider>,
    );
    await user.click(
      screen.getByText("Reset database", { selector: "summary" }),
    );
    const trigger = screen.getByRole("button", { name: "Reset database…" });
    await waitFor(() => expect(trigger).toBeEnabled());
    await user.click(trigger);
    const dialog = screen.getByRole("dialog", {
      name: "Permanently erase the database",
    });
    const erase = within(dialog).getByRole("button", {
      name: "Erase and restart",
    });
    expect(erase).toBeDisabled();
    await user.type(within(dialog).getByRole("textbox"), "RESET");
    await user.click(erase);
    await waitFor(() => expect(erase).toBeDisabled());
    await user.click(within(dialog).getByRole("button", { name: "Close" }));
    await user.keyboard("{Escape}");
    expect(dialog).toBeInTheDocument();
    await act(async () => reject(failure));
    await within(dialog).findByRole("alert");
    expect(erase).toBeEnabled();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  },
);
