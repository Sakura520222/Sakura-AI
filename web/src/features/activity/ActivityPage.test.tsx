import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { api } from "../../api/client";
vi.mock("../../api/client", async (original) => ({
  ...(await original<object>()),
  api: vi.fn(),
}));
import { LocaleProvider } from "../../app/locale";
import { TimelineEntry } from "./ActivityPage";
import type { Entry } from "./types";
function show(entry: Entry) {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <LocaleProvider initial="en">
        <TimelineEntry entry={entry} sessionId="1" />
      </LocaleProvider>
    </QueryClientProvider>,
  );
}
it("renders tool result JSON and its call identity without a content button", () => {
  show({
    id: "tool_result:1",
    type: "tool_result",
    order_key: "1:7:1",
    created_at: null,
    name: "read_file",
    tool_call_id: "call-abc",
    status: "failed",
    content_visibility: "visible",
    content: '{"error":"missing file"}',
    started_at: "2026-11-13T00:00:00Z",
    completed_at: "2026-11-13T00:00:01Z",
  });
  expect(screen.getByText("read_file")).toBeInTheDocument();
  expect(screen.getByText(/call-abc/)).toBeInTheDocument();
  expect(screen.getByText(/"error"/)).toBeInTheDocument();
  expect(screen.getByText(/1000 ms/)).toBeInTheDocument();
});
it("bounds long content and expands only on demand", async () => {
  show({
    id: "tool_result:2",
    type: "tool_result",
    order_key: "1:7:2",
    created_at: null,
    content_visibility: "visible",
    content: "a".repeat(20000) + "TAIL",
  });
  expect(screen.queryByText(/TAIL/)).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Expand" }));
  expect(screen.queryByText(/TAIL/)).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Show more" }));
  expect(screen.getByText(/TAIL/)).toBeInTheDocument();
});
it("never renders a restricted payload even if the field is present", () => {
  show({
    id: "message:3",
    type: "message",
    order_key: "1:3:3",
    created_at: null,
    content_visibility: "restricted",
    content: "SECRET",
  });
  expect(screen.queryByText("SECRET")).not.toBeInTheDocument();
  expect(
    screen.getByText("You do not have permission to view this content."),
  ).toBeInTheDocument();
});

it("loads only an authorized canonical user message and renders only its content", async () => {
  vi.mocked(api).mockResolvedValue({
    payload: JSON.stringify({
      role: "user",
      content: "Ordinary message",
      private_metadata: "DO NOT DISPLAY",
    }),
  });
  show({
    id: "message:4",
    type: "message",
    role: "user",
    order_key: "1:3:4",
    created_at: null,
    content_visibility: "restricted",
    artifacts: [
      {
        artifact_id: 41,
        artifact_kind: "request_projection",
        can_request: true,
      },
    ],
  });
  expect(await screen.findByText("Ordinary message")).toBeInTheDocument();
  expect(screen.queryByText(/DO NOT DISPLAY/)).not.toBeInTheDocument();
});
it("does not fetch an unauthorized canonical message artifact", () => {
  vi.mocked(api).mockClear();
  show({
    id: "message:5",
    type: "message",
    role: "user",
    order_key: "1:3:5",
    created_at: null,
    content_visibility: "restricted",
    artifacts: [
      {
        artifact_id: 42,
        artifact_kind: "request_projection",
        can_request: false,
      },
    ],
  });
  expect(api).not.toHaveBeenCalled();
});

it("bounds pretty JSON expansion for compact deeply nested input", () => {
  const content = "[".repeat(999) + "0" + "]".repeat(999);
  const view = show({
    id: "tool_result:8",
    type: "tool_result",
    order_key: "1:7:8",
    created_at: null,
    content_visibility: "visible",
    content,
  });
  expect(
    view.container.querySelector("code")?.textContent?.length ?? Infinity,
  ).toBeLessThanOrEqual(2000);
});
it("auto-loads exact backend tool artifacts once while keeping native artifacts gated", async () => {
  vi.mocked(api).mockClear();
  vi.mocked(api).mockImplementation(async (path) => ({
    payload: path.endsWith("/51")
      ? '{"path":"README.md"}'
      : '{"error":"missing file"}',
  }));
  const artifacts = [
    { artifact_id: 51, artifact_kind: "request_projection", can_request: true },
    {
      artifact_id: 52,
      artifact_kind: "response_projection",
      can_request: true,
    },
  ];
  const common = {
    created_at: null,
    name: "read_file",
    tool_execution_id: 9,
    tool_call_id: "canonical-tool",
    status: "failed",
    arguments_artifact_id: 51,
    result_artifact_id: 52,
    artifacts,
  };
  render(
    <QueryClientProvider client={new QueryClient()}>
      <LocaleProvider initial="en">
        <TimelineEntry
          sessionId="1"
          entry={{
            ...common,
            id: "tool_call:9",
            type: "tool_call",
            order_key: "1:6:9",
          }}
          pairedResult={{
            ...common,
            id: "tool_result:9",
            type: "tool_result",
            order_key: "1:7:9",
            content_visibility: "restricted",
          }}
        />
      </LocaleProvider>
    </QueryClientProvider>,
  );
  expect(await screen.findByText(/README.md/)).toBeInTheDocument();
  expect(await screen.findByText(/missing file/)).toBeInTheDocument();
  expect(api).toHaveBeenCalledTimes(2);
  expect(
    screen.queryByRole("button", { name: /View protected content/ }),
  ).not.toBeInTheDocument();
});
it("does not auto-fetch denied tool artifacts or unrelated native projections", () => {
  vi.mocked(api).mockClear();
  show({
    id: "tool_call:10",
    type: "tool_call",
    order_key: "1:6:10",
    created_at: null,
    arguments_artifact_id: 61,
    artifacts: [
      {
        artifact_id: 61,
        artifact_kind: "request_projection",
        can_request: false,
      },
      {
        artifact_id: 62,
        artifact_kind: "request_projection",
        can_request: true,
      },
    ],
  });
  expect(api).not.toHaveBeenCalled();
  expect(
    screen.getByRole("button", { name: /View protected content/ }),
  ).toBeInTheDocument();
});
