import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { UseQueryResult } from "@tanstack/react-query";
import { expect, it, vi } from "vitest";
import { LocaleProvider } from "../../app/locale";
import VersionBrowser, { type ReleaseRecord } from "./VersionBrowser";
import type { Catalog } from "./state";

function query<T>(data: T) {
  return {
    data,
    isPending: false,
    isError: false,
    isFetching: false,
    refetch: vi.fn(),
  } as unknown as UseQueryResult<T, Error>;
}

it("starts on the running channel and keeps explicit channel selection", async () => {
  const user = userEvent.setup();
  render(
    <LocaleProvider initial="en">
      <VersionBrowser
        currentChannel="development"
        catalog={query<Catalog>({ stale: false, heads: {}, images: [] })}
        releases={query({ releases: [] })}
        selected={null}
        selectionDisabled={false}
        onSelect={vi.fn()}
      />
    </LocaleProvider>,
  );
  const channel = screen.getByRole("combobox", { name: "Update channel" });
  expect(channel).toHaveValue("development");
  await user.selectOptions(channel, "stable");
  expect(channel).toHaveValue("stable");
});

it("expands release notes inside their record and does not open a dialog", async () => {
  const user = userEvent.setup();
  render(
    <LocaleProvider initial="en">
      <VersionBrowser
        currentChannel="stable"
        catalog={query<Catalog>({ stale: false, heads: {}, images: [] })}
        releases={query<{ releases: ReleaseRecord[] }>({
          releases: [
            {
              tag_name: "v9.0.0",
              name: "Release nine",
              body: "## Fixed\n\nUseful **notes**.",
            },
          ],
        })}
        selected={null}
        selectionDisabled={false}
        onSelect={vi.fn()}
      />
    </LocaleProvider>,
  );
  await user.click(screen.getByRole("button", { name: "Release history" }));
  await user.click(screen.getByRole("button", { name: "Read notes" }));
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  const record = screen.getByText("Release nine").closest("article")!;
  expect(within(record).getByRole("heading", { name: "Fixed" })).toBeVisible();
  await user.click(within(record).getByRole("button", { name: "Hide notes" }));
  expect(
    within(record).queryByRole("heading", { name: "Fixed" }),
  ).not.toBeInTheDocument();
});
