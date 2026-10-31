import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { StructuredEditor } from "./StructuredEditor";
import { accountPayload } from "./AccountsPanel";

describe("structured configuration editors", () => {
  it("edits a known property without dropping unknown historical siblings", () => {
    const update = vi.fn();
    render(
      <StructuredEditor
        value={{ enabled: true, historical: "retained" }}
        schema={{
          type: "object",
          properties: { enabled: { type: "boolean" } },
          required: ["enabled"],
        }}
        label="Policy"
        onChange={update}
        message={(key) => key}
      />,
    );
    fireEvent.click(screen.getByRole("switch", { name: "Policy: enabled" }));
    expect(update).toHaveBeenCalledWith({
      enabled: false,
      historical: "retained",
    });
    expect(
      screen.queryByRole("button", { name: "editor_remove: enabled" }),
    ).toBeNull();
  });
  it("keeps built-in map names fixed while allowing new entries", () => {
    const update = vi.fn();
    render(
      <StructuredEditor
        value={{ bug: { color: "ff0000" } }}
        schema={{
          type: "object",
          "x-fixedKeys": ["bug"],
          additionalProperties: {
            type: "object",
            properties: { color: { type: "string" } },
            required: ["color"],
          },
        }}
        label="Labels"
        onChange={update}
        message={(key) => key}
      />,
    );
    expect(
      screen.queryByRole("button", { name: "editor_remove: bug" }),
    ).toBeNull();
    fireEvent.change(
      screen.getByRole("textbox", { name: "editor_key: Labels" }),
      { target: { value: "feature" } },
    );
    fireEvent.click(screen.getByRole("button", { name: "editor_add: Labels" }));
    expect(update).toHaveBeenCalledWith({
      bug: { color: "ff0000" },
      feature: { color: "" },
    });
  });
  it("reorders list entries without flattening object values", () => {
    const update = vi.fn();
    render(
      <StructuredEditor
        value={["first", "second"]}
        schema={{ type: "array", items: { type: "string" } }}
        label="Rules"
        onChange={update}
        message={(key) => key}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "editor_move_up: 2" }));
    expect(update).toHaveBeenCalledWith(["second", "first"]);
  });
});

describe("account payload", () => {
  it("retains empty credential semantics and durable account revision", () => {
    const values = {
      name: "Primary",
      provider_id: "provider",
      protocol: "protocol",
      api_base: "https://example.test",
      api_key: "",
      region: "",
      default_model: "model",
      models_text: "one\ntwo\none",
      enabled: false,
      notes: "",
    };
    const account = {
      id: "acc_one",
      name: "Previous",
      provider_id: "provider",
      protocol: "protocol",
      api_base: "https://example.test",
      has_key: true,
      region: "",
      models: [],
      default_model: "model",
      enabled: true,
      notes: "",
      revision: 17,
      updated_at: 123,
    };
    expect(accountPayload(values, account)).toMatchObject({
      id: "acc_one",
      revision: 17,
      updated_at: 123,
      api_key: "",
      enabled: false,
      models: ["one", "two"],
    });
    expect(accountPayload(values)).not.toHaveProperty("revision");
  });
});
