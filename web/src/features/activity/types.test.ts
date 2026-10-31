import { describe, expect, it } from "vitest";
import { mergeEntries, type Entry } from "./types";

const entry = (id: string, order_key: string, content = ""): Entry => ({
  id,
  order_key,
  content,
  type: "message",
  created_at: null,
});
describe("activity projection reconciliation", () => {
  it("updates a repeated entry once and preserves historical entries", () => {
    const result = mergeEntries(
      [
        entry("message:1", "100:1:1", "first"),
        entry("message:2", "101:1:2", "partial"),
      ],
      [
        entry("message:2", "101:1:2", "complete"),
        entry("message:3", "102:1:3"),
      ],
    );
    expect(result.map((item) => item.id)).toEqual([
      "message:1",
      "message:2",
      "message:3",
    ]);
    expect(result[1].content).toBe("complete");
  });
  it("sorts server ordering numerically without losing integer precision", () => {
    expect(
      mergeEntries(
        [],
        [
          entry("later", "999999999999999999:2:10"),
          entry("earlier", "999999999999999999:2:9"),
        ],
      ).map((item) => item.id),
    ).toEqual(["earlier", "later"]);
  });
});
