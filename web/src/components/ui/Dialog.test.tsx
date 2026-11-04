import { useState, type ReactNode } from "react";
import {
  act,
  render,
  renderHook,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import { LocaleProvider } from "../../app/locale";
import { ConfirmProvider, useConfirm } from "./ConfirmProvider";
import { Dialog } from "./Dialog";

function Providers({ children }: { children: ReactNode }) {
  return (
    <LocaleProvider initial="en">
      <ConfirmProvider>{children}</ConfirmProvider>
    </LocaleProvider>
  );
}

const options = {
  title: "Delete item?",
  description: "Delete the selected item.",
  destructive: true,
};
beforeEach(() => localStorage.clear());

describe("shared confirmation dialog", () => {
  it("declines a request when the provider is missing", async () => {
    const { result } = renderHook(useConfirm);
    await expect(result.current(options)).resolves.toBe(false);
  });

  it("exposes the title and description and requires explicit confirmation", async () => {
    const user = userEvent.setup();
    const { result } = renderHook(useConfirm, { wrapper: Providers });
    let answer!: Promise<boolean>;
    act(() => {
      answer = result.current(options);
    });
    const dialog = screen.getByRole("dialog", { name: options.title });
    expect(dialog).toHaveAccessibleDescription(options.description);
    await user.click(within(dialog).getByRole("button", { name: "Confirm" }));
    await expect(answer).resolves.toBe(true);
  });

  it.each(["Cancel", "Close", "Escape"])(
    "declines a request on %s",
    async (action) => {
      const user = userEvent.setup();
      const { result } = renderHook(useConfirm, { wrapper: Providers });
      let answer!: Promise<boolean>;
      act(() => {
        answer = result.current(options);
      });
      if (action === "Escape") await user.keyboard("{Escape}");
      else await user.click(screen.getByRole("button", { name: action }));
      await expect(answer).resolves.toBe(false);
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    },
  );

  it("declines superseded and unmounted requests without leaving a pending promise", async () => {
    const { result, unmount } = renderHook(useConfirm, { wrapper: Providers });
    let first!: Promise<boolean>, second!: Promise<boolean>;
    act(() => {
      first = result.current(options);
      second = result.current({ ...options, title: "Second request" });
    });
    await expect(first).resolves.toBe(false);
    expect(
      screen.getByRole("dialog", { name: "Second request" }),
    ).toBeInTheDocument();
    unmount();
    await expect(second).resolves.toBe(false);
  });
});

function NestedDialogs() {
  const [outer, setOuter] = useState(false);
  const [inner, setInner] = useState(false);
  return (
    <>
      <button onClick={() => setOuter(true)}>Open editor</button>
      <Dialog open={outer} onOpenChange={setOuter} title="Editor">
        <button onClick={() => setInner(true)}>Open details</button>
        <Dialog open={inner} onOpenChange={setInner} title="Details">
          <button disabled>Unavailable</button>
        </Dialog>
      </Dialog>
    </>
  );
}

it("keeps keyboard focus inside the top dialog and Escape closes only that dialog", async () => {
  const user = userEvent.setup();
  render(
    <Providers>
      <NestedDialogs />
    </Providers>,
  );
  await user.click(screen.getByRole("button", { name: "Open editor" }));
  await user.click(screen.getByRole("button", { name: "Open details" }));
  const inner = screen.getByRole("dialog", { name: "Details" });
  await user.tab();
  expect(inner).toContainElement(document.activeElement as HTMLElement);
  await user.tab({ shift: true });
  expect(inner).toContainElement(document.activeElement as HTMLElement);
  await user.keyboard("{Escape}");
  expect(
    screen.queryByRole("dialog", { name: "Details" }),
  ).not.toBeInTheDocument();
  expect(screen.getByRole("dialog", { name: "Editor" })).toBeInTheDocument();
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "Open details" })).toHaveFocus(),
  );
  await user.keyboard("{Escape}");
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "Open editor" })).toHaveFocus(),
  );
});
